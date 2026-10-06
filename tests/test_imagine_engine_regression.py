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
BIN = ROOT / "rust/target/release/nedbd"
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
            print("Imagine engine regressions: CTE, EXPLAIN, prepared CTE, identities and NULL counts passed")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

if __name__ == "__main__":
    main()
