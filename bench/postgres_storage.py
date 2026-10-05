#!/usr/bin/env python3
"""Owned, isolated Docker PostgreSQL A/B storage benchmark. Never targets a user DB."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time
import uuid

os.environ['NEDB_FLUSH_MS'] = 'off'


def footprint(path):
    apparent = allocated = 0
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                st = os.stat(os.path.join(root, name), follow_symlinks=False)
            except FileNotFoundError:
                continue
            allocated += st.st_blocks * 512
            if name in files:
                apparent += st.st_size
    return {'apparent_bytes': apparent, 'allocated_bytes': allocated}


def payload(row, version, size):
    # Hex-encoded seeded entropy: deterministic, no giant in-memory dataset,
    # and deliberately resistant to PostgreSQL TOAST compression.
    return hashlib.shake_256(f'nedb-storage-v1:{row}:{version}'.encode()).hexdigest((size + 1)//2)[:size]


def layout(target, size):
    rows = (target + size - 1) // size
    return rows, rows * size


def expected(i, args):
    deleted = i < args.rows // 100
    version = args.rounds if i < args.rows // 10 else 0
    return deleted, version, payload(i, version, args.record_bytes)


def attach(raw, path):
    import nedb
    from nedb import wrap_postgresql
    if not nedb.__has_native__:
        raise RuntimeError('Native NEDB from this checkout is required')
    db = wrap_postgresql(raw, db_name='storage_bench', backend='dag', dag_path=str(path))
    db.nedb.auto_discover = False
    db.nedb.register('storage_bench', 'storage_bench', pk='id')
    db.nedb.strict_shadow = True
    db.nedb.shadow_writes = True
    return db


def snapshot(raw, root):
    with raw.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database()), pg_total_relation_size('storage_bench'), pg_current_wal_lsn()::text")
        database, relation, lsn = cur.fetchone()
    pg = footprint(root / 'pg')
    dag = footprint(root / 'nedb')
    return {'postgres': pg, 'postgres_wal': footprint(root / 'pg' / 'pg_wal'),
            'nedb': dag, 'combined_allocated_bytes': pg['allocated_bytes'] + dag['allocated_bytes'],
            'pg_database_bytes': database, 'pg_relation_bytes': relation, 'wal_lsn': lsn}


def worker(args):
    import psycopg2
    root = Path(args.work_dir)
    raw = psycopg2.connect(args.dsn)
    if args.verify:
        from nedb.backends.dag import DagBackend
        dag = DagBackend(str(root / 'nedb')) if args.shadow else None
        if dag and (not dag.verify() or dag.seq < 0):
            raise RuntimeError('Reopened provenance verification failed')
        seen = 0
        with raw.cursor(name='verify_rows') as cur:
            cur.itersize = 100
            cur.execute('SELECT id, version, payload FROM storage_bench ORDER BY id')
            for i, version, body in cur:
                deleted, want_version, want = expected(i, args)
                if deleted or (version, body) != (want_version, want):
                    raise RuntimeError(f'Host mismatch at {i}')
                seen += 1
        if seen != args.rows - args.rows // 100:
            raise RuntimeError('Host row count mismatch')
        # Check every shadow record, including retained deletion tombstones.
        if dag:
            for i in range(args.rows):
                deleted, version, body = expected(i, args)
                doc = dag.get('storage_bench', str(i))
                if not doc or doc.get('payload') != body or doc.get('version') != version or bool(doc.get('_deleted')) != deleted:
                    raise RuntimeError(f'Reopened shadow mismatch at {i}')
        result = {'ok': True, 'host_rows': seen, 'shadow_records_checked': args.rows if dag else 0,
                  'head': dag.head if dag else None, 'seq': dag.seq if dag else None}
        raw.close()
        (root / 'verification.json').write_text(json.dumps(result, indent=2))
        return
    with raw.cursor() as cur:
        cur.execute('SET synchronous_commit = on')
        for setting in ('fsync', 'full_page_writes', 'synchronous_commit'):
            cur.execute('SHOW ' + setting)
            if cur.fetchone()[0] != 'on':
                raise RuntimeError(f'{setting} must be on')
        cur.execute('CREATE TABLE storage_bench (id BIGINT PRIMARY KEY, version INTEGER NOT NULL, payload TEXT NOT NULL)')
    raw.commit()
    db = attach(raw, root / 'nedb') if args.shadow else raw
    phases = {}
    stop = threading.Event()
    peak = [0]

    def sample():
        while not stop.is_set():
            peak[0] = max(peak[0], footprint(root / 'pg')['allocated_bytes'] + footprint(root / 'nedb')['allocated_bytes'])
            stop.wait(1)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()

    def phase(name, count, operation):
        before = snapshot(raw, root)
        start = time.perf_counter()
        with db.cursor() as cur:
            for i in range(count):
                operation(cur, i)
                if (i + 1) % args.batch == 0:
                    db.commit()
                    if args.shadow:
                        db.nedb.checkpoint()
            if count % args.batch:
                db.commit()
                if args.shadow:
                    db.nedb.checkpoint()
        seconds = time.perf_counter() - start
        after = snapshot(raw, root)
        peak[0] = max(peak[0], before['combined_allocated_bytes'], after['combined_allocated_bytes'])
        with raw.cursor() as cur:
            cur.execute('SELECT pg_wal_lsn_diff(%s::pg_lsn, %s::pg_lsn)', (after['wal_lsn'], before['wal_lsn']))
            wal = int(cur.fetchone()[0])
        phases[name] = {'operations': count, 'seconds': seconds, 'ops_per_s': count / seconds if seconds else 0,
                        'before': before, 'after': after, 'wal_generated_bytes': wal,
                        'combined_growth_bytes': after['combined_allocated_bytes'] - before['combined_allocated_bytes']}
        (root / 'phases.json').write_text(json.dumps(phases, indent=2))
        print(f'{name}: {seconds:.2f}s, combined={after["combined_allocated_bytes"]:,} bytes', flush=True)

    baseline = snapshot(raw, root)
    try:
        phase('load', args.rows, lambda cur, i: cur.execute('INSERT INTO storage_bench (id, version, payload) VALUES (%s,%s,%s)', (i, 0, payload(i, 0, args.record_bytes))))
        for version in range(1, args.rounds + 1):
            phase(f'update_{version}', args.rows // 10, lambda cur, i: cur.execute('UPDATE storage_bench SET version=%s, payload=%s WHERE id=%s', (version, payload(i, version, args.record_bytes), i)))
        start = time.perf_counter()
        reads = min(1000, args.rows)
        with db.cursor() as cur:
            for i in range(reads):
                cur.execute('SELECT version, payload FROM storage_bench WHERE id=%s', (i,))
                version, body = cur.fetchone()
                want_version = args.rounds if i < args.rows // 10 else 0
                if version != want_version or body != payload(i, version, args.record_bytes):
                    raise RuntimeError(f'Point-read mismatch at {i}')
        seconds = time.perf_counter() - start
        read_size = snapshot(raw, root)
        phases['point_read'] = {'operations': reads, 'seconds': seconds, 'ops_per_s': reads / seconds,
                                'before': read_size, 'after': read_size,
                                'wal_generated_bytes': 0, 'combined_growth_bytes': 0}
        raw.commit()
        phase('delete', args.rows // 100, lambda cur, i: cur.execute('DELETE FROM storage_bench WHERE id=%s', (i,)))
        raw.commit()
        raw.autocommit = True
        with raw.cursor() as cur:
            cur.execute('VACUUM (ANALYZE) storage_bench')
            cur.execute('CHECKPOINT')
        maintenance = snapshot(raw, root)
        if args.shadow:
            surface = db.nedb
            if surface.shadow_errors or surface.unmirrored_tables or surface.partial_shadows:
                raise RuntimeError('Adapter reported incomplete or failed shadow writes')
            if not surface.verify():
                raise RuntimeError('Provenance verify failed before restart')
        import nedb
        result = {'baseline': baseline, 'phases': phases, 'after_maintenance': maintenance,
                  'sampled_peak_combined_allocated_bytes': max(peak[0], maintenance['combined_allocated_bytes']),
                  'sample_interval_seconds': 1, 'nedb_version': nedb.__version__,
                  'postgres_version': raw.server_version}
        (root / 'result.json').write_text(json.dumps(result, indent=2))
    finally:
        stop.set()
        sampler.join()
        raw.close()


def docker(*args, **kwargs):
    return subprocess.run(['docker', *args], check=True, text=True, **kwargs)


def render(out):
    lines = ['# PostgreSQL + NEDB storage exposure', '',
             f'Commit: `{out["commit"]}` · logical payload: {out["logical_payload_bytes"]:,} bytes · rows: {out["rows"]:,}', '',
             '| Phase | Raw PG allocated | Wrapped PG allocated | NEDB allocated | Added combined bytes | Wrapped/raw elapsed |',
             '|---|---:|---:|---:|---:|---:|']
    raw, wrapped = out['results']['raw'], out['results']['wrapped']
    for name, r in raw['phases'].items():
        w = wrapped['phases'][name]
        ra, wa = r['after'], w['after']
        lines.append(f'| {name} | {ra["postgres"]["allocated_bytes"]:,} | {wa["postgres"]["allocated_bytes"]:,} | {wa["nedb"]["allocated_bytes"]:,} | {wa["combined_allocated_bytes"] - ra["combined_allocated_bytes"]:,} | {w["seconds"]/r["seconds"]:.2f}× |')
    lines += ['', 'Fresh isolated PostgreSQL clusters per leg; same records, statements, batch commits, and durable settings.',
              '1 GB means 1,000,000,000 logical UTF-8 payload bytes, rounded up to a complete record; IDs and metadata are additional.',
              'Seeded entropy encoded as hex; this is one compression-resistant synthetic workload, not a representative customer dataset.',
              'Updates replace the same 10% of records each round; deletes remove 1%; the adapter retains deletion tombstones.',
              'Allocated PG includes the entire cluster and WAL. WAL size is a subset, not added twice. JSON also reports relation/database/apparent sizes and WAL generated.',
              'Peak is sampled once per second plus phase boundaries; shorter peaks can be missed. Sampling adds overhead to both legs.',
              'Write timing includes payload generation, client calls, host commits, and NEDB checkpoints. Point-read timing includes correctness checks. No physical I/O write-amplification claim is made.',
              'VACUUM ANALYZE and CHECKPOINT are measured after workload timing; ordinary VACUUM may retain reusable space. No provenance history pruning is performed.',
              'Postgres is restarted and NEDB reopened in a fresh process; every host row and every expected shadow record is checked, and verify() must pass.',
              'Sequential A/B on a shared runner is exploratory evidence. A 10 TB extrapolation is not a measured result.', '']
    for name, result in out['results'].items():
        lines += [f'{name}: sampled peak {result["sampled_peak_combined_allocated_bytes"]:,} allocated bytes; restart verification: {result["verification"]["ok"]}.']
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--target-bytes', type=int, default=1_000_000_000)
    ap.add_argument('--record-bytes', type=int, default=16384)
    ap.add_argument('--batch', type=int, default=100)
    ap.add_argument('--rounds', type=int, default=3)
    ap.add_argument('--work-dir', required=True)
    ap.add_argument('--image', default='postgres:17')
    ap.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--verify', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--shadow', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--dsn', help=argparse.SUPPRESS)
    args = ap.parse_args()
    if min(args.target_bytes, args.record_bytes, args.batch, args.rounds) < 1:
        ap.error('sizes, batch, and rounds must be positive')
    args.rows, actual = layout(args.target_bytes, args.record_bytes)
    if args.worker:
        worker(args)
        return
    root = Path(args.work_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    out = {'commit': os.environ.get('GITHUB_SHA', 'local'), 'platform': platform.platform(),
           'target_bytes': args.target_bytes, 'logical_payload_bytes': actual, 'rows': args.rows,
           'record_bytes': args.record_bytes, 'batch': args.batch, 'update_rounds': args.rounds,
           'image': args.image, 'results': {}}
    try:
        for label in ('raw', 'wrapped'):
            leg = root / label
            (leg / 'pg').mkdir(parents=True)
            (leg / 'nedb').mkdir()
            name = 'nedb-storage-' + uuid.uuid4().hex[:12]
            try:
                docker('run', '-d', '--name', name, '-p', '127.0.0.1::5432',
                       '-e', 'POSTGRES_PASSWORD=bench', '-v', f'{leg / "pg"}:/var/lib/postgresql/data', args.image,
                       '-c', 'fsync=on', '-c', 'synchronous_commit=on', '-c', 'full_page_writes=on', '-c', 'max_wal_size=256MB')
                deadline = time.monotonic() + 90
                while subprocess.run(['docker', 'exec', name, 'pg_isready', '-U', 'postgres'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                    if time.monotonic() > deadline:
                        raise RuntimeError('PostgreSQL readiness timed out')
                    time.sleep(1)
                port = docker('port', name, '5432/tcp', capture_output=True).stdout.strip().rsplit(':', 1)[1]
                out.setdefault('image_id', docker('inspect', name, '--format', '{{.Image}}', capture_output=True).stdout.strip())
                cmd = [sys.executable, str(Path(__file__).resolve()), '--worker', '--work-dir', str(leg),
                       '--target-bytes', str(args.target_bytes), '--record-bytes', str(args.record_bytes),
                       '--batch', str(args.batch), '--rounds', str(args.rounds),
                       '--dsn', f'postgresql://postgres:bench@127.0.0.1:{port}/postgres']
                if label == 'wrapped':
                    cmd.append('--shadow')
                subprocess.run(cmd, check=True)
                docker('restart', name)
                deadline = time.monotonic() + 90
                while subprocess.run(['docker', 'exec', name, 'pg_isready', '-U', 'postgres'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                    if time.monotonic() > deadline:
                        raise RuntimeError('PostgreSQL restart timed out')
                    time.sleep(1)
                subprocess.run(cmd + ['--verify'], check=True)
                result = json.loads((leg / 'result.json').read_text())
                result['verification'] = json.loads((leg / 'verification.json').read_text())
                out['results'][label] = result
            finally:
                subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        out['status'] = 'ok'
        (root / 'storage-results.md').write_text(render(out))
    except Exception as exc:
        out['status'] = 'error'
        out['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        (root / 'storage-results.json').write_text(json.dumps(out, indent=2))


if __name__ == '__main__':
    main()
