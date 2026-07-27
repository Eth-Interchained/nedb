# NEDB cast helpers — source this:  . ./nedb.sh
# Works in Git Bash / MINGW64. Needs curl only.

: "${NEDB:=http://127.0.0.1:7070}"   # daemon
: "${DB:=}"                          # current database

# JSON-escape stdin (backslash, quote, control chars) — prompts with
# apostrophes or quotes would otherwise produce invalid JSON.
_nedb_esc() { sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e "s/$(printf '\t')/\\\\t/g" | tr -d '\r\n'; }

nedb-dbs() { curl -s "$NEDB/v1/databases"; echo; }

nedb-use() {
  [ -z "$1" ] && { echo "usage: nedb-use <database>"; echo "have: $(nedb-dbs)"; return 1; }
  DB="$1"; export DB; echo "DB=$DB"
}

nedb-new() {
  [ -z "$1" ] && { echo "usage: nedb-new <database>"; return 1; }
  curl -s -X POST "$NEDB/v1/databases" -H 'Content-Type: application/json' \
    -d "{\"name\":\"$1\"}"; echo; DB="$1"; export DB; echo "DB=$DB"
}

cast() {
  if [ -z "$1" ] || [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    cat <<'EOF'
cast "<prompt>"          plan only  (nothing runs)
cast -x "<prompt>"       plan AND execute
nedb-dbs                 list databases
nedb-use <db>            pick one
nedb-new <db>            create one
NEDB=http://host:7070    point at another daemon
EOF
    [ -n "$DB" ] && echo "" && echo "DB=$DB" || echo "
DB is unset — run: nedb-use <name>"
    return 0
  fi
  local ex=false
  if [ "$1" = "-x" ] || [ "$1" = "--execute" ]; then ex=true; shift; fi
  if [ -z "$DB" ]; then
    echo "no database selected. try:"; echo "  nedb-use \$(nedb-dbs | tr -d '[]\"' | cut -d, -f1)"
    echo "  have: $(nedb-dbs)"; return 1
  fi
  local p; p=$(printf '%s' "$*" | _nedb_esc)
  local body; body=$(curl -s -X POST "$NEDB/v1/databases/$DB/cast" \
       -H 'Content-Type: application/json' \
       -d "{\"prompt\":\"$p\",\"execute\":$ex}")

  # Readable summary, then the raw JSON. The summary is the point: you are
  # meant to READ the NQL before trusting it. Falls back to raw if no python.
  if command -v python >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1; then
    local PY; PY=$(command -v python3 || command -v python)
    printf '%s' "$body" | "$PY" -c '
import json,os,sys
try: d=json.load(sys.stdin)
except Exception: print(sys.stdin.read()); raise SystemExit
if "nql" not in d:
    print("  error:", d.get("error","?")); raise SystemExit(1)
ok  = "yes" if d.get("valid") else "NO"
kn  = "yes" if d.get("collection_known") else "NO"
print("  nql        ", d["nql"])
print("  valid      ", ok, "   collection", d.get("collection"), "known:", kn)
if d.get("error"): print("  error      ", d["error"])
if d.get("drift"):
    # The failure valid/known cannot catch: a literal the model invented.
    print("  DRIFT      ", d["drift"])
    print("              ^ this plan may answer a different question")
if d.get("executed"):
    n = d.get("count", 0)
    rows = d.get("rows") or []
    print("  rows       ", n)
    # Hide _coll/_hash/_seq by default. A 64-char BLAKE2b hash on every line
    # buries the actual data -- and the data is what you are here to read.
    # NEDB_RAW=1 brings them back.
    raw = os.environ.get("NEDB_RAW") == "1"
    SHOW = 10
    hid = False
    for r in rows[:SHOW]:
        if not raw and isinstance(r, dict):
            trimmed = {k: v for k, v in r.items() if k == "_id" or not k.startswith("_")}
            if len(trimmed) != len(r):
                hid = True
            r = trimmed
        print("     ", json.dumps(r))
    tail = []
    if n > SHOW:
        tail.append("%d more" % (n - SHOW))
    if hid:
        tail.append("_hash/_seq hidden, NEDB_RAW=1 to show")
    if tail:
        print("      ... " + " | ".join(tail))
else:
    # Say what did NOT happen, and how to make it happen. "executed no" read
    # as a status field people skim past -- the plan looked like an answer.
    # (no apostrophes in here: this python is inside a single-quoted shell arg)
    print("  NOT RUN     this is the plan only. to run it:")
    print("                cast -x " + json.dumps(d.get("prompt","")))
'
  else
    printf '%s\n' "$body"
  fi
}
