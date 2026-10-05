# Claude Code Notification Kit

Stop babysitting the terminal. Two hooks tell you — from the next room —
when Claude **finished** (and what it did) or **needs your input**.

- **Stop hook**: reads the hook payload, takes Claude's final reply from its
  `last_assistant_message` field (falling back to the transcript only if that
  field is missing or empty), and announces a one-line summary (max 12 words):
  *"All done, 61 tests passing, 2 files changed"*, not *"Task complete"*.
  The summary is a synthesis, not a prefix: sentences are scored by outcome
  words and numbers, and the winning outcome-bearing clauses are kept —
  even when the stats sit deep in a long final message. Markdown
  (backticks, bold, bullets) is stripped so `say` never reads it aloud.
- **Notification hook**: relays what Claude is waiting on
  (*"Claude needs your permission to use Bash"*).
- Three toggleable channels: macOS desktop notification, spoken line
  (`say`), and optional phone push via [ntfy.sh](https://ntfy.sh)
  (plain HTTP POST, no account; **off by default**).
- Distinct prefix and sound per event: ✅ + *Glass* for finished,
  ⚠️ + *Sosumi* for needs-input. The project name (from the payload's
  `cwd`) is in every title and spoken line, so concurrent sessions are
  distinguishable.
- Built-in safety: reads only the tail of huge transcripts, caps stdin,
  never invokes a shell (quotes/backticks/`$` in summaries are inert),
  and redacts anything that looks like a key or token before it reaches
  any channel.
- No dependencies beyond bash + the python3 that ships with macOS. No
  network traffic except the optional ntfy channel.

## Install

```sh
./install.sh
```

This:

1. Copies `notify-kit.py` to `~/.claude/hooks/notify-kit.py`.
2. Merges the Stop + Notification hooks into `~/.claude/settings.json`
   **without clobbering** existing settings (a timestamped backup is
   written first; existing hooks are left alone; re-running is a no-op).
3. Writes a default `~/.claude/notify-kit.conf` if none exists.

Pass a path to target a different settings file, e.g. a project's:
`./install.sh /path/to/repo/.claude/settings.json`.

Restart Claude Code (or start a new session) to pick up the hooks.

### settings.json snippet

If you'd rather wire it by hand, this is all the installer adds
(see `settings-snippet.json`):

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command",
        "command": "python3 \"$HOME/.claude/hooks/notify-kit.py\" stop" } ] }
    ],
    "Notification": [
      { "hooks": [ { "type": "command",
        "command": "python3 \"$HOME/.claude/hooks/notify-kit.py\" notification" } ] }
    ]
  }
}
```

## Configure channels

Edit `~/.claude/notify-kit.conf`:

```
DESKTOP=1        # macOS notification banner
SPEAK=1          # spoken line via `say`
NTFY=0           # phone push, off by default
NTFY_TOPIC=      # e.g. joses-claude-x7k2q9 — treat the topic like a password
NTFY_SERVER=https://ntfy.sh
```

For phone push: install the ntfy app, subscribe to your topic, set
`NTFY=1` and `NTFY_TOPIC=<your topic>`. Anyone who knows the topic name
can read it, so make it unguessable (summaries are redacted regardless).

## Test

```sh
./tests/run_tests.sh
```

Pipes saved fixture payloads (normal finish, needs-input, huge
transcript, huge raw payload, quotes/backticks/`$`, fake API keys,
missing fields, two different cwds) through the hooks in dry-run mode
and asserts on the produced notification text. No notifications fire
and nothing touches the network.

## Uninstall

```sh
./install.sh --uninstall
```

Removes the kit's entries from `~/.claude/settings.json` (only its own —
other hooks are untouched) and deletes `~/.claude/hooks/notify-kit.py`.
Delete `~/.claude/notify-kit.conf` too if you want a clean slate.
