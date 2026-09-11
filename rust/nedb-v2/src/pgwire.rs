//! A PostgreSQL wire-protocol **read** endpoint for NEDB.
//!
//! # What this is, and what it is not
//!
//! This is **not** "NEDB speaks SQL". It is a read-only front door that speaks
//! the PostgreSQL v3 wire protocol well enough that tools built for Postgres —
//! `psql`, DBeaver, Metabase, Grafana, psycopg, any libpq-based client — can
//! connect to a NEDB store and run queries against it. A small, explicitly
//! documented subset of `SELECT` is translated to NQL; everything else is
//! rejected with a clear error naming what was not understood.
//!
//! The reason this exists: NEDB's differentiators (a tamper-evident chain,
//! permanent time travel, causal provenance) were unreachable from the tools
//! people already have. Every consumer needed a bespoke client. One protocol
//! implementation removes that, and it removes it for reads — which is what
//! auditors, analysts and dashboards actually do.
//!
//! **Writes are deliberately not supported.** A write has to carry `caused_by`,
//! valid-time bounds and idempotency to be worth anything in this engine, and
//! none of that has a natural SQL spelling. `INSERT`/`UPDATE`/`DELETE` return a
//! clear error pointing at the HTTP API rather than silently doing half a job.
//!
//! # Supported SQL subset
//!
//! ```sql
//! SELECT * | col [, col]* | COUNT(*) | <agg>(col)
//!   FROM <collection>
//!   [ AS OF SYSTEM TIME <seq> ]     -- bridges to NQL's AS OF
//!   [ WHERE <predicate> ]           -- the full NQL predicate surface
//!   [ GROUP BY <col> ]
//!   [ HAVING <predicate> ]
//!   [ ORDER BY <col> [ASC|DESC] (, ...) ]
//!   [ LIMIT <n> ] [ OFFSET <n> ]
//! ```
//!
//! Single-quoted SQL string literals are rewritten to NQL's double-quoted form,
//! and `<>` is rewritten to `!=`. Column projection is applied **here**, after
//! NQL returns whole documents, because NQL itself is FROM-first and has no
//! projection clause.
//!
//! No JOINs, no subqueries, no CTEs, no window functions, no DDL. Those are not
//! oversights to be apologised for — they are the boundary of what this door is
//! for, and the error messages say so.
//!
//! # Protocol coverage
//!
//! The **simple query protocol** (`Q`) is implemented, which is what `psql` and
//! libpq's `PQexec` use — and therefore what psycopg2 uses, since it
//! interpolates parameters client-side. The **extended query protocol**
//! (`Parse`/`Bind`/`Execute`) is not yet implemented; a client that insists on
//! it gets an error naming the gap rather than a hang. SSL is declined (`N`),
//! so clients must connect without TLS — put this behind a tunnel or a
//! loopback bind, which is the default.
//!
//! Authentication mirrors the HTTP surface: when `NEDBD_TOKEN` is set the
//! password must equal it (cleartext, hence the loopback default); otherwise
//! any connection is accepted.

use std::collections::HashMap;
use std::sync::Arc;

use serde_json::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

use crate::db::Db;

// ── Postgres type OIDs we hand out ──────────────────────────────────────────
const OID_BOOL: i32 = 16;
const OID_INT8: i32 = 20;
const OID_FLOAT8: i32 = 701;
const OID_TEXT: i32 = 25;

const PROTO_V3: i32 = 196_608; // 3.0 << 16
const SSL_REQUEST: i32 = 80_877_103;
const GSS_REQUEST: i32 = 80_877_104;
const CANCEL_REQUEST: i32 = 80_877_102;

/// How a caller resolves a database name to an open `Db`.
///
/// A trait object rather than a concrete handle so this module does not depend
/// on `server::Manager` — which keeps the protocol code unit-testable against a
/// plain `Db` with no HTTP stack in the way.
pub trait DbResolver: Send + Sync + 'static {
    /// Look up an open database by the name the client connected with.
    ///
    /// MAY BLOCK. The implementation is allowed to take a lock, so this is
    /// always called from `spawn_blocking` — never on an async worker. Taking
    /// a tokio `RwLock::blocking_read()` on a runtime thread panics outright
    /// ("Cannot block the current thread from within a runtime"), which is
    /// exactly how the first cut of this failed.
    fn resolve(&self, name: &str) -> Option<Arc<Db>>;
    /// The bearer token, when one is configured. `None` = open access.
    fn token(&self) -> Option<String> {
        None
    }
}

// ── wire encoding helpers ───────────────────────────────────────────────────

struct Out(Vec<u8>);

impl Out {
    fn msg(tag: u8) -> Self {
        // Tag, then a 4-byte length placeholder patched in `finish`.
        Out(vec![tag, 0, 0, 0, 0])
    }
    fn i16(&mut self, v: i16) { self.0.extend_from_slice(&v.to_be_bytes()); }
    fn i32(&mut self, v: i32) { self.0.extend_from_slice(&v.to_be_bytes()); }
    fn cstr(&mut self, s: &str) {
        // A NUL inside an identifier would truncate the field and desynchronise
        // the stream, so strip rather than trust.
        self.0.extend_from_slice(s.replace('\0', "").as_bytes());
        self.0.push(0);
    }
    fn bytes(&mut self, b: &[u8]) { self.0.extend_from_slice(b); }
    /// Patch the length prefix (which covers the length field itself, not the tag).
    fn finish(mut self) -> Vec<u8> {
        let len = (self.0.len() - 1) as i32;
        self.0[1..5].copy_from_slice(&len.to_be_bytes());
        self.0
    }
}

fn err_msg(code: &str, message: &str) -> Vec<u8> {
    let mut m = Out::msg(b'E');
    m.bytes(b"S"); m.cstr("ERROR");
    m.bytes(b"C"); m.cstr(code);
    m.bytes(b"M"); m.cstr(message);
    m.0.push(0);
    m.finish()
}

fn ready() -> Vec<u8> {
    let mut m = Out::msg(b'Z');
    m.bytes(b"I"); // idle, not in a transaction
    m.finish()
}

fn command_complete(tag: &str) -> Vec<u8> {
    let mut m = Out::msg(b'C');
    m.cstr(tag);
    m.finish()
}

// ── SQL → NQL translation ───────────────────────────────────────────────────

/// One output column: the key to read from the row, and the name to show.
///
/// The two differ for aggregates. NQL answers `SUM(total)` with a row holding
/// `sum_total` (plus `count` and a legacy `value` alias), while SQL callers
/// expect a single column called `sum`. Carrying both halves keeps NEDB's
/// internal key names off the wire — the first cut leaked `['count','value']`
/// out of a `SELECT COUNT(*)`, which is two columns where SQL promises one.
#[derive(Debug, PartialEq, Clone)]
pub struct Col {
    pub src: String,
    pub out: String,
}

impl Col {
    fn same(name: &str) -> Self {
        Col { src: name.to_string(), out: name.to_string() }
    }
    fn renamed(src: &str, out: &str) -> Self {
        Col { src: src.to_string(), out: out.to_string() }
    }
}

/// What a translated statement asks for.
#[derive(Debug, PartialEq)]
pub enum Stmt {
    /// Run this NQL, then project these columns (empty = all).
    Query { nql: String, project: Vec<Col> },
    /// Answer from a fixed table — the handshake queries clients send on connect.
    Canned { cols: Vec<String>, row: Vec<String> },
    /// Nothing to do (empty statement, or a SET the client does not need honoured).
    Ok(&'static str),
}

/// Strip SQL comments and collapse whitespace, so the matchers below can be
/// simple without being fragile about formatting.
fn normalise(sql: &str) -> String {
    let mut out = String::with_capacity(sql.len());
    let mut chars = sql.chars().peekable();
    let mut in_s = false;
    while let Some(c) = chars.next() {
        if in_s {
            out.push(c);
            if c == '\'' { in_s = false; }
            continue;
        }
        match c {
            '\'' => { in_s = true; out.push(c); }
            '-' if chars.peek() == Some(&'-') => {
                // line comment
                for n in chars.by_ref() { if n == '\n' { break; } }
                out.push(' ');
            }
            '/' if chars.peek() == Some(&'*') => {
                chars.next();
                let mut prev = ' ';
                while let Some(n) = chars.next() {
                    if prev == '*' && n == '/' { break; }
                    prev = n;
                }
                out.push(' ');
            }
            _ => out.push(c),
        }
    }
    out.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Rewrite SQL literal/operator spellings into NQL's.
///
/// Only `'…'` → `"…"` and `<>` → `!=`. Done with an explicit scan rather than a
/// regex so a quote inside a string cannot be mistaken for a delimiter: SQL
/// escapes an embedded quote by doubling it (`'it''s'`), and that has to become
/// a single character inside the NQL string rather than terminating it.
fn sql_literals_to_nql(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut it = s.chars().peekable();
    while let Some(c) = it.next() {
        match c {
            '\'' => {
                out.push('"');
                while let Some(ch) = it.next() {
                    if ch == '\'' {
                        if it.peek() == Some(&'\'') {
                            it.next();
                            out.push('\''); // doubled '' is one literal quote
                        } else {
                            break;
                        }
                    } else if ch == '"' {
                        // A double quote inside a SQL literal must be escaped
                        // for NQL, whose lexer collapses \" to a literal quote.
                        out.push('\\');
                        out.push('"');
                    } else {
                        out.push(ch);
                    }
                }
                out.push('"');
            }
            '<' if it.peek() == Some(&'>') => { it.next(); out.push_str("!="); }
            _ => out.push(c),
        }
    }
    out
}

fn strip_prefix_ci(s: &str, prefix: &str) -> Option<String> {
    if s.len() >= prefix.len() && s[..prefix.len()].eq_ignore_ascii_case(prefix) {
        Some(s[prefix.len()..].trim_start().to_string())
    } else {
        None
    }
}

/// Find a top-level keyword (not inside quotes or parentheses), returning its
/// byte offset. Case-insensitive, and only matches on word boundaries.
fn find_kw(s: &str, kw: &str) -> Option<usize> {
    let bytes = s.as_bytes();
    let k = kw.as_bytes();
    let mut depth = 0i32;
    let mut in_s = false;
    let mut in_d = false;
    let mut i = 0usize;
    while i < bytes.len() {
        let c = bytes[i];
        if in_s { if c == b'\'' { in_s = false; } i += 1; continue; }
        if in_d { if c == b'"' { in_d = false; } i += 1; continue; }
        match c {
            b'\'' => { in_s = true; i += 1; continue; }
            b'"' => { in_d = true; i += 1; continue; }
            b'(' => { depth += 1; i += 1; continue; }
            b')' => { depth -= 1; i += 1; continue; }
            _ => {}
        }
        if depth == 0 && i + k.len() <= bytes.len()
            && bytes[i..i + k.len()].eq_ignore_ascii_case(k)
        {
            let before_ok = i == 0 || !(bytes[i - 1] as char).is_alphanumeric() && bytes[i - 1] != b'_';
            let after = i + k.len();
            let after_ok = after >= bytes.len()
                || !(bytes[after] as char).is_alphanumeric() && bytes[after] != b'_';
            if before_ok && after_ok {
                return Some(i);
            }
        }
        i += 1;
    }
    None
}

/// Translate one SQL statement into something executable, or explain why not.
pub fn translate(sql_raw: &str) -> Result<Stmt, String> {
    let sql = normalise(sql_raw);
    let sql = sql.trim().trim_end_matches(';').trim();
    if sql.is_empty() {
        return Ok(Stmt::Ok(""));
    }
    let upper = sql.to_uppercase();

    // ── the handshake. Clients issue these before anything useful; answering
    // them with plausible values is the difference between "connects" and
    // "hangs on startup". They are canned on purpose — NEDB has no pg_catalog
    // and pretending otherwise would be worse than a clear boundary.
    if upper.starts_with("SET ") || upper.starts_with("BEGIN") || upper.starts_with("COMMIT")
        || upper.starts_with("ROLLBACK") || upper.starts_with("DISCARD")
        || upper.starts_with("LISTEN ") || upper.starts_with("UNLISTEN ")
    {
        // Accepted and ignored: there is one implicit read-only transaction.
        return Ok(Stmt::Ok(if upper.starts_with("SET") { "SET" } else { "OK" }));
    }
    if upper.starts_with("SHOW ") {
        let name = sql[5..].trim().to_lowercase();
        let val = match name.as_str() {
            "transaction_isolation" | "default_transaction_isolation" => "read committed",
            "server_version" => SERVER_VERSION,
            "server_encoding" | "client_encoding" => "UTF8",
            "standard_conforming_strings" => "on",
            "is_superuser" => "off",
            _ => "",
        };
        return Ok(Stmt::Canned { cols: vec![name], row: vec![val.to_string()] });
    }
    if upper == "SELECT VERSION()" {
        return Ok(Stmt::Canned {
            cols: vec!["version".into()],
            row: vec![full_version_string()],
        });
    }
    if upper == "SELECT 1" || upper == "SELECT 1;" {
        return Ok(Stmt::Canned { cols: vec!["?column?".into()], row: vec!["1".into()] });
    }
    if upper.starts_with("SELECT CURRENT_SCHEMA") {
        return Ok(Stmt::Canned { cols: vec!["current_schema".into()], row: vec!["public".into()] });
    }
    if upper.starts_with("SELECT CURRENT_DATABASE") {
        return Ok(Stmt::Canned { cols: vec!["current_database".into()], row: vec!["nedb".into()] });
    }
    if upper.starts_with("SELECT CURRENT_USER") || upper.starts_with("SELECT USER") {
        return Ok(Stmt::Canned { cols: vec!["current_user".into()], row: vec!["nedb".into()] });
    }

    // ── the refusals, each naming the boundary rather than "syntax error" ──
    for (kw, why) in [
        ("INSERT", "writes go through the HTTP API (POST /v1/databases/<db>/put) so a write can carry caused_by, valid-time bounds and idempotency — none of which have a SQL spelling here"),
        ("UPDATE", "this endpoint is read-only; NEDB is append-only, so an UPDATE is a new versioned put via the HTTP API"),
        ("DELETE", "this endpoint is read-only; use DELETE /v1/databases/<db>/rows/<coll>/<id>"),
        ("CREATE", "DDL is not supported — collections are created implicitly by writing to them"),
        ("ALTER", "DDL is not supported"),
        ("DROP", "DDL is not supported"),
        ("TRUNCATE", "not supported: NEDB is append-only by design, so history cannot be discarded"),
        ("COPY", "not supported; use GET /v1/databases/<db>/since for bulk export"),
    ] {
        if upper.starts_with(kw) {
            return Err(format!("{} is not supported on the Postgres read endpoint — {}", kw, why));
        }
    }
    if !upper.starts_with("SELECT") {
        return Err(format!(
            "only SELECT is supported on the Postgres read endpoint (got {:?})",
            sql.split_whitespace().next().unwrap_or("")
        ));
    }
    for (kw, why) in [
        (" JOIN ", "JOIN is not supported — NQL is single-collection; join in your client or model the relation with LINK/TRAVERSE"),
        (" UNION ", "UNION is not supported"),
        (" INTERSECT ", "INTERSECT is not supported"),
        (" EXCEPT ", "EXCEPT is not supported"),
        (" OVER (", "window functions are not supported"),
        ("DISTINCT ", "DISTINCT is not supported — GROUP BY <col> gives the distinct values with counts"),
    ] {
        if upper.contains(kw) {
            return Err(why.to_string());
        }
    }
    if find_kw(&upper, "FROM").is_none() {
        return Err("SELECT without FROM is not supported on this endpoint".into());
    }

    // ── SELECT <projection> FROM <rest> ──────────────────────────────────────
    let after_select = strip_prefix_ci(sql, "SELECT").ok_or("expected SELECT")?;
    let from_at = find_kw(&after_select.to_uppercase(), "FROM")
        .ok_or("expected FROM after the select list")?;
    let projection = after_select[..from_at].trim().to_string();
    let rest = after_select[from_at + 4..].trim().to_string();
    if rest.is_empty() {
        return Err("expected a collection name after FROM".into());
    }
    // A subquery in the FROM position, or a comma-separated table list (an
    // implicit cross join), are both out of scope — say which.
    if rest.starts_with('(') {
        return Err("subqueries in FROM are not supported".into());
    }
    let coll_end = rest.find(' ').unwrap_or(rest.len());
    let coll = &rest[..coll_end];
    if coll.contains(',') {
        return Err("selecting from more than one collection is not supported (no JOIN)".into());
    }
    // Postgres clients often qualify as schema.table; NEDB has one namespace.
    let coll = coll.rsplit('.').next().unwrap_or(coll).trim_matches('"');
    let tail = rest[coll_end..].trim();

    // ── the select list ──────────────────────────────────────────────────────
    let pu = projection.to_uppercase();
    let mut agg_clause = String::new();
    let mut project: Vec<Col> = vec![];

    if projection == "*" {
        // everything
    } else if pu.starts_with("COUNT(") {
        // COUNT(*) and COUNT(col) both become NQL's bare COUNT: NQL counts the
        // group, and a per-column non-null count is not expressible here.
        agg_clause = " COUNT".to_string();
        project.push(Col::same("count"));
    } else if let Some(agg) = ["SUM", "AVG", "MIN", "MAX"]
        .iter()
        .find(|a| pu.starts_with(&format!("{}(", a)))
    {
        let inner = projection[agg.len() + 1..]
            .trim_end_matches(')')
            .trim()
            .to_string();
        if inner.is_empty() || inner == "*" {
            return Err(format!("{}() needs a column", agg));
        }
        agg_clause = format!(" {} {}", agg, inner);
        // NQL emits `<agg>_<field>`; SQL names the column after the function.
        project.push(Col::renamed(
            &format!("{}_{}", agg.to_lowercase(), inner),
            &agg.to_lowercase(),
        ));
    } else {
        for part in projection.split(',') {
            let p = part.trim();
            if p.is_empty() {
                return Err("empty column in the select list".into());
            }
            if p.contains('(') {
                return Err(format!(
                    "expressions in the select list are not supported ({:?}) — \
                     supported: *, a column list, COUNT(*), or SUM/AVG/MIN/MAX(col)", p));
            }
            // strip an alias: `col AS x` / `col x`
            let raw = p.split_whitespace().next().unwrap_or(p);
            let name = raw.rsplit('.').next().unwrap_or(raw).trim_matches('"');
            project.push(Col::same(name));
        }
    }

    // ── clause tail: AS OF SYSTEM TIME → AS OF, then pass the rest through ──
    //
    // The clause keywords NQL shares with SQL (WHERE, GROUP BY, HAVING,
    // ORDER BY, LIMIT, OFFSET) are deliberately handed to the NQL parser
    // unchanged rather than re-parsed here. NQL is the authority on what is
    // valid; re-implementing its grammar would give two parsers to disagree.
    let mut tail = tail.to_string();
    let tu = tail.to_uppercase();
    if let Some(at) = find_kw(&tu, "AS OF SYSTEM TIME") {
        let before = tail[..at].to_string();
        let after = tail[at + "AS OF SYSTEM TIME".len()..].trim_start().to_string();
        // Take the sequence token; the rest of the tail follows it.
        let end = after.find(' ').unwrap_or(after.len());
        let seq = after[..end].trim().trim_matches('\'').trim_matches('"').to_string();
        if seq.parse::<u64>().is_err() {
            return Err(format!(
                "AS OF SYSTEM TIME takes a NEDB sequence number here, not a timestamp (got {:?}). \
                 NEDB's history is sequence-addressed and never garbage-collected, so a seq is \
                 exact where a wall-clock time would be approximate", seq));
        }
        tail = format!("{} AS OF {} {}", before.trim(), seq, after[end..].trim())
            .trim()
            .to_string();
    }

    // ── GROUP BY: refuse a bare column that SQL would refuse ─────────────────
    //
    // A grouped NQL row holds only the group key, `count` and the aggregate —
    // so projecting `total` from `GROUP BY region` found nothing and rendered
    // NULL. Silently answering NULL for a column the query cannot produce is
    // the exact failure shape this engine keeps getting bitten by, so it is an
    // error, using Postgres's own wording so the message is already familiar.
    let tu_all = tail.to_uppercase();
    if let Some(gb_at) = find_kw(&tu_all, "GROUP BY") {
        let after = tail[gb_at + "GROUP BY".len()..].trim_start();
        let key_end = after.find(|c: char| c == ' ' || c == ',').unwrap_or(after.len());
        let group_key = after[..key_end].trim().trim_matches('"').to_string();
        let is_agg = !agg_clause.is_empty();
        for c in &project {
            let ok = c.src == group_key
                || c.src == "count"
                || (is_agg && c.out == agg_clause.trim().split(' ').next()
                        .unwrap_or("").to_lowercase());
            if !ok {
                return Err(format!(
                    "column {:?} must appear in the GROUP BY clause or be used in an \
                     aggregate function — a grouped row carries the group key, `count`, \
                     and the aggregate, nothing else",
                    c.src));
            }
        }
    }

    let tail = sql_literals_to_nql(&tail);
    let nql = format!("FROM {}{}{}", coll,
                      if agg_clause.is_empty() { String::new() } else { agg_clause },
                      if tail.is_empty() { String::new() } else { format!(" {}", tail) });

    Ok(Stmt::Query { nql: nql.trim().to_string(), project })
}

const SERVER_VERSION: &str = "15.0";

fn full_version_string() -> String {
    format!(
        "PostgreSQL {} (NEDB {} read endpoint) — tamper-evident, append-only, \
         permanent history. Read-only: SELECT only.",
        SERVER_VERSION,
        env!("CARGO_PKG_VERSION")
    )
}

// ── result shaping ──────────────────────────────────────────────────────────

/// Pick the column order for a result set.
///
/// With an explicit projection, that order. Otherwise the union of keys across
/// the returned rows — `_`-prefixed provenance columns last, so `psql` shows
/// the user's own fields first and `_hash` does not push `status` off screen.
fn columns_for(rows: &[Value], project: &[Col]) -> Vec<Col> {
    if !project.is_empty() {
        return project.to_vec();
    }
    let mut plain: Vec<String> = vec![];
    let mut meta: Vec<String> = vec![];
    for r in rows {
        if let Value::Object(m) = r {
            for k in m.keys() {
                let target = if k.starts_with('_') { &mut meta } else { &mut plain };
                if !target.contains(k) {
                    target.push(k.clone());
                }
            }
        }
    }
    plain.sort();
    meta.sort();
    plain.extend(meta);
    plain.into_iter().map(|k| Col::same(&k)).collect()
}

fn oid_for(rows: &[Value], col: &str) -> i32 {
    for r in rows {
        match r.get(col) {
            Some(Value::Bool(_)) => return OID_BOOL,
            Some(Value::Number(n)) => {
                return if n.is_i64() || n.is_u64() { OID_INT8 } else { OID_FLOAT8 }
            }
            Some(Value::String(_)) => return OID_TEXT,
            Some(Value::Null) | None => continue,
            _ => return OID_TEXT, // arrays/objects render as JSON text
        }
    }
    OID_TEXT
}

/// Render one cell in the text format Postgres clients expect for format 0.
fn cell(v: Option<&Value>) -> Option<String> {
    match v {
        None | Some(Value::Null) => None, // NULL on the wire
        Some(Value::String(s)) => Some(s.clone()),
        Some(Value::Bool(b)) => Some(if *b { "t".into() } else { "f".into() }),
        Some(other) => Some(other.to_string()),
    }
}

fn row_description(cols: &[Col], oids: &[i32]) -> Vec<u8> {
    let mut m = Out::msg(b'T');
    m.i16(cols.len() as i16);
    for (i, c) in cols.iter().enumerate() {
        m.cstr(&c.out);
        m.i32(0); // table OID — unknown
        m.i16((i + 1) as i16); // column attribute number
        m.i32(oids[i]);
        m.i16(-1); // variable length
        m.i32(-1); // no type modifier
        m.i16(0); // text format
    }
    m.finish()
}

fn data_row(vals: &[Option<String>]) -> Vec<u8> {
    let mut m = Out::msg(b'D');
    m.i16(vals.len() as i16);
    for v in vals {
        match v {
            None => m.i32(-1),
            Some(s) => {
                m.i32(s.len() as i32);
                m.bytes(s.as_bytes());
            }
        }
    }
    m.finish()
}

/// Encode a completed result set as the `T`/`D`*/`C` message sequence.
pub fn encode_result(rows: &[Value], project: &[Col]) -> Vec<u8> {
    let cols = columns_for(rows, project);
    let oids: Vec<i32> = cols.iter().map(|c| oid_for(rows, &c.src)).collect();
    let mut out = row_description(&cols, &oids);
    for r in rows {
        let vals: Vec<Option<String>> = cols.iter().map(|c| cell(r.get(&c.src))).collect();
        out.extend_from_slice(&data_row(&vals));
    }
    out.extend_from_slice(&command_complete(&format!("SELECT {}", rows.len())));
    out
}

fn encode_canned(cols: &[String], row: &[String]) -> Vec<u8> {
    let cols: Vec<Col> = cols.iter().map(|c| Col::same(c)).collect();
    let oids: Vec<i32> = cols.iter().map(|_| OID_TEXT).collect();
    let mut out = row_description(&cols, &oids);
    let vals: Vec<Option<String>> = row.iter().map(|s| Some(s.clone())).collect();
    out.extend_from_slice(&data_row(&vals));
    out.extend_from_slice(&command_complete("SELECT 1"));
    out
}

// ── connection handling ─────────────────────────────────────────────────────

async fn read_exact(sock: &mut TcpStream, n: usize) -> std::io::Result<Vec<u8>> {
    let mut buf = vec![0u8; n];
    sock.read_exact(&mut buf).await?;
    Ok(buf)
}

async fn read_i32(sock: &mut TcpStream) -> std::io::Result<i32> {
    let b = read_exact(sock, 4).await?;
    Ok(i32::from_be_bytes([b[0], b[1], b[2], b[3]]))
}

fn parse_startup_params(body: &[u8]) -> HashMap<String, String> {
    let mut out = HashMap::new();
    let mut parts = body.split(|b| *b == 0).map(|s| String::from_utf8_lossy(s).to_string());
    while let (Some(k), Some(v)) = (parts.next(), parts.next()) {
        if k.is_empty() {
            break;
        }
        out.insert(k, v);
    }
    out
}

/// Serve one client connection to completion.
async fn handle(mut sock: TcpStream, resolver: Arc<dyn DbResolver>) -> std::io::Result<()> {
    // ── startup, including the SSL negotiation clients try first ────────────
    let params = loop {
        let len = read_i32(&mut sock).await?;
        if len < 8 || len > 1 << 20 {
            return Ok(()); // nonsense framing — drop the connection
        }
        let code = read_i32(&mut sock).await?;
        let body = read_exact(&mut sock, (len - 8) as usize).await?;
        match code {
            SSL_REQUEST | GSS_REQUEST => {
                // Decline and let the client retry in the clear.
                sock.write_all(b"N").await?;
                continue;
            }
            CANCEL_REQUEST => return Ok(()), // nothing cancellable: reads are synchronous
            PROTO_V3 => break parse_startup_params(&body),
            other => {
                let major = other >> 16;
                sock.write_all(&err_msg(
                    "0A000",
                    &format!("unsupported frontend protocol {}.{} — this endpoint speaks 3.0",
                             major, other & 0xffff),
                )).await?;
                return Ok(());
            }
        }
    };

    let db_name = params.get("database").cloned().unwrap_or_default();

    // Resolve the database ONCE, here, on a blocking thread.
    //
    // A Postgres connection is bound to one database for its whole life, so
    // per-connection resolution is both correct and simpler than resolving per
    // statement — and it keeps the lock acquisition off the async worker.
    let resolved: Option<Arc<Db>> = {
        let r = Arc::clone(&resolver);
        let name = db_name.clone();
        tokio::task::spawn_blocking(move || r.resolve(&name))
            .await
            .unwrap_or(None)
    };

    // ── auth: mirror the HTTP surface ───────────────────────────────────────
    if let Some(expected) = resolver.token() {
        // AuthenticationCleartextPassword (3)
        let mut m = Out::msg(b'R');
        m.i32(3);
        sock.write_all(&m.finish()).await?;

        let tag = read_exact(&mut sock, 1).await?;
        if tag[0] != b'p' {
            sock.write_all(&err_msg("28000", "expected a password message")).await?;
            return Ok(());
        }
        let len = read_i32(&mut sock).await?;
        if len < 4 || len > 1 << 16 {
            return Ok(());
        }
        let body = read_exact(&mut sock, (len - 4) as usize).await?;
        let supplied = String::from_utf8_lossy(&body).trim_end_matches('\0').to_string();
        // Constant-time-ish: compare lengths and bytes without early return.
        let ok = supplied.len() == expected.len()
            && supplied.bytes().zip(expected.bytes()).fold(0u8, |a, (x, y)| a | (x ^ y)) == 0;
        if !ok {
            sock.write_all(&err_msg("28P01", "password authentication failed")).await?;
            return Ok(());
        }
    }

    let mut m = Out::msg(b'R');
    m.i32(0); // AuthenticationOk
    sock.write_all(&m.finish()).await?;

    for (k, v) in [
        ("server_version", SERVER_VERSION),
        ("server_encoding", "UTF8"),
        ("client_encoding", "UTF8"),
        ("DateStyle", "ISO, MDY"),
        ("integer_datetimes", "on"),
        ("standard_conforming_strings", "on"),
        ("application_name", "nedbd"),
    ] {
        let mut p = Out::msg(b'S');
        p.cstr(k);
        p.cstr(v);
        sock.write_all(&p.finish()).await?;
    }
    let mut k = Out::msg(b'K');
    k.i32(std::process::id() as i32);
    k.i32(0);
    sock.write_all(&k.finish()).await?;
    sock.write_all(&ready()).await?;

    // ── message loop ────────────────────────────────────────────────────────
    loop {
        let mut tag = [0u8; 1];
        if sock.read_exact(&mut tag).await.is_err() {
            return Ok(()); // client hung up
        }
        let len = read_i32(&mut sock).await?;
        if len < 4 || len > 64 << 20 {
            return Ok(());
        }
        let body = read_exact(&mut sock, (len - 4) as usize).await?;

        match tag[0] {
            b'X' => return Ok(()), // Terminate
            b'Q' => {
                let sql = String::from_utf8_lossy(&body).trim_end_matches('\0').to_string();
                let out = run_simple_query(&sql, &db_name, resolved.as_ref());
                sock.write_all(&out).await?;
                sock.write_all(&ready()).await?;
            }
            // The extended query protocol. Answering with a clear error beats
            // silence: a client that waits for a ParseComplete that never comes
            // hangs, and a hang is the worst possible diagnostic.
            b'P' | b'B' | b'E' | b'D' | b'C' | b'H' => {
                sock.write_all(&err_msg(
                    "0A000",
                    "the extended query protocol (Parse/Bind/Execute) is not implemented yet — \
                     this endpoint speaks the simple query protocol, which psql and libpq's \
                     PQexec use. Send the statement with parameters already interpolated.",
                )).await?;
                sock.write_all(&ready()).await?;
            }
            b'S' => {
                sock.write_all(&ready()).await?;
            }
            other => {
                sock.write_all(&err_msg(
                    "08P01",
                    &format!("unexpected frontend message {:?}", other as char),
                )).await?;
                sock.write_all(&ready()).await?;
            }
        }
    }
}

/// Execute a simple-query payload, which may hold several `;`-separated statements.
fn run_simple_query(sql: &str, db_name: &str, db: Option<&Arc<Db>>) -> Vec<u8> {
    let mut out = vec![];
    let statements = split_statements(sql);
    if statements.is_empty() {
        // EmptyQueryResponse
        return Out::msg(b'I').finish();
    }
    for stmt_sql in statements {
        match translate(&stmt_sql) {
            Err(why) => {
                out.extend_from_slice(&err_msg("0A000", &why));
                return out; // abandon the rest of the batch, as Postgres does
            }
            Ok(Stmt::Ok(tag)) => {
                out.extend_from_slice(&command_complete(if tag.is_empty() { "SELECT 0" } else { tag }));
            }
            Ok(Stmt::Canned { cols, row }) => {
                out.extend_from_slice(&encode_canned(&cols, &row));
            }
            Ok(Stmt::Query { nql, project }) => {
                let db = match db {
                    Some(db) => db,
                    None => {
                        out.extend_from_slice(&err_msg(
                            "3D000",
                            &format!(
                                "database {:?} is not open on this server — create it first \
                                 (POST /v1/databases), or connect with -d <name>",
                                db_name),
                        ));
                        return out;
                    }
                };
                match crate::nql::query(db, &nql) {
                    Ok((rows, _)) => out.extend_from_slice(&encode_result(&rows, &project)),
                    Err(e) => {
                        out.extend_from_slice(&err_msg(
                            "42601",
                            &format!("{} (translated to NQL: {})", e, nql),
                        ));
                        return out;
                    }
                }
            }
        }
    }
    out
}

/// Split on `;` at the top level, ignoring separators inside string literals.
fn split_statements(sql: &str) -> Vec<String> {
    let mut out = vec![];
    let mut cur = String::new();
    let mut in_s = false;
    for c in sql.chars() {
        match c {
            '\'' => { in_s = !in_s; cur.push(c); }
            ';' if !in_s => {
                if !cur.trim().is_empty() { out.push(cur.clone()); }
                cur.clear();
            }
            _ => cur.push(c),
        }
    }
    if !cur.trim().is_empty() {
        out.push(cur);
    }
    out
}

/// Bind and serve the Postgres read endpoint until the process exits.
pub async fn run(host: &str, port: u16, resolver: Arc<dyn DbResolver>) -> anyhow::Result<()> {
    let listener = TcpListener::bind((host, port)).await?;
    println!("  pgwire   postgres READ endpoint on {}:{} — psql / DBeaver / psycopg (SELECT only)",
             host, port);
    loop {
        let (sock, _peer) = match listener.accept().await {
            Ok(v) => v,
            Err(e) => {
                eprintln!("  [pgwire] accept failed: {}", e);
                continue;
            }
        };
        let r = Arc::clone(&resolver);
        tokio::spawn(async move {
            let _ = sock.set_nodelay(true);
            if let Err(e) = handle(sock, r).await {
                // A client disconnecting mid-message is routine, not an incident.
                if e.kind() != std::io::ErrorKind::UnexpectedEof
                    && e.kind() != std::io::ErrorKind::ConnectionReset
                {
                    eprintln!("  [pgwire] connection error: {}", e);
                }
            }
        });
    }
}

// ─────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn q(sql: &str) -> String {
        match translate(sql) {
            Ok(Stmt::Query { nql, .. }) => nql,
            other => panic!("expected a query for {:?}, got {:?}", sql, other),
        }
    }
    /// Output column names, in order.
    fn proj(sql: &str) -> Vec<String> {
        match translate(sql) {
            Ok(Stmt::Query { project, .. }) => project.iter().map(|c| c.out.clone()).collect(),
            other => panic!("expected a query for {:?}, got {:?}", sql, other),
        }
    }
    /// (source key, output name) pairs, for the aggregate renaming.
    fn proj_pairs(sql: &str) -> Vec<(String, String)> {
        match translate(sql) {
            Ok(Stmt::Query { project, .. }) =>
                project.iter().map(|c| (c.src.clone(), c.out.clone())).collect(),
            other => panic!("expected a query for {:?}, got {:?}", sql, other),
        }
    }
    fn names(cols: &[Col]) -> Vec<String> { cols.iter().map(|c| c.out.clone()).collect() }

    #[test]
    fn select_star_becomes_bare_from() {
        assert_eq!(q("SELECT * FROM orders"), "FROM orders");
        assert_eq!(q("select * from orders;"), "FROM orders");
        assert_eq!(proj("SELECT * FROM orders"), Vec::<String>::new());
    }

    #[test]
    fn a_column_list_becomes_a_projection_not_a_clause() {
        // NQL has no projection, so the column list is carried separately and
        // applied to the returned rows.
        assert_eq!(q("SELECT status, total FROM orders"), "FROM orders");
        assert_eq!(proj("SELECT status, total FROM orders"), vec!["status", "total"]);
    }

    #[test]
    fn aliases_and_qualified_names_reduce_to_the_field() {
        assert_eq!(proj("SELECT o.status AS s, o.total total FROM orders o"),
                   vec!["status", "total"]);
        assert_eq!(q("SELECT * FROM public.orders"), "FROM orders");
        assert_eq!(q("SELECT * FROM \"orders\""), "FROM orders");
    }

    #[test]
    fn where_clauses_pass_through_with_sql_literals_rewritten() {
        assert_eq!(q("SELECT * FROM orders WHERE status = 'paid'"),
                   r#"FROM orders WHERE status = "paid""#);
        assert_eq!(q("SELECT * FROM orders WHERE status <> 'paid'"),
                   r#"FROM orders WHERE status != "paid""#);
        assert_eq!(q("SELECT * FROM orders WHERE status IN ('paid','open')"),
                   r#"FROM orders WHERE status IN ("paid","open")"#);
    }

    /// SQL escapes an embedded quote by doubling it. That must become ONE
    /// character inside the NQL string, not terminate it.
    #[test]
    fn a_doubled_sql_quote_is_one_literal_character() {
        assert_eq!(q("SELECT * FROM t WHERE name = 'it''s'"),
                   r#"FROM t WHERE name = "it's""#);
    }

    /// A double quote inside a SQL literal has to be escaped for NQL, whose
    /// lexer collapses \" — otherwise it would close the string early.
    #[test]
    fn a_double_quote_inside_a_sql_literal_is_escaped_for_nql() {
        assert_eq!(q(r#"SELECT * FROM t WHERE name = 'say "hi"'"#),
                   r#"FROM t WHERE name = "say \"hi\"""#);
    }

    #[test]
    fn the_shared_clauses_are_handed_to_nql_unchanged() {
        assert_eq!(q("SELECT * FROM orders ORDER BY total DESC LIMIT 10 OFFSET 5"),
                   "FROM orders ORDER BY total DESC LIMIT 10 OFFSET 5");
        assert_eq!(q("SELECT * FROM orders GROUP BY region"), "FROM orders GROUP BY region");
        assert_eq!(q("SELECT * FROM o WHERE total BETWEEN 1 AND 9 ORDER BY a, b DESC"),
                   "FROM o WHERE total BETWEEN 1 AND 9 ORDER BY a, b DESC");
    }

    /// An aggregate must surface as ONE column, named as SQL names it.
    ///
    /// NQL answers `SUM(total)` with `{count, sum_total, value}` — `value`
    /// being a back-compat alias. Passing that straight through gave
    /// `SELECT COUNT(*)` two columns (`count`, `value`) where SQL promises
    /// one, and leaked an internal key name onto the wire.
    #[test]
    fn an_aggregate_is_one_column_named_as_sql_names_it() {
        assert_eq!(proj_pairs("SELECT COUNT(*) FROM orders"),
                   vec![("count".to_string(), "count".to_string())]);
        assert_eq!(proj_pairs("SELECT SUM(total) FROM orders"),
                   vec![("sum_total".to_string(), "sum".to_string())]);
        assert_eq!(proj_pairs("SELECT avg(total) FROM orders"),
                   vec![("avg_total".to_string(), "avg".to_string())]);
        assert_eq!(proj_pairs("SELECT MIN(total) FROM orders"),
                   vec![("min_total".to_string(), "min".to_string())]);
        // And the encoded result really is one column with that name.
        let rows = vec![json!({"count": 4, "sum_total": 420, "value": 420})];
        let p = vec![Col::renamed("sum_total", "sum")];
        let cols = columns_for(&rows, &p);
        assert_eq!(names(&cols), vec!["sum"], "one column, SQL's name");
        assert_eq!(cell(rows[0].get(&cols[0].src)), Some("420".to_string()));
    }

    /// A grouped NQL row holds the group key, `count` and the aggregate —
    /// nothing else. Projecting another column found nothing and rendered
    /// NULL, which is a silent wrong answer. Postgres errors; so do we, in
    /// Postgres's own words.
    #[test]
    fn a_bare_column_with_group_by_is_refused_not_nulled() {
        let e = translate("SELECT region, total FROM orders GROUP BY region").unwrap_err();
        assert!(e.contains("must appear in the GROUP BY clause"), "{}", e);
        assert!(e.contains("total"), "the message names the offending column: {}", e);

        // The group key itself, and `count`, are both legitimate.
        assert!(translate("SELECT region FROM orders GROUP BY region").is_ok());
        assert!(translate("SELECT region, count FROM orders GROUP BY region").is_ok());
        // As is an aggregate over the grouped set.
        assert!(translate("SELECT SUM(total) FROM orders GROUP BY region").is_ok());
        // And `*` is unaffected — it returns whatever the grouped row holds.
        assert!(translate("SELECT * FROM orders GROUP BY region").is_ok());
    }

    #[test]
    fn count_star_becomes_nql_count() {
        assert_eq!(q("SELECT COUNT(*) FROM orders"), "FROM orders COUNT");
        assert_eq!(q("SELECT count(*) FROM orders WHERE total > 5"),
                   "FROM orders COUNT WHERE total > 5");
    }

    #[test]
    fn aggregates_carry_their_target_column() {
        assert_eq!(q("SELECT SUM(total) FROM orders"), "FROM orders SUM total");
        assert_eq!(q("SELECT avg(total) FROM orders WHERE region = 'eu'"),
                   r#"FROM orders AVG total WHERE region = "eu""#);
        assert!(translate("SELECT SUM(*) FROM orders").is_err());
    }

    /// The bridge worth having: Postgres spells time travel
    /// `AS OF SYSTEM TIME`, and NEDB's is sequence-addressed and permanent.
    #[test]
    fn as_of_system_time_bridges_to_nql_as_of() {
        assert_eq!(q("SELECT * FROM orders AS OF SYSTEM TIME 42"),
                   "FROM orders AS OF 42");
        assert_eq!(q("SELECT * FROM orders AS OF SYSTEM TIME 42 WHERE total > 1"),
                   "FROM orders AS OF 42 WHERE total > 1");
        // A wall-clock timestamp is refused with the reason, not silently ignored.
        let e = translate("SELECT * FROM orders AS OF SYSTEM TIME '2026-01-01'").unwrap_err();
        assert!(e.contains("sequence number"), "{}", e);
    }

    #[test]
    fn handshake_queries_are_answered_so_clients_can_connect() {
        assert!(matches!(translate("SELECT version()"), Ok(Stmt::Canned { .. })));
        assert!(matches!(translate("SHOW transaction_isolation"), Ok(Stmt::Canned { .. })));
        assert!(matches!(translate("SELECT current_schema()"), Ok(Stmt::Canned { .. })));
        assert!(matches!(translate("SET extra_float_digits = 3"), Ok(Stmt::Ok(_))));
        assert!(matches!(translate("BEGIN"), Ok(Stmt::Ok(_))));
        assert!(matches!(translate(""), Ok(Stmt::Ok(_))));
    }

    /// Every refusal has to name the boundary. "Syntax error" would send a
    /// developer hunting for a typo that is not there.
    #[test]
    fn unsupported_sql_is_refused_with_a_reason() {
        for (sql, expect) in [
            ("INSERT INTO t VALUES (1)", "caused_by"),
            ("UPDATE t SET a = 1", "append-only"),
            ("DELETE FROM t", "read-only"),
            ("CREATE TABLE t (a int)", "DDL"),
            ("TRUNCATE t", "append-only"),
            ("SELECT * FROM a JOIN b ON a.x = b.x", "JOIN is not supported"),
            ("SELECT * FROM a UNION SELECT * FROM b", "UNION"),
            ("SELECT DISTINCT region FROM orders", "GROUP BY"),
            ("SELECT * FROM (SELECT 1) x", "subqueries in FROM"),
            ("SELECT * FROM a, b", "more than one collection"),
            ("SELECT lower(status) FROM orders", "expressions in the select list"),
            ("VACUUM", "only SELECT"),
        ] {
            let e = translate(sql).unwrap_err();
            assert!(e.contains(expect), "for {:?} expected {:?} in {:?}", sql, expect, e);
        }
    }

    #[test]
    fn comments_and_whitespace_do_not_confuse_the_translator() {
        assert_eq!(q("SELECT *\n  FROM orders  -- trailing note\n"), "FROM orders");
        assert_eq!(q("SELECT /* inline */ * FROM orders"), "FROM orders");
        // A keyword inside a string literal must not be treated as a clause.
        assert_eq!(q("SELECT * FROM t WHERE note = 'from here to JOIN'"),
                   r#"FROM t WHERE note = "from here to JOIN""#);
    }

    #[test]
    fn find_kw_ignores_quotes_parens_and_substrings() {
        assert_eq!(find_kw("SELECT A FROM B", "FROM"), Some(9));
        assert_eq!(find_kw("SELECT 'FROM' FROM B", "FROM"), Some(14));
        assert_eq!(find_kw("SELECT F(x FROM y) FROM B", "FROM"), Some(19));
        assert_eq!(find_kw("SELECT FROMAGE", "FROM"), None);
        assert_eq!(find_kw("SELECT X_FROM", "FROM"), None);
    }

    // ── result encoding ──────────────────────────────────────────────────────

    #[test]
    fn provenance_columns_sort_after_the_users_own_fields() {
        let rows = vec![json!({"_id":"1","_hash":"ab","status":"paid","total":9})];
        assert_eq!(names(&columns_for(&rows, &[])),
                   vec!["status", "total", "_hash", "_id"]);
    }

    #[test]
    fn an_explicit_projection_sets_the_column_order() {
        let rows = vec![json!({"a":1,"b":2})];
        let p = vec![Col::same("b"), Col::same("a")];
        assert_eq!(names(&columns_for(&rows, &p)), vec!["b", "a"]);
    }

    #[test]
    fn columns_are_the_union_across_sparse_rows() {
        // A document store has no schema, so row 2 may carry a field row 1 lacks.
        let rows = vec![json!({"a":1}), json!({"b":2})];
        assert_eq!(names(&columns_for(&rows, &[])), vec!["a", "b"]);
    }

    #[test]
    fn type_oids_follow_the_first_non_null_value() {
        let rows = vec![json!({"i":1,"f":1.5,"b":true,"s":"x","n":null})];
        assert_eq!(oid_for(&rows, "i"), OID_INT8);
        assert_eq!(oid_for(&rows, "f"), OID_FLOAT8);
        assert_eq!(oid_for(&rows, "b"), OID_BOOL);
        assert_eq!(oid_for(&rows, "s"), OID_TEXT);
        // All-null and absent columns fall back to text rather than guessing.
        assert_eq!(oid_for(&rows, "n"), OID_TEXT);
        assert_eq!(oid_for(&rows, "absent"), OID_TEXT);
    }

    #[test]
    fn a_column_that_is_null_in_the_first_row_still_gets_its_type() {
        let rows = vec![json!({"v": null}), json!({"v": 7})];
        assert_eq!(oid_for(&rows, "v"), OID_INT8);
    }

    #[test]
    fn cells_render_in_postgres_text_format() {
        assert_eq!(cell(Some(&json!("x"))), Some("x".to_string()));
        assert_eq!(cell(Some(&json!(true))), Some("t".to_string()));
        assert_eq!(cell(Some(&json!(false))), Some("f".to_string()));
        assert_eq!(cell(Some(&json!(42))), Some("42".to_string()));
        assert_eq!(cell(Some(&json!(null))), None);
        assert_eq!(cell(None), None);
        // Nested values render as JSON text rather than being dropped.
        assert_eq!(cell(Some(&json!({"a":1}))), Some("{\"a\":1}".to_string()));
    }

    /// The framing has to be exact or the client desynchronises and hangs.
    /// Length covers the length field itself but not the tag byte.
    #[test]
    fn message_framing_length_excludes_the_tag() {
        let mut m = Out::msg(b'Z');
        m.bytes(b"I");
        let bytes = m.finish();
        assert_eq!(bytes[0], b'Z');
        assert_eq!(i32::from_be_bytes([bytes[1], bytes[2], bytes[3], bytes[4]]), 5);
        assert_eq!(bytes.len(), 6);
    }

    #[test]
    fn a_result_set_encodes_as_description_then_rows_then_complete() {
        let rows = vec![json!({"a": 1}), json!({"a": 2})];
        let out = encode_result(&rows, &[]);
        assert_eq!(out[0], b'T');
        let tags: Vec<u8> = {
            // Walk the message stream by its own length prefixes.
            let mut t = vec![];
            let mut i = 0usize;
            while i < out.len() {
                t.push(out[i]);
                let len = i32::from_be_bytes([out[i+1], out[i+2], out[i+3], out[i+4]]) as usize;
                i += 1 + len;
            }
            t
        };
        assert_eq!(tags, vec![b'T', b'D', b'D', b'C'],
                   "one description, one row each, one completion");
    }

    #[test]
    fn an_empty_result_still_sends_a_description() {
        let out = encode_result(&[], &[Col::same("a")]);
        assert_eq!(out[0], b'T', "clients need the shape even with no rows");
    }

    #[test]
    fn statements_split_on_top_level_semicolons_only() {
        assert_eq!(split_statements("SELECT 1; SELECT 2").len(), 2);
        assert_eq!(split_statements("SELECT ';'").len(), 1);
        assert_eq!(split_statements("SELECT 1;").len(), 1);
        assert_eq!(split_statements("   ").len(), 0);
    }

    #[test]
    fn the_extended_protocol_gap_is_named_not_hidden() {
        // Guard the message a driver will see, because a hang would be worse.
        let e = String::from_utf8_lossy(&err_msg("0A000", "x")).to_string();
        assert!(e.contains("ERROR"));
        assert!(e.contains("0A000"));
    }
}
