#!/usr/bin/env bash
# Install (or uninstall) the Claude Code notification kit.
#
#   ./install.sh                 # install into ~/.claude/settings.json
#   ./install.sh path/to/settings.json
#   ./install.sh --uninstall     # remove hooks + script
set -euo pipefail

KIT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_DEST="$HOME/.claude/hooks/notify-kit.py"
SETTINGS="${1:-$HOME/.claude/settings.json}"

if [[ "${1:-}" == "--uninstall" ]]; then
  SETTINGS="${2:-$HOME/.claude/settings.json}"
  python3 - "$SETTINGS" <<'PY'
import json, sys, os
path = sys.argv[1]
if not os.path.exists(path):
    print(f"{path} not found; nothing to do")
    sys.exit(0)
with open(path) as f:
    settings = json.load(f)
hooks = settings.get("hooks", {})
for event in ("Stop", "Notification"):
    entries = hooks.get(event, [])
    entries = [
        e for e in entries
        if not any("notify-kit.py" in h.get("command", "") for h in e.get("hooks", []))
    ]
    if entries:
        hooks[event] = entries
    else:
        hooks.pop(event, None)
    settings["hooks"] = hooks
with open(path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
print(f"Removed notify-kit hooks from {path}")
PY
  rm -f "$HOOK_DEST"
  echo "Removed $HOOK_DEST"
  exit 0
fi

mkdir -p "$(dirname "$HOOK_DEST")" "$(dirname "$SETTINGS")"
cp "$KIT_DIR/notify-kit.py" "$HOOK_DEST"
chmod +x "$HOOK_DEST"
echo "Installed hook script -> $HOOK_DEST"

# Merge hooks into settings.json without clobbering anything already there.
python3 - "$SETTINGS" <<'PY'
import json, os, sys, time
path = sys.argv[1]
settings = {}
if os.path.exists(path):
    with open(path) as f:
        settings = json.load(f)
    backup = f"{path}.bak.{int(time.time())}"
    with open(backup, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print(f"Backed up existing settings -> {backup}")

hooks = settings.setdefault("hooks", {})
for event, arg in (("Stop", "stop"), ("Notification", "notification")):
    cmd = f'python3 "$HOME/.claude/hooks/notify-kit.py" {arg}'
    entries = hooks.setdefault(event, [])
    already = any(
        "notify-kit.py" in h.get("command", "")
        for e in entries for h in e.get("hooks", [])
    )
    if not already:
        entries.append({"hooks": [{"type": "command", "command": cmd}]})

with open(path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
print(f"Wired Stop + Notification hooks into {path}")
PY

if [[ ! -f "$HOME/.claude/notify-kit.conf" ]]; then
  cat > "$HOME/.claude/notify-kit.conf" <<'CONF'
# Claude Code notification kit — 1 to enable a channel, 0 to disable.
DESKTOP=1
SPEAK=1
# Phone push via ntfy.sh (no account needed). Set NTFY=1 and pick a
# hard-to-guess topic, then subscribe to it in the ntfy app.
NTFY=0
NTFY_TOPIC=
NTFY_SERVER=https://ntfy.sh
CONF
  echo "Wrote default config -> $HOME/.claude/notify-kit.conf"
fi

echo "Done. Restart Claude Code (or start a new session) to pick up the hooks."
