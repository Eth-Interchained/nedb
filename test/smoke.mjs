import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { NedbCore } from "nedb-engine";

function scene(title) { console.log(`\n🎬 ${title}`); }
function beat(message) { console.log(`   ✓ ${message}`); }
function parseJson(value, label = "JSON value") {
  assert.equal(typeof value, "string", `${label} should be a JSON string`);
  return JSON.parse(value);
}
function parseRows(rows) {
  assert.ok(Array.isArray(rows), "query() should return an array");
  return rows.map((row, index) => parseJson(row, `query row ${index}`));
}
function requireMethod(target, names) {
  for (const name of names) if (typeof target[name] === "function") return target[name].bind(target);
  throw new TypeError(`Expected one of these methods to exist: ${names.join(", ")}`);
}
function createIndex(db, coll, field, kind) { return requireMethod(db, ["createIndex", "create_index"])(coll, field, kind); }
function getAsOf(db, coll, id, seq) { return requireMethod(db, ["getAsOf", "get_as_of"])(coll, id, seq); }
function neighborsAsOf(db, from, rel, seq) { return requireMethod(db, ["neighborsAsOf", "neighbors_as_of"])(from, rel, seq); }
function durableStoryDb() {
  const dir = mkdtempSync(join(tmpdir(), "nedb-cinematic-"));
  return { dir, db: NedbCore.open(dir), cleanup() { rmSync(dir, { recursive: true, force: true }); } };
}

test("cinematic: NEDB seals a rideshare incident into a verifiable DAG", () => {
  scene("SCENE 1 — Incident #4471 enters the DAG");
  const db = new NedbCore();
  createIndex(db, "drivers", "status", "eq");
  createIndex(db, "drivers", "rating", "eq");
  const bob = parseJson(db.put("drivers", "bob", JSON.stringify({ name: "Bob", status: "active", rating: 4.9, city: "Orlando" })), "Bob put result");
  const dave = parseJson(db.put("drivers", "dave", JSON.stringify({ name: "Dave", status: "active", rating: 2.1, city: "Orlando" })), "Dave put result");
  const incident = parseJson(db.put("incidents", "ride_4471", JSON.stringify({ title: "Why was I assigned a 2.1-rated driver?", rider: "Carol", assigned_driver: "dave", status: "investigating", caused_by: [dave._hash] })), "incident put result");
  beat("Two drivers and one incident were written through real NEDB put()");
  beat(`Bob hash:      ${bob._hash.slice(0, 16)}…`);
  beat(`Dave hash:     ${dave._hash.slice(0, 16)}…`);
  beat(`Incident hash: ${incident._hash.slice(0, 16)}…`);
  assert.match(bob._hash, /^[a-f0-9]{64}$/i);
  assert.match(dave._hash, /^[a-f0-9]{64}$/i);
  assert.match(incident._hash, /^[a-f0-9]{64}$/i);
  assert.equal(db.verify(), true, "fresh DAG should verify");
  assert.match(db.head(), /^[a-f0-9]{64}$/i, "head must be a 64-char commitment hash");
  beat(`Merkle head sealed the scene: ${db.head().slice(0, 16)}…`);
});

test("cinematic: query finds the risky assignment without pretending", () => {
  scene("SCENE 2 — The audit query finds the bad assignment");
  const db = new NedbCore();
  createIndex(db, "drivers", "status", "eq");
  createIndex(db, "drivers", "rating", "eq");
  db.put("drivers", "bob", JSON.stringify({ name: "Bob", status: "active", rating: 4.9 }));
  db.put("drivers", "dave", JSON.stringify({ name: "Dave", status: "active", rating: 2.1 }));
  const rows = parseRows(db.query('FROM drivers WHERE status = "active" ORDER BY rating ASC'));
  assert.equal(rows.length, 2);
  assert.equal(rows[0].name, "Dave");
  assert.equal(rows[0].rating, 2.1);
  beat("NQL returned active drivers ordered by rating");
  beat("The lowest-rated active driver was Dave, exactly as the data says");
  assert.equal(db.verify(), true);
});

test("cinematic: time travel preserves the truth before correction", () => {
  scene("SCENE 3 — Dave improves, but the old truth remains provable");
  const db = new NedbCore();
  db.put("drivers", "dave", JSON.stringify({ name: "Dave", status: "active", rating: 2.1, note: "initial complaint state" }));
  const initialDave = parseJson(db.get("drivers", "dave"), "initial dave");
  const beforeTraining = BigInt(initialDave._seq);
  beat(`Snapshot captured at stored seq=${beforeTraining.toString()}`);
  db.put("drivers", "dave", JSON.stringify({ name: "Dave", status: "active", rating: 4.2, note: "after retraining and review" }));
  const current = parseJson(db.get("drivers", "dave"), "current dave");
  const past = parseJson(getAsOf(db, "drivers", "dave", beforeTraining), "past dave");
  assert.equal(current.rating, 4.2);
  assert.equal(current.note, "after retraining and review");
  assert.equal(past.rating, 2.1);
  assert.equal(past.note, "initial complaint state");
  beat("Current Dave is improved");
  beat("Past Dave is still exactly what the incident saw");
  assert.equal(db.verify(), true);
});

test("cinematic: durable NEDB wakes up with the same head", () => {
  scene("SCENE 4 — The database restarts and remembers");
  const story = durableStoryDb();
  try {
    story.db.put("incidents", "ride_4471", JSON.stringify({ title: "Why was I assigned a 2.1-rated driver?", assigned_driver: "dave", status: "sealed" }));
    const beforeHead = story.db.head();
    const beforeSeq = story.db.seq();
    story.db.flush?.();
    beat(`Before restart seq=${beforeSeq.toString()}`);
    beat(`Before restart head=${beforeHead.slice(0, 16)}…`);
    const reopened = NedbCore.open(story.dir);
    const recovered = parseJson(reopened.get("incidents", "ride_4471"), "recovered incident");
    assert.equal(recovered.assigned_driver, "dave");
    assert.equal(recovered.status, "sealed");
    assert.equal(reopened.verify(), true);
    assert.equal(reopened.head(), beforeHead, "durable reopen should preserve the same Merkle head");
    beat("Reopened DB recovered the incident");
    beat(`After restart head=${reopened.head().slice(0, 16)}…`);
  } finally { story.cleanup(); }
});

test("cinematic: graph edges can be time-traveled too", () => {
  scene("SCENE 5 — The causal graph shows who assigned what, and when");
  const db = new NedbCore();
  db.put("drivers", "dave", JSON.stringify({ name: "Dave", rating: 2.1 }));
  db.put("dispatchers", "alice", JSON.stringify({ name: "Alice" }));
  const beforeLink = db.seq() - 1n;
  db.link("dispatchers:alice", "assigned", "drivers:dave");
  const linkRow = parseRows(db.query('FROM __links__ WHERE _from = "dispatchers:alice" AND _rel = "assigned"'))[0];
  const afterLink = BigInt(linkRow._seq);
  const nowNeighbors = db.neighbors("dispatchers:alice", "assigned");
  const beforeNeighbors = neighborsAsOf(db, "dispatchers:alice", "assigned", beforeLink);
  const afterNeighbors = neighborsAsOf(db, "dispatchers:alice", "assigned", afterLink);
  assert.deepEqual(nowNeighbors, ["drivers:dave"]);
  assert.deepEqual(beforeNeighbors, []);
  assert.deepEqual(afterNeighbors, ["drivers:dave"]);
  beat("Current graph shows Alice assigned Dave");
  beat("Before the link sequence, that edge did not exist");
  beat("At and after the link sequence, the edge exists");
  assert.equal(db.verify(), true);
});
