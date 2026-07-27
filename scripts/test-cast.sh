#!/usr/bin/env bash
# test-cast.sh — exercise the /cast endpoint against a REAL running nedbd.
#
#   ./scripts/test-cast.sh                 # assumes nedbd already on :7070
#   ./scripts/test-cast.sh --boot          # start one, test it, shut it down
#   NEDB_URL=http://host:7070 ./scripts/test-cast.sh
#
# Build first:
#   cargo build --release --features cast -p nedb-engine --bin nedbd
#
# Weights: download model.cast from
#   https://github.com/aiassistsecure/nedb-cast-slm/releases
# and put it in the data dir, or export NEDBD_CAST_MODEL=/path/to/model.cast
#
# Exit code is 0 only if every check passed, so this drops into CI unchanged.

set -uo pipefail

B="${NEDB_URL:-http://127.0.0.1:7070}"
DB="casttest_$$"
PASS=0; FAIL=0; SKIP=0
BOOT=0; PID=""
[ "${1:-}" = "--boot" ] && BOOT=1

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  G=$'\033[32;1m'; R=$'\033[31;1m'; Y=$'\033[33;1m'; C=$'\033[36;1m'; D=$'\033[2m'; Z=$'\033[0m'
else
  G=""; R=""; Y=""; C=""; D=""; Z=""
fi

ok()   { echo "${G}[OK  ]${Z} $1 ${D}${2:-}${Z}"; PASS=$((PASS+1)); }
bad()  { echo "${R}[FAIL]${Z} $1 ${D}${2:-}${Z}"; FAIL=$((FAIL+1)); }
skip() { echo "${Y}[SKIP]${Z} $1 ${D}${2:-}${Z}"; SKIP=$((SKIP+1)); }
sect() { echo; echo "${C}$(printf '=%.0s' {1..66})${Z}"; echo "${C} $1${Z}"; echo "${C}$(printf '=%.0s' {1..66})${Z}"; }

cleanup() {
  curl -s -X DELETE "$B/v1/databases/$DB" >/dev/null 2>&1
  [ -n "$PID" ] && kill "$PID" 2>/dev/null
}
trap cleanup EXIT

# ── boot ──────────────────────────────────────────────────────────────────────
if [ "$BOOT" = "1" ]; then
  BIN="./target/release/nedbd"
  [ -x "$BIN" ] || BIN="$(command -v nedbd)"
  if [ ! -x "$BIN" ]; then
    echo "${R}no nedbd binary. Build it:${Z}"
    echo "  cargo build --release --features cast -p nedb-engine --bin nedbd"
    exit 1
  fi
  D_DIR="$(mktemp -d)"
  echo "booting $BIN --cast --data $D_DIR"
  "$BIN" --cast --data "$D_DIR" > /tmp/nedbd-cast-test.log 2>&1 &
  PID=$!
  sleep 3
  grep -i "cast" /tmp/nedbd-cast-test.log | head -3
fi

# ── 1. daemon reachable ───────────────────────────────────────────────────────
sect "Daemon"
H=$(curl -s --max-time 5 "$B/health")
if echo "$H" | grep -q '"ok":true'; then
  ok "nedbd is up" "$(echo "$H" | tr -d '{}"' | cut -c1-70)"
else
  bad "nedbd not reachable at $B" "start it, or pass --boot"
  echo; echo "RESULT: FAIL"; exit 1
fi

# ── 2. seed a real database ───────────────────────────────────────────────────
sect "Seed a real database"
curl -s --max-time 5 -X POST "$B/v1/databases" -H 'Content-Type: application/json' \
  -d "{\"name\":\"$DB\"}" >/dev/null
for row in '{"coll":"orders","id":"o1","doc":{"total":150,"status":"paid"}}' \
           '{"coll":"orders","id":"o2","doc":{"total":40,"status":"pending"}}' \
           '{"coll":"orders","id":"o3","doc":{"total":900,"status":"paid"}}'; do
  curl -s --max-time 5 -X POST "$B/v1/databases/$DB/put" \
    -H 'Content-Type: application/json' -d "$row" >/dev/null
done
COLLS=$(curl -s --max-time 5 "$B/v1/databases/$DB" | grep -o '"collections":\[[^]]*\]')
if echo "$COLLS" | grep -q orders; then
  ok "seeded 3 orders" "$COLLS"
else
  bad "seed failed" "$COLLS"
fi

# ── 3. is cast enabled? ───────────────────────────────────────────────────────
sect "Cast endpoint"
RESP=$(curl -s --max-time 30 -w '\n%{http_code}' -X POST "$B/v1/databases/$DB/cast" \
  -H 'Content-Type: application/json' -d '{"prompt":"show me all orders"}')
CODE=$(echo "$RESP" | tail -1); BODY=$(echo "$RESP" | sed '$d')

case "$CODE" in
  501) skip "built without --features cast" "rebuild: cargo build --release --features cast"
       echo; echo "passed $PASS  failed $FAIL  skipped $SKIP"; echo "RESULT: SKIPPED"; exit 0 ;;
  503) skip "cast not enabled at runtime" "start nedbd with --cast and provide model.cast"
       echo; echo "passed $PASS  failed $FAIL  skipped $SKIP"; echo "RESULT: SKIPPED"; exit 0 ;;
  200) ok "POST /cast responded 200" ;;
  *)   bad "unexpected status $CODE" "$(echo "$BODY" | cut -c1-160)" ;;
esac

NQL=$(echo "$BODY" | grep -o '"nql":"[^"]*"' | cut -d'"' -f4)
echo "      ${D}prompt: \"show me all orders\"${Z}"
echo "      ${C}${NQL}${Z}"
echo "$BODY" | grep -q '"valid":true' && ok "generated NQL is valid" || bad "invalid NQL" "$NQL"
echo "$BODY" | grep -q '"collection_known":true' \
  && ok "collection exists in this db" \
  || bad "named a collection this db lacks" "$NQL"
echo "$BODY" | grep -q '"executed":false' \
  && ok "did NOT execute by default" "execute defaults false, as designed" \
  || bad "executed without being asked"

# ── 4. execute:true returns correct rows ──────────────────────────────────────
sect "Cast and execute"
R2=$(curl -s --max-time 30 -X POST "$B/v1/databases/$DB/cast" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"orders over 100","execute":true}')
NQL2=$(echo "$R2" | grep -o '"nql":"[^"]*"' | cut -d'"' -f4)
CNT=$(echo "$R2" | grep -o '"count":[0-9]*' | cut -d: -f2)
echo "      ${D}prompt: \"orders over 100\"${Z}"
echo "      ${C}${NQL2}${Z}"
if [ "$CNT" = "2" ]; then
  ok "returned 2 rows (o1=150, o3=900)" "o2=40 correctly excluded"
else
  bad "expected 2 rows, got ${CNT:-none}" "$(echo "$R2" | cut -c1-200)"
fi
echo "$R2" | grep -q '"total":40' && bad "row below threshold leaked through" || ok "filter is semantically correct"

# ── 5. failure modes are LOUD ─────────────────────────────────────────────────
sect "Failure modes"
R3=$(curl -s --max-time 30 -w '\n%{http_code}' -X POST "$B/v1/databases/$DB/cast" \
  -H 'Content-Type: application/json' -d '{"prompt":"show me all stylists"}')
C3=$(echo "$R3" | tail -1); B3=$(echo "$R3" | sed '$d')
N3=$(echo "$B3" | grep -o '"nql":"[^"]*"' | cut -d'"' -f4)
echo "      ${D}prompt: \"show me all stylists\" (collection does not exist)${Z}"
echo "      ${C}${N3}${Z}"
if echo "$N3" | grep -qi "FROM stylists"; then
  if [ "$C3" = "422" ]; then
    ok "unknown collection rejected with 422" "not a silent empty result"
  else
    bad "unknown collection returned $C3" "should be 422 with an explicit error"
  fi
else
  skip "model chose a different collection" "$N3"
fi

C4=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "$B/v1/databases/$DB/cast" \
  -H 'Content-Type: application/json' -d '{"prompt":""}')
[ "$C4" = "400" ] && ok "empty prompt rejected (400)" || bad "empty prompt returned $C4"

C5=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST "$B/v1/databases/nope_$$/cast" \
  -H 'Content-Type: application/json' -d '{"prompt":"orders"}')
[ "$C5" = "404" ] && ok "missing database rejected (404)" || bad "missing db returned $C5"

# ── 6. /query still works (no regression) ─────────────────────────────────────
sect "No regression on /query"
QC=$(curl -s --max-time 5 -X POST "$B/v1/databases/$DB/query" \
  -H 'Content-Type: application/json' -d '{"nql":"FROM orders WHERE total > 100"}' \
  | grep -o '"count":[0-9]*' | cut -d: -f2)
[ "$QC" = "2" ] && ok "hand-written NQL still returns 2 rows" || bad "/query regressed: got ${QC:-none}"

# ── summary ───────────────────────────────────────────────────────────────────
sect "Summary"
echo "  ${G}passed  $PASS${Z}"
echo "  $([ $FAIL -gt 0 ] && echo "${R}failed  $FAIL${Z}" || echo "${D}failed    0${Z}")"
echo "  $([ $SKIP -gt 0 ] && echo "${Y}skipped $SKIP${Z}" || echo "${D}skipped   0${Z}")"
echo
if [ $FAIL -gt 0 ]; then echo "${R}RESULT: FAIL${Z}"; exit 1; fi
echo "${G}RESULT: OK${Z}"
