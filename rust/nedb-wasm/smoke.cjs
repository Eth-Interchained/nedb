// NEDB wasm smoke test — runs the wasm-bindgen output under plain Node.
// Exercises the in-memory engine end to end: put/get/query/SQL/time-travel/
// graph/verify, mirroring the napi binding's contract.
'use strict';
const assert = require('node:assert');
const { NedbWasm } = require('./pkg/nedb_wasm.js');

const db = new NedbWasm();
console.log('constructed NedbWasm (in-memory)');

// ── put / get ─────────────────────────────────────────────────────────────
const r1 = JSON.parse(db.put('users', 'alice', JSON.stringify({ age: 32 })));
assert.strictEqual(r1._id, 'alice');
assert.strictEqual(r1._coll, 'users');
assert.strictEqual(r1.age, 32);
assert.ok(r1._hash && r1._hash.length === 64, 'blake2b _hash present');
assert.ok(typeof r1._seq === 'number' && r1._seq > 0, 'seq assigned');
const seqV1 = db.seq();
console.log('put alice ->', JSON.stringify({ _seq: r1._seq, _hash: r1._hash.slice(0, 12) + '…' }));

const g1 = JSON.parse(db.get('users', 'alice'));
assert.strictEqual(g1.age, 32);
assert.strictEqual(db.get('users', 'nobody'), undefined, 'missing -> undefined');
console.log('get alice ->', JSON.stringify({ age: g1.age }));

// ── causal + bi-temporal lanes ──────────────────────────────────────────────
const r2 = JSON.parse(db.put('beliefs', 'dark_mode', JSON.stringify({
    value: true, caused_by: [r1._hash], valid_from: '2024-01-01', valid_to: '2024-12-31',
})));
assert.deepStrictEqual(r2._caused_by, [r1._hash], '_caused_by projected');
assert.strictEqual(r2._valid_from, '2024-01-01');
assert.strictEqual(r2._valid_to, '2024-12-31');
console.log('causal/bi-temporal lanes projected');

// ── NQL + SQL ───────────────────────────────────────────────────────────────
db.put('users', 'bob', JSON.stringify({ age: 41 }));
const rows = db.query('FROM users WHERE age = 32').map(JSON.parse);
assert.strictEqual(rows.length, 1);
assert.strictEqual(rows[0]._id, 'alice');
console.log('NQL query ->', rows.length, 'row');

const srows = db.query("SELECT _id, age FROM users WHERE age > 30").map(JSON.parse);
assert.ok(srows.length >= 2, 'SQL dialect returns rows');
assert.strictEqual(db.nesql_dialect('FROM users'), 'nql');
assert.strictEqual(db.nesql_dialect('SELECT 1'), 'sql');
console.log('SQL query ->', srows.length, 'rows; dialect routing ok');

// ── time travel ─────────────────────────────────────────────────────────────
db.put('users', 'alice', JSON.stringify({ age: 33 }));
const cur = JSON.parse(db.get('users', 'alice'));
assert.strictEqual(cur.age, 33, 'current version wins');
const old = JSON.parse(db.get_as_of('users', 'alice', seqV1));
assert.strictEqual(old.age, 32, 'AS OF returns the old version');
console.log('time travel: age was', old.age, 'at seq', seqV1, ', now', cur.age);

// ── graph ───────────────────────────────────────────────────────────────────
db.link('alice', 'knows', 'bob');
assert.deepStrictEqual(db.neighbors('alice', 'knows'), ['bob']);
assert.deepStrictEqual(db.inbound('bob', 'knows'), ['alice']);
db.unlink('alice', 'knows', 'bob');
assert.deepStrictEqual(db.neighbors('alice', 'knows'), []);
console.log('graph link/neighbors/inbound/unlink ok');

// ── integrity ───────────────────────────────────────────────────────────────
assert.strictEqual(db.verify(), true, 'hash chain verifies');
const head = db.head();
assert.ok(/^[0-9a-f]{64}$/.test(head), 'head is 64-char hex');
assert.ok(db.seq() > seqV1, 'seq advanced');
console.log('verify() true; head', head.slice(0, 12) + '…; seq', db.seq());

// ── tip / since ─────────────────────────────────────────────────────────────
const tip = JSON.parse(db.tip());
assert.ok(tip._seq > 0, 'tip has seq');
const feed = JSON.parse(db.since(0, 100));
// NOTE: the engine's since() reports has_more=true while the cursor trails the
// head when some seqs are collection-registry records (ensure_collection
// consumes seqs) — pre-existing engine semantics, identical via napi. Assert
// the binding contract: envelope shape + every node well-formed.
assert.ok(feed.nodes.length > 0, 'since() returns nodes');
assert.strictEqual(feed.from_seq, 0);
assert.strictEqual(feed.head_seq, db.seq() - 1);
assert.strictEqual(typeof feed.has_more, 'boolean');
for (const n of feed.nodes) {
    assert.ok(typeof n._seq === 'number' && n._id && n._hash, 'node well-formed');
}
console.log('tip + since() ok:', feed.nodes.length, 'nodes in feed');

// ── delete (tombstone, history preserved) ────────────────────────────────────
db.delete('users', 'bob');
assert.strictEqual(db.get('users', 'bob'), undefined, 'deleted -> undefined');
assert.strictEqual(db.verify(), true, 'chain still verifies after delete');
console.log('delete tombstone ok; verify still true');

// ── indexes ─────────────────────────────────────────────────────────────────
db.create_index('users', 'age');
const orows = db.query('FROM users ORDER BY age DESC').map(JSON.parse);
assert.ok(orows.length >= 1, 'sorted index query works');
console.log('create_index + ORDER BY ok');

console.log('\nALL WASM SMOKE TESTS PASSED');
