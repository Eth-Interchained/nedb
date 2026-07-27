#!/usr/bin/env bash
# Seed a `shop` database matching the schema nedb-cast-slm was trained on.
#
#   ./scripts/seed-shop.sh                 # seed 'shop' on 127.0.0.1:7070
#   NEDB=http://host:7070 ./scripts/seed-shop.sh
#   ./scripts/seed-shop.sh mystore         # different database name
#
# WHY THESE EXACT NAMES
#
# The model was trained on six synthetic domains, and `shop` is one of them:
#
#   orders     total status quantity customer placed_at discounted
#   products   price stock category rating title
#   customers  age city tier lifetime_value name
#
# Those field names, and the enum VALUES below, are in its 581-token vocabulary.
# Seed a collection called `purchases` with a `cost` field and the model will
# still say `FROM orders WHERE total > ...`, because that is what it knows. It
# is a 3.3M-parameter model, not a schema reader.
#
# So: matching these names is not cosmetic, it is the difference between a
# planner that works and one that guesses. On your own schema, expect
# `collection_known: false` and rephrase toward names it has seen.
#
# Relations (`purchased`, `reviewed`, `belongs_to`) are also from the training
# domain, so TRAVERSE works: "customers traverse purchased".
set -euo pipefail

NEDB="${NEDB:-http://127.0.0.1:7070}"
DB="${1:-shop}"
API="$NEDB/v1/databases"

C='\033[36m'; G='\033[32m'; R='\033[31m'; D='\033[2m'; Z='\033[0m'
[ -t 1 ] || { C=''; G=''; R=''; D=''; Z=''; }

die() { printf "${R}%s${Z}\n" "$*" >&2; exit 1; }

curl -sf --max-time 5 "$NEDB/health" >/dev/null 2>&1 \
  || die "no nedbd at $NEDB — start it:  nedbd --dag --cast ./data"

printf "${C}seeding %s at %s${Z}\n" "$DB" "$NEDB"

# Create the database. 409/400 = already exists, which is fine (idempotent).
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API" \
  -H 'Content-Type: application/json' -d "{\"name\":\"$DB\"}")
case "$code" in
  200|201) printf "  ${D}created database %s${Z}\n" "$DB" ;;
  *)       printf "  ${D}database %s already exists (%s)${Z}\n" "$DB" "$code" ;;
esac

n=0
put() { # put <coll> <id> <json>
  local body
  body=$(curl -s --max-time 5 -X POST "$API/$DB/put" \
    -H 'Content-Type: application/json' \
    -d "{\"coll\":\"$1\",\"id\":\"$2\",\"doc\":$3}")
  # Match "ok":true with OR without a space. Serializers differ, and matching
  # only the compact form reported every successful write as a failure -- a
  # false negative that looks exactly like a broken database.
  if printf '%s' "$body" | grep -q '"ok"[[:space:]]*:[[:space:]]*true'; then
    n=$((n+1))
  else
    printf "  ${R}put %s/%s failed:${Z} %s\n" "$1" "$2" "${body:0:120}"
  fi
}

# ── orders ── status from the trained enum: paid pending refunded shipped
#              cancelled failed. customer names from the trained pool.
put orders o1  '{"total":150.0,"status":"paid","quantity":3,"customer":"marisa","placed_at":"2026-03-04","discounted":false}'
put orders o2  '{"total":40.0,"status":"pending","quantity":1,"customer":"alex","placed_at":"2026-03-11","discounted":false}'
put orders o3  '{"total":900.0,"status":"paid","quantity":12,"customer":"priya","placed_at":"2026-04-02","discounted":true}'
put orders o4  '{"total":88.5,"status":"refunded","quantity":2,"customer":"jordan","placed_at":"2026-04-19","discounted":false}'
put orders o5  '{"total":260.0,"status":"shipped","quantity":5,"customer":"nina","placed_at":"2026-05-06","discounted":true}'
put orders o6  '{"total":15.25,"status":"cancelled","quantity":1,"customer":"sam","placed_at":"2026-05-21","discounted":false}'
put orders o7  '{"total":410.0,"status":"paid","quantity":8,"customer":"chen","placed_at":"2026-06-01","discounted":false}'
put orders o8  '{"total":72.0,"status":"failed","quantity":2,"customer":"omar","placed_at":"2026-06-15","discounted":false}'

# ── products ── category from the trained enum: shampoo tools color styling
put products p1 '{"price":24.0,"stock":120,"category":"shampoo","rating":4.6,"title":"repair mask"}'
put products p2 '{"price":18.5,"stock":0,"category":"styling","rating":4.1,"title":"clay pomade"}'
put products p3 '{"price":95.0,"stock":42,"category":"color","rating":4.9,"title":"gloss kit"}'
put products p4 '{"price":320.0,"stock":7,"category":"tools","rating":4.4,"title":"repair mask"}'
put products p5 '{"price":6.75,"stock":880,"category":"shampoo","rating":3.2,"title":"clay pomade"}'

# ── customers ── city + tier from the trained enums
put customers c1 '{"age":34,"city":"winter park","tier":"pro","lifetime_value":4200.0,"name":"marisa"}'
put customers c2 '{"age":27,"city":"orlando","tier":"free","lifetime_value":150.0,"name":"alex"}'
put customers c3 '{"age":52,"city":"winter park","tier":"enterprise","lifetime_value":4980.0,"name":"priya"}'
put customers c4 '{"age":41,"city":"maitland","tier":"trial","lifetime_value":0.0,"name":"jordan"}'
put customers c5 '{"age":63,"city":"sanford","tier":"pro","lifetime_value":2310.0,"name":"nina"}'

# ── relations ── the trained edge names, so TRAVERSE has something to walk
link() {
  curl -s --max-time 5 -X POST "$API/$DB/link" -H 'Content-Type: application/json' \
    -d "{\"frm\":\"$1\",\"rel\":\"$2\",\"to\":\"$3\"}" >/dev/null || true
}
link customers:c1 purchased orders:o1
link customers:c3 purchased orders:o3
link customers:c5 purchased orders:o5
link customers:c1 reviewed  products:p1
link customers:c3 reviewed  products:p3
link orders:o1    belongs_to customers:c1

# Verify by reading back, not by trusting the write responses.
count() {
  curl -s --max-time 5 -X POST "$API/$DB/query" -H 'Content-Type: application/json' \
    -d "{\"nql\":\"FROM $1\"}" | grep -o "\"count\"[[:space:]]*:[[:space:]]*[0-9]*" | grep -o "[0-9]*$"
}
o=$(count orders); p=$(count products); c=$(count customers)
printf "  ${G}%s writes${Z}  ${D}verified: orders=%s products=%s customers=%s${Z}\n" \
  "$n" "${o:-0}" "${p:-0}" "${c:-0}"

[ "${o:-0}" = "8" ] && [ "${p:-0}" = "5" ] && [ "${c:-0}" = "5" ] \
  || die "seed verification failed — expected 8/5/5"

cat <<EOF

${C}try these${Z}  ${D}(. ./scripts/nedb.sh && nedb-use $DB)${Z}
  cast "show me all orders"
  cast "orders with total over 100"          ${D}# name the field, not just "over 100"${Z}
  cast "paid orders"
  cast "top 3 orders"
  cast "orders sorted by total descending"
  cast "customers in winter park"
  cast "products with stock under 50"
  cast "orders grouped by status count"
  cast -x "orders with total over 100"       ${D}# actually run it${Z}
EOF
