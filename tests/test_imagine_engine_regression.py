#!/usr/bin/env python3
"""Live regressions for Imagine's NEDB gate: CTEs and identity counts.
Missing drivers or binaries fail rather than silently skipping CI coverage.
"""
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
import json
from pathlib import Path

import psycopg2
import psycopg

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(os.environ.get("NEDBD_BIN", str(ROOT / "rust/target/release/nedbd")))
assert BIN.is_file(), f"missing daemon: {BIN}"

def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

def main():
    hp, pp = port(), port()
    with tempfile.TemporaryDirectory(prefix="nedb-imagine-regression-") as data:
        proc = subprocess.Popen([str(BIN), "--data", data, "--port", str(hp),
                                 "--pg-port", str(pp)], stdout=subprocess.DEVNULL)
        try:
            for _ in range(80):
                if proc.poll() is not None:
                    raise RuntimeError("daemon exited during startup")
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{hp}/health", timeout=1).close()
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("daemon startup timed out")
            req = urllib.request.Request(f"http://127.0.0.1:{hp}/v1/databases",
                data=json.dumps({"name": "shop"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=5).close()
            dsn = f"host=127.0.0.1 port={pp} dbname=shop user=nedb connect_timeout=5"
            with psycopg2.connect(dsn) as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO orders (id, status) VALUES (1,'paid'),(2,'pending'),(3,'paid')")
                    def rows(sql):
                        cur.execute(sql)
                        return cur.fetchall()
                    cte = "WITH paid AS (SELECT * FROM orders WHERE status='paid') SELECT count(*) FROM paid"
                    assert rows(cte) == [(2,)]
                    assert rows("EXPLAIN " + cte), "EXPLAIN must return a plan"
                    assert rows("SELECT count(*) FROM orders WHERE status='paid'") == rows("SELECT count(id) FROM orders WHERE status='paid'") == [(2,)]
                    assert rows("SELECT id FROM orders ORDER BY id") == [(1,), (2,), (3,)]
                    cur.execute("INSERT INTO generated (status) VALUES ('paid'),('paid')")
                    assert rows("SELECT count(*), count(id) FROM generated") == [(2, 2)]
                    cur.execute("INSERT INTO orders (id, status) VALUES (NULL,'paid')")
                    assert rows("SELECT count(*), count(id) FROM orders") == [(4, 3)]
                    assert rows("SELECT count(id) FROM orders WHERE status='absent'") == [(0,)]
                    cur.execute("INSERT INTO hosts (id, region, hostname) VALUES (1,'east','a'),(2,'east','b'),(3,'west','c'),(4,NULL,'d')")
                    assert rows("SELECT region, count(1) FROM hosts GROUP BY 1 ORDER BY region NULLS LAST") == [('east', 2), ('west', 1), (None, 1)]
                    assert rows("SELECT hostname, count(1) FROM hosts GROUP BY 1 ORDER BY hostname") == [('a', 1), ('b', 1), ('c', 1), ('d', 1)]
                    assert rows("SELECT region, hostname, count(*) FROM hosts GROUP BY 1,2 ORDER BY hostname") == [('east', 'a', 1), ('east', 'b', 1), ('west', 'c', 1), (None, 'd', 1)]
                    cur.execute("INSERT INTO samples (id, taken_at) VALUES (1,'2025-12-31 23:59:59'),(2,'2026-03-01 00:00:00'),(3,'2026-03-31 23:59:59'),(4,'2026-04-01 00:00:00'),(5,NULL)")
                    assert rows("SELECT id FROM samples WHERE EXTRACT(MONTH FROM taken_at)=3 AND EXTRACT(YEAR FROM taken_at)=2026 ORDER BY id") == [(2,), (3,)]
                    assert rows("SELECT count(*) FROM samples WHERE EXTRACT(YEAR FROM taken_at)=2025") == [(1,)]
                    assert rows("SELECT EXTRACT(YEAR FROM taken_at) FROM samples WHERE id=5") == [(None,)]
                    assert rows("EXPLAIN SELECT id FROM samples WHERE EXTRACT(YEAR FROM taken_at)=2026")
                    for bad in ("SELECT region FROM hosts GROUP BY 0",
                                "SELECT region FROM hosts GROUP BY 2",
                                "SELECT count(*) FROM hosts GROUP BY 1",
                                "SELECT EXTRACT(YEAR FROM '2026-02-30')",
                                "SELECT EXTRACT(nonsense FROM '2026-01-01')"):
                        try:
                            rows(bad)
                        except psycopg2.Error:
                            pass
                        else:
                            raise AssertionError(f"invalid query accepted: {bad}")
                    try:
                        rows("WITH RECURSIVE x AS (SELECT 1) SELECT * FROM x")
                    except psycopg2.Error:
                        pass
                    else:
                        raise AssertionError("recursive CTE unexpectedly accepted")
            with psycopg.connect(dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("WITH paid AS (SELECT * FROM orders WHERE status=%s) SELECT count(*) FROM paid",
                                ("paid",), prepare=True)
                    assert cur.fetchall() == [(3,)]
                    cur.execute("SELECT id FROM samples WHERE EXTRACT(YEAR FROM taken_at)=%s ORDER BY id", (2026,), prepare=True)
                    assert cur.fetchall() == [(2,), (3,), (4,)]
                    cur.execute("SELECT region, count(*) FROM hosts GROUP BY 1 ORDER BY region NULLS LAST", prepare=True)
                    assert cur.fetchall() == [('east', 2), ('west', 1), (None, 1)]
            print("Imagine engine regressions: CTEs, identities, GROUP BY ordinals, EXTRACT, NULLs and prepared queries passed")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

if __name__ == "__main__":
    main()
