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

# A --boot run uses a private port so it cannot silently talk to a nedbd that
# was already running. That exact confusion produced an impossible result on the
# first real run: "cast DISABLED" alongside a 200 with generated NQL, because
# the boot instance lost the port race and every request went to another daemon.
PORT="${NEDB_PORT:-7070}"
B="${NEDB_URL:-http://127.0.0.1:$PORT}"
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
  PORT=$(( 17070 + (RANDOM % 2000) ))
  B="http://127.0.0.1:$PORT"
  echo "booting $BIN --cast --port $PORT --data $D_DIR"
  "$BIN" --cast --port "$PORT" --data "$D_DIR" > /tmp/nedbd-cast-test.log 2>&1 &
  PID=$!
  sleep 3
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "${R}the daemon exited immediately. Log:${Z}"
    cat /tmp/nedbd-cast-test.log
    exit 1
  fi
  grep -iE "cast|listen" /tmp/nedbd-cast-test.log | head -4
  # Record whether THIS daemon has a model, so later checks can tell the
  # difference between "cast is off" and "cast answered anyway".
  if grep -q "cast     enabled" /tmp/nedbd-cast-test.log; then
    BOOT_CAST=1
  else
    BOOT_CAST=0
  fi
fi
BOOT_CAST="${BOOT_CAST:-unknown}"

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
CREATED=$(curl -s --max-time 5 -w '\n%{http_code}' -X POST "$B/v1/databases" \
  -H 'Content-Type: application/json' -d "{\"name\":\"$DB\"}")
CC=$(echo "$CREATED" | tail -1)
echo "      ${D}create db -> $CC $(echo "$CREATED" | sed '$d' | cut -c1-90)${Z}"

# Never discard the write response. Swallowing it is why an empty database
# looked like a mystery instead of an error message.
for row in '{"coll":"orders","id":"o1","doc":{"total":150,"status":"paid"}}' \
           '{"coll":"orders","id":"o2","doc":{"total":40,"status":"pending"}}' \
           '{"coll":"orders","id":"o3","doc":{"total":900,"status":"paid"}}'; do
  PR=$(curl -s --max-time 5 -w '\n%{http_code}' -X POST "$B/v1/databases/$DB/put" \
    -H 'Content-Type: application/json' -d "$row")
  PC=$(echo "$PR" | tail -1); PB=$(echo "$PR" | sed '$d')
  if [ "$PC" != "200" ]; then
    echo "      ${R}put -> $PC $(echo "$PB" | cut -c1-140)${Z}"
  else
    echo "      ${D}put -> 200 $(echo "$PB" | grep -o '\"_id\":\"[^\"]*\"' | head -1)${Z}"
  fi
done
# Verify by querying the data back. The collections list is a weaker signal --
# it was empty on a run where the writes had actually landed, which turned a
# real problem into a confusing one.
SEEDED=$(curl -s --max-time 5 -X POST "$B/v1/databases/$DB/query" \
  -H 'Content-Type: application/json' -d '{"nql":"FROM orders"}' \
  | grep -o '"count":[0-9]*' | cut -d: -f2)
if [ "$SEEDED" = "3" ]; then
  ok "seeded 3 orders" "verified by reading them back"
else
  bad "seed failed" "FROM orders returned ${SEEDED:-nothing}; every later check depends on this"
  echo
  echo "${R}Aborting: without seed data the remaining assertions are meaningless.${Z}"
  echo "passed $PASS  failed $FAIL  skipped $SKIP"
  echo "RESULT: FAIL"
  exit 1
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
  200)
    if [ "$BOOT_CAST" = "0" ]; then
      bad "IMPOSSIBLE: daemon logged 'cast DISABLED' but /cast returned 200" \
          "you are almost certainly talking to a DIFFERENT nedbd. Check: curl $B/health"
    else
      ok "POST /cast responded 200"
    fi ;;
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
#
# NOTE on what "correct" means here. Observed on a real run:
#
#   prompt  "paid orders over 100"
#   nql     FROM orders WHERE status = "paid" LIMIT 100     <- WRONG
#   right   FROM orders WHERE status = "paid" AND total > 100
#
# The model read "over 100" as LIMIT 100 and dropped the second predicate. The
# row COUNT still came out at 2, because both paid orders happen to exceed 100 —
# a count-only assertion would have called that a pass. So this section checks
# the returned rows satisfy the filter, not just how many came back. Multi-
# predicate WHERE is the model'"'"'s known weak clause (85.1% eval / 61.2% holdout).
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
