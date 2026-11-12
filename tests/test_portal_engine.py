# SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
# SPDX-License-Identifier: BUSL-1.1
"""Portal-aware engine open (step 6b): checkpoint seals shards, open
serves sealed state through portals, the tail stays in RAM.

Covers the locked design (docs/portals.md): the migration path, tail
writes across reopens, re-sealing from a portal session, legacy/portal
answer parity, deletion parity (tombstones + last_seq + CAS), the open
decision table (corrupt = fail loud, registry lost = full AOF replay,
torn publish = full AOF replay, stale snapshot never wins), capability
errors, and the env-var switch.
"""
import json
import contextlib
import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from nedb import NEDB  # noqa: E402
from nedb.portal_store import (PortalCapabilityError, PortalError,  # noqa: E402
                               PortalStoreView)
from nedb.log import canon  # noqa: E402


@contextlib.contextmanager
def _raises(exc_type):
    """pytest.raises without the pytest dependency — CI runs the suites
    as plain scripts in an environment with no pytest installed."""
    try:
        yield
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__}")


def _seed(path, n=40):
    """A db with live docs in two collections and some deletions."""
    db = NEDB(path, portal_reads=True)
    for i in range(n):
        db.put("items", f"k{i}", {"n": i, "grp": "even" if i % 2 == 0 else "odd"})
    for i in range(0, n, 7):
        db.delete("items", f"k{i}")
    db.put("meta", "name", {"v": "vision-shape"})
    db.link("items:k1", "rel", "items:k2")
    return db


def _live(db, n=40):
    return {f"k{i}": db.get("items", f"k{i}") for i in range(n)}


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_portal_checkpoint_then_portal_open(tmp_path):
    p = str(tmp_path / "db")
    db = _seed(p)
    head = db.checkpoint()
    assert head == db.head
    assert os.path.exists(os.path.join(p, "portals.json"))
    assert os.path.exists(os.path.join(p, "portal-state.json"))
    assert not os.path.exists(os.path.join(p, "snapshot.json"))
    expect = _live(db)
    db.close()

    db2 = NEDB(p, portal_reads=True)
    assert isinstance(db2.store, PortalStoreView)
    assert _live(db2) == expect
    assert db2.get("items", "k0") is None          # sealed deletion
    assert db2.get("meta", "name")["v"] == "vision-shape"
    assert db2.neighbors("items:k1", "rel") == ["items:k2"]
    assert db2.verify()
    assert sorted(db2.collections()) == ["items", "meta"]
    db2.close()


def test_tail_writes_survive_reopen_and_reseal(tmp_path):
    p = str(tmp_path / "db")
    db = _seed(p)
    db.checkpoint()
    db.close()

    db = NEDB(p, portal_reads=True)
    db.put("items", "new1", {"n": 1000})            # brand-new key (tail)
    db.put("items", "k1", {"n": -1, "grp": "odd"})  # overwrite a sealed doc
    db.delete("items", "k2")                        # delete a sealed doc
    db.close()

    db = NEDB(p, portal_reads=True)                 # tail folds from AOF
    assert db.get("items", "new1")["n"] == 1000
    assert db.get("items", "k1")["n"] == -1
    assert db.get("items", "k2") is None
    db.checkpoint()                                 # re-seal from a portal session
    db.close()

    db = NEDB(p, portal_reads=True)
    assert isinstance(db.store, PortalStoreView)
    assert db.get("items", "new1")["n"] == 1000
    assert db.get("items", "k1")["n"] == -1
    assert db.get("items", "k2") is None
    assert db.verify()
    db.close()


def test_legacy_and_portal_answers_are_byte_identical(tmp_path):
    p = str(tmp_path / "db")
    db = _seed(p)
    db.checkpoint()
    db.close()

    legacy = NEDB(p)                                # legacy open, same files
    legacy_snap = {k: canon(v) if v is not None else None
                   for k, v in legacy.store.snapshot().items()}
    legacy.close()

    portal = NEDB(p, portal_reads=True)
    portal_snap = {k: canon(v) if v is not None else None
                   for k, v in portal.store.snapshot().items()}
    portal.close()
    assert portal_snap == legacy_snap


def test_deletion_parity_last_seq_and_cas(tmp_path):
    p = str(tmp_path / "db")
    db = _seed(p)
    del_seq = db.last_seq("items", "k0")            # k0 was deleted in _seed
    live_seq = db.last_seq("items", "k1")
    db.checkpoint()
    db.close()

    db = NEDB(p, portal_reads=True)
    assert db.last_seq("items", "k0") == del_seq    # tombstone seq from sidecar
    assert db.last_seq("items", "k1") == live_seq   # sealed doc's _seq
    # CAS against the sealed deletion: if_seq == delete seq must pass.
    out = db.tx([{"op": "put", "coll": "items", "id": "k0",
                  "doc": {"n": 0, "grp": "even"}, "if_seq": del_seq}])
    assert out["count"] == 1
    assert db.get("items", "k0")["n"] == 0
    db.close()


def test_query_streams_and_scans_in_portal_mode(tmp_path):
    p = str(tmp_path / "db")
    db = _seed(p, n=30)
    db.checkpoint()
    db.close()

    db = NEDB(p, portal_reads=True)
    rows = db.query('FROM items WHERE grp = "even"')
    expect = sorted(i for i in range(30)
                    if i % 2 == 0 and i % 7 != 0)
    assert sorted(r["n"] for r in rows) == expect
    keys = db.store.keys("items:")
    assert len(keys) == len([i for i in range(30) if i % 7 != 0])
    db.close()


def test_as_of_contract(tmp_path):
    p = str(tmp_path / "db")
    db = NEDB(p, portal_reads=True)
    db.put("c", "a", {"v": 1})
    db.checkpoint()
    wm = db._portal_watermark
    db.close()

    db = NEDB(p, portal_reads=True)
    assert db.get("c", "a", as_of=wm)["v"] == 1     # at the watermark: fine
    with _raises(PortalCapabilityError):
        db.get("c", "a", as_of=wm - 1)              # pre-checkpoint: refused
    with _raises(PortalCapabilityError):
        db.create_index("c", "v")                   # backfill would materialize
    db.close()


def test_windowed_log_keeps_absolute_seqs_and_chains_appends(tmp_path):
    p = str(tmp_path / "db")
    db = _seed(p)
    db.checkpoint()
    seq_at_seal = db.seq
    db.close()

    db = NEDB(p, portal_reads=True)
    assert db.log.windowed
    assert db.seq == seq_at_seal                    # absolute, not tail-local
    doc = db.put("items", "after", {"n": -7})
    assert doc["_seq"] == seq_at_seal + 1           # appends continue the chain
    assert db.verify()                               # streaming verify, full chain
    db.close()

    db = NEDB(p)                                     # legacy full open agrees
    assert db.get("items", "after")["n"] == -7
    assert db.verify()
    db.close()


def test_idem_retry_of_pre_window_op_dedups(tmp_path):
    p = str(tmp_path / "db")
    db = NEDB(p, portal_reads=True)
    db.put("c", "a", {"v": 1}, idem="write-1")
    seq_before = db.seq
    db.checkpoint()
    db.close()

    db = NEDB(p, portal_reads=True)
    out = db.put("c", "a", {"v": 999}, idem="write-1")   # retry: deduped
    assert out["v"] == 1                                  # original stands
    assert db.seq == seq_before + 1                       # only the checkpoint op
                                                          # was appended since
    db.put("c", "b", {"v": 2}, idem="write-2")            # fresh idem works
    assert db.get("c", "b")["v"] == 2
    db.close()


def test_windowed_capability_errors(tmp_path):
    p = str(tmp_path / "db")
    db = _seed(p)
    db.checkpoint()
    db.close()

    db = NEDB(p, portal_reads=True)
    with _raises(PortalCapabilityError):
        db.since(0)                                  # below the window
    assert db.since(db._portal_watermark) == []      # at the window edge: fine
    with _raises(PortalCapabilityError):
        db.rebuild()                                 # full replay by definition
    with _raises(PortalCapabilityError):
        db.execute({"from": "items", "trace": True})
    db.close()


def test_streaming_verify_detects_pre_window_tamper(tmp_path):
    p = str(tmp_path / "db")
    db = _seed(p)
    db.checkpoint()
    db.close()

    aof = os.path.join(p, "log.aof")
    lines = open(aof).read().splitlines()
    for i, ln in enumerate(lines):
        if '"id": "k5"' in ln:                       # tamper a sealed-era op
            lines[i] = ln.replace('"n": 5', '"n": 555')
            assert '"n": 555' in lines[i]
            break
    else:
        raise AssertionError("seed op for k5 not found in AOF")
    with open(aof, "w") as fh:
        fh.write("\n".join(lines) + "\n")

    db = NEDB(p, portal_reads=True)
    assert db.verify() is False                     # streaming verify catches it
    db.close()


def test_env_var_switch_and_flag_override(tmp_path, monkeypatch):
    p = str(tmp_path / "db")
    db = _seed(p)
    db.checkpoint()
    db.close()

    monkeypatch.setenv("NEDB_PORTAL_READS", "1")
    db = NEDB(p)                                    # env alone turns it on
    assert isinstance(db.store, PortalStoreView)
    db.close()
    db = NEDB(p, portal_reads=False)                # explicit flag wins
    assert not isinstance(db.store, PortalStoreView)
    db.close()


# ---------------------------------------------------------------------------
# The open decision table
# ---------------------------------------------------------------------------

def test_corrupt_registry_fails_loud(tmp_path):
    p = str(tmp_path / "db")
    db = _seed(p)
    db.checkpoint()
    db.close()
    with open(os.path.join(p, "portals.json"), "w") as fh:
        fh.write("{ not json")
    with _raises(PortalError):
        NEDB(p, portal_reads=True)


def test_registry_lost_recovers_by_full_replay(tmp_path):
    p = str(tmp_path / "db")
    db = _seed(p)
    expect = _live(db)
    db.checkpoint()
    db.close()
    os.remove(os.path.join(p, "portals.json"))      # registry lost post-authority
    db = NEDB(p, portal_reads=True)
    assert not isinstance(db.store, PortalStoreView)  # replayed, not portaled
    assert _live(db) == expect
    assert db.verify()
    db.close()


def test_torn_publish_recovers_by_full_replay(tmp_path):
    p = str(tmp_path / "db")
    db = _seed(p)
    expect = _live(db)
    db.checkpoint()
    db.close()
    # The state's commit tie no longer matches the registry's shard
    # set: the checkpoint was torn before its registry publish (or the
    # registry is from another generation).
    sp = os.path.join(p, "portal-state.json")
    state = json.loads(open(sp).read())
    state["shards"] = state["shards"] + ["00" * 32]
    with open(sp, "w") as fh:
        json.dump(state, fh)
    db = NEDB(p, portal_reads=True)
    assert not isinstance(db.store, PortalStoreView)
    assert _live(db) == expect
    db.close()


def test_stale_snapshot_never_wins_over_portals(tmp_path):
    p = str(tmp_path / "db")
    db = NEDB(p)                                    # legacy session
    db.put("c", "old", {"v": 1})
    db.checkpoint()                                 # legacy snapshot S1
    stale = open(os.path.join(p, "snapshot.json"), "rb").read()
    db.close()

    db = NEDB(p, portal_reads=True)
    db.put("c", "new", {"v": 2})
    db.checkpoint()                                 # portal seal retires S1
    db.close()
    with open(os.path.join(p, "snapshot.json"), "wb") as fh:
        fh.write(stale)                             # crash-before-retire debris

    db = NEDB(p, portal_reads=True)
    assert isinstance(db.store, PortalStoreView)
    assert db.get("c", "new")["v"] == 2             # portal state, not S1
    assert db.get("c", "old")["v"] == 1
    db.close()


def test_checkpoint_crash_debris_is_ignored(tmp_path):
    p = str(tmp_path / "db")
    db = _seed(p)
    expect = _live(db)
    db.close()
    # Debris of a checkpoint that died mid-shard-write on a fresh store:
    # temp dir + unregistered segment files, no registry, no state.
    os.makedirs(os.path.join(p, ".portal-tmp"), exist_ok=True)
    with open(os.path.join(p, "seg-000000.dat"), "wb") as fh:
        fh.write(b"\x04\x00\x00\x00junk")
    db = NEDB(p, portal_reads=True)
    assert _live(db) == expect
    # ...and a portal checkpoint still lands cleanly afterwards.
    db.checkpoint()
    db.close()
    db = NEDB(p, portal_reads=True)
    assert isinstance(db.store, PortalStoreView)
    assert _live(db) == expect
    db.close()


if __name__ == "__main__":
    # CI runs the Python suites as plain scripts (no pytest). Execute each
    # test_* function with a fresh temp dir standing in for pytest's
    # tmp_path fixture (and a minimal setenv shim for monkeypatch), so
    # this suite really runs there — same convention as test_portals.py.
    import inspect
    import pathlib
    import tempfile
    import traceback

    class _MP:
        def __init__(self):
            self._saved = {}

        def setenv(self, k, v):
            self._saved.setdefault(k, os.environ.get(k))
            os.environ[k] = v

        def restore(self):
            for k, v in self._saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    _fns = [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]
    _failed = 0
    for _name, _fn in _fns:
        with tempfile.TemporaryDirectory() as _td:
            _mp = _MP()
            try:
                _params = inspect.signature(_fn).parameters
                _kwargs = {}
                if "tmp_path" in _params:
                    _kwargs["tmp_path"] = pathlib.Path(_td)
                if "monkeypatch" in _params:
                    _kwargs["monkeypatch"] = _mp
                _fn(**_kwargs)
                print("PASS", _name)
            except Exception:
                _failed += 1
                print("FAIL", _name)
                traceback.print_exc()
            finally:
                _mp.restore()
    raise SystemExit(1 if _failed else 0)
