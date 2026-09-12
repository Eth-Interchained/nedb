#!/usr/bin/env node
// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

// node-postgres (`pg`) against a live nedbd.
//
// Why a Node leg exists at all: the engine SHIPS to npm, and every Node
// consumer in this ecosystem — the Studio's api-server, salon-platform,
// nedb-links — reaches a database from Node. Proving the wire works from
// Python and asserting it therefore works from Node is exactly the kind of
// inference this repo does not accept.
//
// `pg` matters specifically because it is the third parameter-passing style:
// psycopg2 interpolates client-side, asyncpg declares types and refuses a
// mismatch, and `pg` sends everything as text with UNSPECIFIED type OIDs (0)
// and lets the server decide. A server that only works when the client states
// a type would pass the first two and fail here.
//
// Driven by tests/pgwire_node.py, which owns the fixture. This file reads its
// connection details from argv and reports one JSON line per assertion, so the
// Python side counts Node's checks the same way it counts everyone else's.

const assertions = [];
function check(name, cond, detail) {
  assertions.push({ name, ok: !!cond, detail: detail === undefined ? "" : String(detail) });
}
function eq(name, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  check(name, g === w, g === w ? "" : `got ${g}, want ${w}`);
}

async function refused(Client, conf, name, sql, needle) {
  // A fresh client per refusal: `pg` marks a connection unusable after a
  // server error, so reusing one would measure the connection, not the engine.
  const c = new Client(conf);
  await c.connect();
  try {
    await c.query(sql);
    check(name, false, "it answered instead of refusing");
  } catch (e) {
    const msg = String(e.message || e);
    check(name, msg.toLowerCase().includes(needle.toLowerCase()), msg.slice(0, 140));
  } finally {
    try { await c.end(); } catch { /* already closed by the error */ }
  }
}

async function main() {
  const [port, database] = [Number(process.argv[2]), process.argv[3]];
  let pg;
  try {
    pg = require("pg");
  } catch {
    process.stdout.write(JSON.stringify({ skip: "the 'pg' package is not installed" }) + "\n");
    return;
  }
  const conf = { host: "127.0.0.1", port, database, user: "nedb" };
  const client = new pg.Client(conf);
  await client.connect();

  try {
    // ── the basics ────────────────────────────────────────────────────────
    let r = await client.query("SELECT _id, status, total FROM orders ORDER BY total");
    eq("rows come back in order", r.rows.map((x) => x._id), ["2", "1", "3"]);
    check("the row description carries field names",
      r.fields.map((f) => f.name).includes("status"),
      r.fields.map((f) => f.name).join(","));

    // ── parameters, sent with UNSPECIFIED type OIDs ────────────────────────
    r = await client.query(
      "SELECT _id FROM orders WHERE status = $1 AND total > $2", ["paid", 100]);
    eq("text + number parameters (pg declares neither type)",
      r.rows.map((x) => x._id).sort(), ["1", "3"]);

    r = await client.query("SELECT count(*) AS n FROM orders WHERE total > $1", [50]);
    eq("a number parameter in a count", Number(r.rows[0].n), 2);

    // ── the qualified predicate every ORM writes ──────────────────────────
    r = await client.query(
      "SELECT orders._id FROM orders WHERE orders.status = $1", ["open"]);
    eq("a QUALIFIED column in WHERE finds its field", r.rows.map((x) => x._id), ["2"]);
    r = await client.query("SELECT o.status FROM orders o WHERE o.total = $1", [40]);
    eq("a table ALIAS works as a qualifier", r.rows.map((x) => x.status), ["open"]);

    // ── the catalogue, which is what a schema browser reads ───────────────
    r = await client.query(
      "SELECT relname FROM pg_catalog.pg_class WHERE relkind = 'r' ORDER BY 1");
    eq("relations are listed", r.rows.map((x) => x.relname), ["drivers", "orders"]);
    r = await client.query(
      "SELECT table_name FROM information_schema.tables ORDER BY 1");
    eq("information_schema is readable",
      r.rows.map((x) => x.table_name), ["drivers", "orders"]);
    r = await client.query(
      "SELECT typname FROM pg_catalog.pg_type WHERE oid = $1", [23]);
    eq("a catalogue column typed for a parameter", r.rows.map((x) => x.typname), ["int4"]);

    // ── grouped, aliased, aggregated ──────────────────────────────────────
    r = await client.query(
      "SELECT orders.status, count(*) AS n FROM orders GROUP BY orders.status ORDER BY 1");
    eq("a grouped query with a mixed select list",
      r.rows.map((x) => [x.status, Number(x.n)]), [["open", 1], ["paid", 2]]);

    // ── writes, and history for free ──────────────────────────────────────
    r = await client.query("SELECT _seq FROM orders WHERE _id = '1'");
    const before = Number(r.rows[0]._seq);
    await client.query("UPDATE orders SET total = $1 WHERE _id = $2", [999, "1"]);
    r = await client.query("SELECT total FROM orders WHERE _id = '1'");
    eq("an UPDATE is visible", Number(r.rows[0].total), 999);
    r = await client.query(
      `SELECT total FROM orders AS OF SYSTEM TIME ${before} WHERE _id = '1'`);
    eq("...and the prior value is still readable", Number(r.rows[0].total), 120);
  } finally {
    await client.end();
  }

  // ── boundaries, each named ──────────────────────────────────────────────
  await refused(pg.Client, conf, "DDL is refused by name", "CREATE TABLE t (a int)", "DDL");
  await refused(pg.Client, conf, "TRUNCATE is refused, and says why",
    "TRUNCATE orders", "append-only");
  await refused(pg.Client, conf, "an unknown qualifier is refused, not answered empty",
    "SELECT _id FROM orders WHERE nosuch.status = 'x'", "no table or alias");

  for (const a of assertions) process.stdout.write(JSON.stringify(a) + "\n");
}

main().catch((e) => {
  // Never exit silently: a harness that reports nothing is indistinguishable
  // from one that passed.
  process.stdout.write(JSON.stringify({
    name: "the node suite ran to completion",
    ok: false,
    detail: `${e && e.stack ? e.stack.split("\n")[0] : e}`,
  }) + "\n");
  process.exitCode = 0; // the Python side owns the verdict
});
