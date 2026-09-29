#!/usr/bin/env python3
"""Claude Code notification kit.

Usage (wired via .claude/settings.json hooks):
    notify-kit.py stop           # Stop hook: Claude finished
    notify-kit.py notification   # Notification hook: Claude needs input

Reads the hook JSON payload from stdin, builds a short redacted summary,
and delivers it on up to three toggleable channels:
  - macOS desktop notification (osascript)
  - spoken line (say)
  - optional phone push via ntfy.sh (plain HTTP POST, off by default)

Config: ~/.claude/notify-kit.conf (KEY=VALUE), overridable with
NOTIFY_KIT_CONF. Keys: DESKTOP, SPEAK, NTFY (each 1/0), NTFY_TOPIC,
NTFY_SERVER (default https://ntfy.sh).

Set NOTIFY_KIT_DRY_RUN=1 to print what would be delivered instead of
delivering it (used by the tests). No shell is ever invoked: every
external command gets an argv list, so quotes/backticks/$ in summaries
are inert. Never exits non-zero (a hook failure must not block Claude).
"""

import json
import os
import re
import subprocess
import sys
import urllib.request

MAX_STDIN_BYTES = 10 * 1024 * 1024   # cap stdin so a huge payload can't choke us
TRANSCRIPT_TAIL_BYTES = 256 * 1024   # only read the tail of a huge transcript
MAX_SUMMARY_WORDS = 12

SECRET_PATTERNS = [
    re.compile(p) for p in [
        r"sk-[A-Za-z0-9_\-]{16,}",                                  # OpenAI/Anthropic style
        r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}",                # GitHub tokens
        r"github_pat_[A-Za-z0-9_]{20,}",
        r"AKIA[0-9A-Z]{16}",                                        # AWS access key id
        r"xox[baprs]-[A-Za-z0-9\-]{10,}",                           # Slack
        r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}",  # JWT
        r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}",
        r"(?i)\b(?:api[_-]?key|apikey|token|secret|password|passwd)\b\s*[=:]\s*\S+",
        r"\b[A-Za-z0-9_\-]{40,}\b",                                 # any long opaque blob
    ]
]


def load_config():
    cfg = {
        "DESKTOP": "1",
        "SPEAK": "1",
        "NTFY": "0",
        "NTFY_TOPIC": "",
        "NTFY_SERVER": "https://ntfy.sh",
    }
    path = os.environ.get(
        "NOTIFY_KIT_CONF", os.path.expanduser("~/.claude/notify-kit.conf")
    )
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except OSError:
        pass
    return cfg


def redact(text):
    for pat in SECRET_PATTERNS:
        text = pat.sub("[redacted]", text)
    return text


def summarize(text):
    """One line, at most MAX_SUMMARY_WORDS words, secrets redacted."""
    if not text:
        return ""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)   # drop fenced code blocks
    text = re.sub(r"^#+\s*", "", text, flags=re.M)       # markdown headers
    text = re.sub(r"[*>|]", " ", text)                   # markdown decoration
    text = redact(text)
    words = text.split()
    if not words:
        return ""
    out = " ".join(words[:MAX_SUMMARY_WORDS])
    if len(words) > MAX_SUMMARY_WORDS:
        out += "…"
    return out


def last_assistant_text(transcript_path):
    """Final assistant message text from a (possibly huge) JSONL transcript."""
    if not transcript_path:
        return ""
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            if size > TRANSCRIPT_TAIL_BYTES:
                f.seek(size - TRANSCRIPT_TAIL_BYTES)
                f.readline()  # discard the partial line at the seek point
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        msg = obj.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            joined = " ".join(t for t in texts if t).strip()
            if joined:
                return joined
    return ""


def applescript_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_quiet(argv):
    try:
        subprocess.run(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15
        )
    except Exception:
        pass


def deliver(kind, project, summary, cfg, dry_run):
    if kind == "stop":
        title = f"✅ {project} — finished"
        sound = "Glass"
        spoken = f"{project} finished. {summary}"
        ntfy_tag = "white_check_mark"
        ntfy_priority = "default"
    else:
        title = f"⚠️ {project} — needs input"
        sound = "Sosumi"
        spoken = f"{project} needs your input. {summary}"
        ntfy_tag = "warning"
        ntfy_priority = "high"

    body = summary

    if cfg.get("DESKTOP") == "1":
        if dry_run:
            print(f"DESKTOP|{title}|{body}|{sound}")
        else:
            script = (
                f"display notification {applescript_str(body)} "
                f"with title {applescript_str(title)} "
                f"sound name {applescript_str(sound)}"
            )
            run_quiet(["osascript", "-e", script])

    if cfg.get("SPEAK") == "1":
        if dry_run:
            print(f"SAY|{spoken}")
        else:
            try:
                # Feed via stdin so text can never be mistaken for flags.
                p = subprocess.Popen(
                    ["say"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                p.stdin.write(spoken.encode("utf-8"))
                p.stdin.close()
            except Exception:
                pass

    if cfg.get("NTFY") == "1" and cfg.get("NTFY_TOPIC"):
        url = cfg["NTFY_SERVER"].rstrip("/") + "/" + cfg["NTFY_TOPIC"]
        if dry_run:
            print(f"NTFY|{url}|{title}|{body}")
        else:
            try:
                req = urllib.request.Request(
                    url,
                    data=body.encode("utf-8"),
                    headers={
                        # HTTP headers must be latin-1; strip the emoji.
                        "Title": title.encode("ascii", "ignore").decode().strip(),
                        "Tags": ntfy_tag,
                        "Priority": ntfy_priority,
                    },
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                pass


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "stop"
    if kind not in ("stop", "notification"):
        kind = "stop"

    raw = sys.stdin.buffer.read(MAX_STDIN_BYTES)
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    cwd = payload.get("cwd") or os.getcwd()
    project = os.path.basename(os.path.normpath(str(cwd))) or "claude"

    if kind == "stop":
        text = last_assistant_text(payload.get("transcript_path") or "")
        summary = summarize(text) or "Task finished"
    else:
        summary = summarize(str(payload.get("message") or "")) or "Claude is waiting for you"

    cfg = load_config()
    dry_run = os.environ.get("NOTIFY_KIT_DRY_RUN") == "1"
    deliver(kind, project, summary, cfg, dry_run)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
