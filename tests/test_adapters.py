"""Tests for the SQL adapter, Redis compat layer, and auto-indexer."""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

from nedb import NEDB, AutoIndexDB
from nedb.sql import sql_exec, sql_to_nql, SQLUnsupportedError
from nedb.redis_compat import RedisCompat, RedisUnsupportedError

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ok  {name}")
    else:    FAIL += 1; print(f"  FAIL {name}")

def fresh():
    db = NEDB()
    db.create_index("users", "status", "eq")
    db.create_index("users", "age",    "ordered")
    db.create_index("users", "bio",    "search")
    db.put("users", "alice", {"name": "Alice", "age": 31, "status": "active",   "bio": "rust db"})
    db.put("users", "bob",   {"name": "Bob",   "age": 24, "status": "active",   "bio": "python data"})
    db.put("users", "carol", {"name": "Carol", "age": 41, "status": "inactive", "bio": "rust systems"})
    return db

# ─── SQL adapter ──────────────────────────────────────────────────────────────
print("\n── SQL adapter ──")

def test_sql_to_nql_simple():
    nql = sql_to_nql("SELECT * FROM users WHERE status = 'active' ORDER BY age DESC LIMIT 5")
    check("sql_to_nql basic", "FROM users" in nql and 'status = "active"' in nql and "ORDER BY age DESC" in nql and "LIMIT 5" in nql)

def test_sql_select():
    db = fresh()
    rows = sql_exec(db, "SELECT * FROM users WHERE status = 'active' ORDER BY age ASC")
    check("SELECT WHERE ORDER BY", len(rows) == 2 and rows[0]["name"] == "Bob")

def test_sql_select_like():
    db = fresh()
    rows = sql_exec(db, "SELECT * FROM users WHERE bio LIKE '%rust%'")
    names = sorted(r["name"] for r in rows)
    check("SELECT LIKE → SEARCH", names == ["Alice", "Carol"])

def test_sql_select_limit():
    db = fresh()
    rows = sql_exec(db, "SELECT * FROM users LIMIT 1")
    check("SELECT LIMIT", len(rows) == 1)

def test_sql_insert():
    db = fresh()
    sql_exec(db, "INSERT INTO users (id, name, age, status, bio) VALUES ('dave', 'Dave', 28, 'active', 'go dev')")
    check("INSERT", db.get("users", "dave")["name"] == "Dave")

def test_sql_update():
    db = fresh()
    sql_exec(db, "UPDATE users SET age = 99 WHERE id = 'alice'")
    check("UPDATE", db.get("users", "alice")["age"] == 99)
    check("UPDATE preserves other fields", db.get("users", "alice")["name"] == "Alice")

def test_sql_delete():
    db = fresh()
    sql_exec(db, "DELETE FROM users WHERE id = 'alice'")
    check("DELETE", db.get("users", "alice") is None)

def test_sql_or_raises():
    from nedb.sql import SQLError as _SE
    try:
        sql_to_nql("SELECT * FROM users WHERE status = 'active' OR age > 30")
        check("OR raises error", False)
    except (SQLUnsupportedError, _SE):
        check("OR raises error", True)

def test_sql_as_of():
    db = fresh()
    snap = db.seq
    db.put("users", "alice", {"name": "Alice", "age": 55, "status": "active"})
    rows = sql_exec(db, f"SELECT * FROM users AS OF {snap} WHERE status = 'active'")
    check("SELECT AS OF (time-travel)", all(r["age"] != 55 for r in rows if r["_id"] == "alice"))

for fn in [test_sql_to_nql_simple, test_sql_select, test_sql_select_like,
           test_sql_select_limit, test_sql_insert, test_sql_update,
           test_sql_delete, test_sql_or_raises, test_sql_as_of]:
    fn()

# ─── Redis compat ─────────────────────────────────────────────────────────────
print("\n── Redis adapter ──")

def test_redis_ping():
    r = RedisCompat(fresh()); check("PING", r.execute("PING") == "PONG")

def test_redis_set_get():
    r = RedisCompat(fresh())
    r.execute("SET", "k", "hello")
    check("SET/GET", r.execute("GET", "k") == "hello")

def test_redis_del():
    r = RedisCompat(fresh())
    r.execute("SET", "k", "v")
    r.execute("DEL", "k")
    check("DEL", r.execute("GET", "k") is None)

def test_redis_exists():
    r = RedisCompat(fresh())
    r.execute("SET", "k", "v")
    check("EXISTS yes", r.execute("EXISTS", "k") == 1)
    check("EXISTS no",  r.execute("EXISTS", "missing") == 0)

def test_redis_incr():
    r = RedisCompat(fresh())
    r.execute("SET", "counter", "0")
    check("INCR", r.execute("INCR", "counter") == 1)
    check("INCRBY", r.execute("INCRBY", "counter", 9) == 10)
    check("DECR",  r.execute("DECR", "counter") == 9)

def test_redis_setnx():
    r = RedisCompat(fresh())
    check("SETNX new", r.execute("SETNX", "k", "v") == 1)
    check("SETNX dup", r.execute("SETNX", "k", "v2") == 0)
    check("SETNX kept", r.execute("GET", "k") == "v")

def test_redis_mset_mget():
    r = RedisCompat(fresh())
    r.execute("MSET", "a", "1", "b", "2")
    check("MGET", r.execute("MGET", "a", "b", "missing") == ["1", "2", None])

def test_redis_hash():
    r = RedisCompat(fresh())
    r.execute("HSET", "user:1", "name", "Ada", "age", "31")
    check("HGET", r.execute("HGET", "user:1", "name") == "Ada")
    check("HGETALL", r.execute("HGETALL", "user:1") == {"name": "Ada", "age": "31"})
    check("HLEN", r.execute("HLEN", "user:1") == 2)
    check("HEXISTS", r.execute("HEXISTS", "user:1", "name") == 1)
    r.execute("HDEL", "user:1", "age")
    check("HDEL", r.execute("HGET", "user:1", "age") is None)
    check("HINCRBY", r.execute("HINCRBY", "user:1", "score", 5) == 5)

def test_redis_set():
    r = RedisCompat(fresh())
    r.execute("SADD", "tags", "python", "rust", "go")
    check("SMEMBERS", r.execute("SMEMBERS", "tags") == {"python", "rust", "go"})
    check("SISMEMBER yes", r.execute("SISMEMBER", "tags", "rust") == 1)
    check("SISMEMBER no",  r.execute("SISMEMBER", "tags", "java") == 0)
    check("SCARD", r.execute("SCARD", "tags") == 3)
    r.execute("SREM", "tags", "go")
    check("SREM", r.execute("SCARD", "tags") == 2)

def test_redis_list():
    r = RedisCompat(fresh())
    r.execute("RPUSH", "q", "a", "b", "c")
    check("LRANGE", r.execute("LRANGE", "q", 0, -1) == ["a", "b", "c"])
    check("LLEN", r.execute("LLEN", "q") == 3)
    check("LINDEX", r.execute("LINDEX", "q", 1) == "b")
    check("LPOP", r.execute("LPOP", "q") == "a")
    check("RPOP", r.execute("RPOP", "q") == "c")

def test_redis_unsupported():
    r = RedisCompat(fresh())
    for cmd in ("EXPIRE", "TTL", "SUBSCRIBE", "PUBLISH", "MULTI", "EXEC"):
        try:
            r.execute(cmd, "k", "60")
            check(f"{cmd} raises", False)
        except RedisUnsupportedError:
            check(f"{cmd} → UNSUPPORTED", True)

for fn in [test_redis_ping, test_redis_set_get, test_redis_del, test_redis_exists,
           test_redis_incr, test_redis_setnx, test_redis_mset_mget, test_redis_hash,
           test_redis_set, test_redis_list, test_redis_unsupported]:
    fn()

# ─── AutoIndexDB ──────────────────────────────────────────────────────────────
print("\n── AutoIndexDB ──")

def test_autoindex():
    db = AutoIndexDB(NEDB(), threshold=3, verbose=False)
    db.put("items", "1", {"name": "A", "status": "active"})
    db.put("items", "2", {"name": "B", "status": "archived"})
    # Query 3 times — threshold reached on the 3rd
    for _ in range(3):
        db.query('FROM items WHERE status = "active"')
    check("autoindex created eq index", ("items", "status", "eq") in db._created)
    rows = db.query('FROM items WHERE status = "active"')
    check("autoindex query still correct", len(rows) == 1 and rows[0]["name"] == "A")
    s = db.suggest()
    check("suggest returns string list", isinstance(s, list))
    a = db.analyze()
    check("analyze returns tallies", "tallies" in a and "indexes_created" in a)

test_autoindex()

# ─── Summary ──────────────────────────────────────────────────────────────────
print(f"\nAdapters: {PASS} passed, {FAIL} failed {'✅' if not FAIL else '❌'}")
sys.exit(1 if FAIL else 0)
