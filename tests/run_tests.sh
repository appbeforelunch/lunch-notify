#!/usr/bin/env bash
# Test harness for the notification kit. Pipes saved fixture payloads
# through notify-kit.py in dry-run mode and checks the produced
# notification text. Run: ./tests/run_tests.sh
set -uo pipefail

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$KIT_DIR/notify-kit.py"
SRC_FIX="$KIT_DIR/tests/fixtures"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cp "$SRC_FIX"/* "$WORK/"

# Point payloads at the copied transcripts.
for f in "$WORK"/*.json; do
  sed -i '' "s|__FIXTURES__|$WORK|g" "$f"
done

# Generate the huge transcript (~20k lines, final assistant message last)
# rather than committing megabytes of fixture to the repo.
python3 - "$WORK/transcript-huge.jsonl" <<'PY'
import json, sys
with open(sys.argv[1], "w") as f:
    filler = "x" * 200
    for i in range(20000):
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": filler}}) + "\n")
    f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Refactor complete: migrated 14 modules, all 220 tests green."}]}}) + "\n")
PY

# Config with every channel on so each dry-run line is exercised.
cat > "$WORK/test.conf" <<'CONF'
DESKTOP=1
SPEAK=1
NTFY=1
NTFY_TOPIC=test-topic
NTFY_SERVER=https://ntfy.sh
CONF

PASS=0
FAIL=0

run_hook() { # $1=stop|notification $2=payload file -> stdout
  NOTIFY_KIT_DRY_RUN=1 NOTIFY_KIT_CONF="$WORK/test.conf" \
    python3 "$HOOK" "$1" < "$2"
}

check() { # $1=test name  $2=output  $3=grep pattern (extended regex)
  if grep -Eq "$3" <<< "$2"; then
    echo "PASS: $1"
    PASS=$((PASS + 1))
  else
    echo "FAIL: $1  (wanted /$3/)"
    echo "$2" | sed 's/^/       /'
    FAIL=$((FAIL + 1))
  fi
}

check_absent() { # $1=test name  $2=output  $3=forbidden pattern
  if grep -Eq "$3" <<< "$2"; then
    echo "FAIL: $1  (found forbidden /$3/)"
    echo "$2" | sed 's/^/       /'
    FAIL=$((FAIL + 1))
  else
    echo "PASS: $1"
    PASS=$((PASS + 1))
  fi
}

# 1. Normal finish: summary carries the actual outcome, finish sound/prefix.
OUT="$(run_hook stop "$WORK/stop-normal.json")"
check "normal finish: outcome in body"      "$OUT" 'DESKTOP\|✅ lunch-notify — finished\|.*61 tests passing'
check "normal finish: finish sound"         "$OUT" '\|Glass$'
check "normal finish: spoken line"          "$OUT" 'SAY\|lunch-notify finished\.'
check "normal finish: ntfy line when on"    "$OUT" 'NTFY\|https://ntfy\.sh/test-topic\|'

# 2. Needs input: different prefix + sound, message in body.
OUT="$(run_hook notification "$WORK/notification-needs-input.json")"
check "needs input: warn prefix"            "$OUT" 'DESKTOP\|⚠️ lunch-notify — needs input\|'
check "needs input: input sound"            "$OUT" '\|Sosumi$'
check "needs input: permission msg"         "$OUT" 'permission to use Bash'
check "needs input: spoken prefix"          "$OUT" 'SAY\|lunch-notify needs your input\.'

# 3. Huge transcript: still finds the final message, finishes fast.
START=$(date +%s)
OUT="$(run_hook stop "$WORK/stop-huge.json")"
ELAPSED=$(( $(date +%s) - START ))
check "huge transcript: final msg found"    "$OUT" 'Refactor complete: migrated 14 modules'
if [ "$ELAPSED" -le 10 ]; then
  echo "PASS: huge transcript: fast (${ELAPSED}s)"; PASS=$((PASS + 1))
else
  echo "FAIL: huge transcript: took ${ELAPSED}s"; FAIL=$((FAIL + 1))
fi

# 4. Huge raw payload on stdin (12 MB of junk) must not hang or crash.
python3 -c 'import sys; sys.stdout.write("{\"cwd\": \"/tmp/junk\", \"pad\": \"" + "y"*12_000_000)' > "$WORK/huge-payload.json"
OUT="$(run_hook stop "$WORK/huge-payload.json")"
check "huge payload: safe fallback"         "$OUT" 'DESKTOP\|.*\|Task finished\|Glass'

# 5. Quotes, backticks, dollar signs survive intact (argv, not shell).
OUT="$(run_hook stop "$WORK/stop-quotes.json")"
check "quotes: double quotes kept"          "$OUT" '"old name"'
check "quotes: backticks kept"              "$OUT" '`new_name`'
check "quotes: dollar sign kept literally"  "$OUT" '\$PATH'

# 6. Secrets are redacted from every channel.
OUT="$(run_hook stop "$WORK/stop-secret.json")"
check_absent "secrets: no sk- key"          "$OUT" 'sk-ant'
check_absent "secrets: no AWS key"          "$OUT" 'AKIA'
check_absent "secrets: no github token"     "$OUT" 'ghp_'
check "secrets: redaction marker present"   "$OUT" '\[redacted\]'

# 7. Missing fields: still produces a sane notification.
OUT="$(run_hook stop "$WORK/stop-missing-fields.json")"
check "missing fields: fallback body"       "$OUT" 'DESKTOP\|✅ .* — finished\|Task finished\|Glass'
OUT="$(run_hook notification "$WORK/notification-missing-message.json")"
check "missing message: fallback body"      "$OUT" 'Claude is waiting for you'

# 8. Two sessions, two cwds: each names its own project.
OUT_A="$(run_hook stop "$WORK/stop-cwd-alpha.json")"
OUT_B="$(run_hook stop "$WORK/stop-cwd-beta.json")"
check "cwd alpha named"                     "$OUT_A" '✅ proj-alpha — finished'
check "cwd beta named"                      "$OUT_B" '✅ proj-beta — finished'
check_absent "alpha not beta"               "$OUT_A" 'proj-beta'

# 9. Summary is capped at 12 words.
BODY="$(run_hook stop "$WORK/stop-normal.json" | grep '^DESKTOP' | cut -d'|' -f3)"
WORDS=$(echo "$BODY" | wc -w | tr -d ' ')
if [ "$WORDS" -le 12 ]; then
  echo "PASS: summary word cap ($WORDS words)"; PASS=$((PASS + 1))
else
  echo "FAIL: summary word cap ($WORDS words): $BODY"; FAIL=$((FAIL + 1))
fi

# 10. ntfy off by default: default config emits no NTFY line.
OUT="$(NOTIFY_KIT_DRY_RUN=1 NOTIFY_KIT_CONF=/nonexistent python3 "$HOOK" stop < "$WORK/stop-normal.json")"
check_absent "ntfy off by default"          "$OUT" '^NTFY'

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
