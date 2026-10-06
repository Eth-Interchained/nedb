#!/usr/bin/env python3
"""Measure shipped NEDB backfill against an already populated, isolated Postgres."""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time
import uuid

from postgres_storage import attach, docker, footprint, layout, payload, snapshot


def connect(dsn):
    import psycopg2
    deadline = time.monotonic() + 90
    while True:
        try:
            return psycopg2.connect(dsn, connect_timeout=3)
        except psycopg2.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def ratio(allocated, logical):
    if logical <= 0:
        raise ValueError('logical source bytes must be positive')
    return allocated / logical


def worker(args):
    root = Path(args.work_dir)
    raw = connect(args.dsn)
    if args.verify:
        from nedb.backends.dag import DagBackend
        dag = DagBackend(str(root / 'nedb'))
        if not dag.verify() or dag.seq < 0:
            raise RuntimeError('Reopened DAG verification failed')
        seen = 0
        with raw.cursor(name='backfill_verify') as cur:
            cur.itersize = 100
            cur.execute('SELECT id, version, payload FROM storage_bench ORDER BY id')
            for i, version, body in cur:
                doc = dag.get('storage_bench', str(i))
                if version != 0 or body != payload(i, 0, args.record_bytes):
                    raise RuntimeError(f'Host mismatch at {i}')
                if not doc or doc.get('id') != i or doc.get('version') != version or doc.get('payload') != body or doc.get('_source') != 'backfill':
                    raise RuntimeError(f'Backfill mismatch at {i}')
                seen += 1
        if seen != args.rows:
            raise RuntimeError('Reopened host row count mismatch')
        (root / 'verification.json').write_text(json.dumps({'ok': True, 'host_rows_checked': seen,
            'shadow_rows_checked': seen, 'head': dag.head, 'seq': dag.seq}, indent=2))
        raw.close()
        return

    # All source writes precede creation of the wrapper; none are shadowed.
    with raw.cursor() as cur:
        for setting in ('fsync', 'full_page_writes', 'synchronous_commit'):
            cur.execute('SHOW ' + setting)
            if cur.fetchone()[0] != 'on':
                raise RuntimeError(f'{setting} must be on')
        cur.execute('CREATE TABLE storage_bench (id BIGINT PRIMARY KEY, version INTEGER NOT NULL, payload TEXT NOT NULL)')
    raw.commit()
    start = time.perf_counter()
    with raw.cursor() as cur:
        for i in range(args.rows):
            cur.execute('INSERT INTO storage_bench (id, version, payload) VALUES (%s,%s,%s)',
                        (i, 0, payload(i, 0, args.record_bytes)))
            if (i + 1) % args.batch == 0:
                raw.commit()
    raw.commit()
    load_seconds = time.perf_counter() - start
    raw.autocommit = True
    with raw.cursor() as cur:
        cur.execute('VACUUM (ANALYZE) storage_bench')
        cur.execute('CHECKPOINT')
        cur.execute('SELECT count(*), sum(octet_length(payload)) FROM storage_bench')
        count, logical = cur.fetchone()
    if count != args.rows or int(logical) != args.rows * args.record_bytes:
        raise RuntimeError('Source payload count/size mismatch')
    before_attach = snapshot(raw, root)
    (root / 'source.json').write_text(json.dumps({'logical_payload_bytes': int(logical),
        'rows': count, 'load_seconds': load_seconds, 'footprint': before_attach}, indent=2))
    db = attach(raw, root / 'nedb')
    db.nedb.shadow_writes = False
    before = snapshot(raw, root)
    stop = threading.Event()
    peak = [before['combined_allocated_bytes']]

    def sample():
        while not stop.is_set():
            peak[0] = max(peak[0], footprint(root / 'pg')['allocated_bytes'] + footprint(root / 'nedb')['allocated_bytes'])
            stop.wait(1)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        start = time.perf_counter()
        imported = db.nedb.backfill(batch_size=args.batch)
        backfill_seconds = time.perf_counter() - start
        start = time.perf_counter()
        db.nedb.checkpoint()
        checkpoint_seconds = time.perf_counter() - start
        after = snapshot(raw, root)
        if imported != args.rows or db.nedb.shadow_errors:
            raise RuntimeError(f'Incomplete backfill: {imported}/{args.rows}, errors={db.nedb.shadow_errors}')
        if not db.nedb.verify():
            raise RuntimeError('Backfill verify() failed')
        with raw.cursor() as cur:
            cur.execute('SELECT count(*), sum(octet_length(payload)) FROM storage_bench')
            if cur.fetchone() != (count, logical):
                raise RuntimeError('Source changed during backfill')
        import nedb
        allocated = after['nedb']['allocated_bytes']
        result = {'status': 'ok', 'commit': os.environ.get('GITHUB_SHA', 'local'),
            'platform': platform.platform(), 'postgres_version': raw.server_version, 'nedb_version': nedb.__version__,
            'target_bytes': args.target_bytes, 'logical_payload_bytes': int(logical), 'rows': args.rows,
            'record_bytes': args.record_bytes, 'batch_size': args.batch,
            'raw_load_seconds': load_seconds, 'backfill_seconds': backfill_seconds,
            'checkpoint_seconds': checkpoint_seconds, 'durable_backfill_seconds': backfill_seconds + checkpoint_seconds,
            'imported_rows': imported, 'postgres_before_attach': before_attach, 'before_backfill': before,
            'after_backfill': after, 'nedb_growth_allocated_bytes': allocated - before['nedb']['allocated_bytes'],
            'nedb_allocated_per_logical_payload_byte': ratio(allocated, int(logical)),
            'estimated_nedb_bytes_for_10tb_logical_payload': ratio(allocated, int(logical)) * 10_000_000_000_000,
            'sampled_peak_combined_allocated_bytes': max(peak[0], after['combined_allocated_bytes']),
            'sample_interval_seconds': 1}
        (root / 'backfill-results.json').write_text(json.dumps(result, indent=2))
        print(f'backfilled {imported:,} rows: NEDB {allocated:,} allocated bytes; ratio {result["nedb_allocated_per_logical_payload_byte"]:.4f}', flush=True)
    finally:
        stop.set()
        sampler.join()
        raw.close()


def render(r):
    return '\n'.join([
        '# PostgreSQL backfill storage exposure', '',
        f'Commit: `{r["commit"]}` · rows: {r["rows"]:,} · record payload: {r["record_bytes"]:,} bytes', '',
        '| Measurement | Result |', '|---|---:|',
        f'| Actual logical source payload | {r["logical_payload_bytes"]:,} bytes |',
        f'| PostgreSQL allocated before backfill | {r["before_backfill"]["postgres"]["allocated_bytes"]:,} bytes |',
        f'| NEDB allocated after durable backfill | {r["after_backfill"]["nedb"]["allocated_bytes"]:,} bytes |',
        f'| NEDB apparent after backfill | {r["after_backfill"]["nedb"]["apparent_bytes"]:,} bytes |',
        f'| NEDB additional allocated bytes | {r["nedb_growth_allocated_bytes"]:,} bytes |',
        f'| NEDB allocated / logical payload | {r["nedb_allocated_per_logical_payload_byte"]:.4f}× |',
        f'| Backfill plus checkpoint | {r["durable_backfill_seconds"]:.3f} seconds |',
        f'| Sampled peak PG + NEDB allocated | {r["sampled_peak_combined_allocated_bytes"]:,} bytes |',
        f'| Restart verification | {r["verification"]["ok"]} · {r["verification"]["shadow_rows_checked"]:,} shadow rows checked |', '',
        f'**Extrapolation only:** 10 TB of comparable logical payload would imply approximately {r["estimated_nedb_bytes_for_10tb_logical_payload"] / 1e12:.3f} TB of initial NEDB allocation.', '',
        'Source is loaded through raw PostgreSQL before attaching NEDB. The shipped backfill API captures full records; shadow writes remain disabled.',
        'Payload bytes are measured with octet_length in Postgres, excluding IDs and version metadata. MB/GB/TB are decimal.',
        'Synthetic seeded hex payloads are one data shape. Compare 100 MB and 1 GB runs to assess ratio stability; customer data may compress differently.',
        'The 10 TB estimate applies to logical record payload, not PostgreSQL cluster size, indexes, WAL or free space. It excludes future history growth.',
        'Allocated PG includes retained WAL. Peak sampling waits one second between scans and can miss brief peaks; scans add overhead.',
        'Backfill fetch batch size is not a durability boundary. The timer separately reports the final durable NEDB checkpoint.',
        'Postgres restarts and NEDB reopens in a fresh process. Every source row and matching shadow row is checked; verify() must pass.',
        'This isolated, static-source run does not establish consistency under concurrent host writes.', ''])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--target-bytes', type=int, default=1_000_000_000)
    ap.add_argument('--record-bytes', type=int, default=16384)
    ap.add_argument('--batch', type=int, default=100)
    ap.add_argument('--work-dir', required=True)
    ap.add_argument('--worker', action='store_true')
    ap.add_argument('--verify', action='store_true')
    ap.add_argument('--dsn')
    args = ap.parse_args()
    if min(args.target_bytes, args.record_bytes, args.batch) < 1:
        ap.error('sizes and batch must be positive')
    args.rows, _ = layout(args.target_bytes, args.record_bytes)
    if args.worker:
        worker(args)
        return
    root = Path(args.work_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / 'pg').mkdir()
    (root / 'nedb').mkdir()
    name = 'nedb-backfill-' + uuid.uuid4().hex[:12]
    result = {}
    try:
        docker('run', '-d', '--name', name, '-p', '127.0.0.1::5432', '-e', 'POSTGRES_PASSWORD=bench',
               '-v', f'{root / "pg"}:/var/lib/postgresql/data', 'postgres:17',
               '-c', 'fsync=on', '-c', 'synchronous_commit=on', '-c', 'full_page_writes=on')
        image_id = docker('inspect', name, '--format', '{{.Image}}', capture_output=True).stdout.strip()
        def dsn():
            port = docker('port', name, '5432/tcp', capture_output=True).stdout.strip().rsplit(':', 1)[1]
            return f'postgresql://postgres:bench@127.0.0.1:{port}/postgres'
        cmd = [sys.executable, str(Path(__file__).resolve()), '--worker', '--work-dir', str(root),
               '--target-bytes', str(args.target_bytes), '--record-bytes', str(args.record_bytes),
               '--batch', str(args.batch), '--dsn', dsn()]
        subprocess.run(cmd, check=True)
        result = json.loads((root / 'backfill-results.json').read_text())
        result['postgres_image_id'] = image_id
        docker('restart', name)
        cmd[-1] = dsn()
        subprocess.run(cmd + ['--verify'], check=True)
        result['verification'] = json.loads((root / 'verification.json').read_text())
        (root / 'backfill-results.md').write_text(render(result))
    except Exception as exc:
        result.update(status='error', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        (root / 'backfill-results.json').write_text(json.dumps(result, indent=2))
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
