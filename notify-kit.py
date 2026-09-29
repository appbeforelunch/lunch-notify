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


OUTCOME_RE = re.compile(
    r"(?i)\b(pass(?:ed|ing|es)?|fail(?:ed|ing|s)?|fix(?:ed|es)?|done|"
    r"complete[ds]?|finish(?:ed)?|built|creat(?:ed|es)|add(?:ed|s)|"
    r"updat(?:ed|es)|remov(?:ed|es)|delet(?:ed|es)|chang(?:ed|es)|"
    r"renam(?:ed|es)|refactor(?:ed)?|migrat(?:ed|es)|deploy(?:ed|s)?|"
    r"merg(?:ed|es)|install(?:ed|s)?|green|working|ready|resolved|"
    r"verified|tested|error(?:s)?|blocked|waiting|needs?)\b"
)
NUMBER_RE = re.compile(r"\b\d[\d,./]*\b")
FUTURE_RE = re.compile(r"(?i)^(i'?ll\b|let me\b|next\b|now i\b|to do\b|if you\b|you can\b|want me\b)")


def redact(text):
    for pat in SECRET_PATTERNS:
        text = pat.sub("[redacted]", text)
    return text


def clean_text(text):
    """Strip markdown so nothing decorative reaches a banner or gets spoken."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)   # fenced code blocks
    text = re.sub(r"^[ \t]*\|.*$", " ", text, flags=re.M)  # markdown table rows
    text = re.sub(r"^(?: {4}|\t).*$", " ", text, flags=re.M)  # indented code lines
    text = re.sub(r"^#+\s*", "", text, flags=re.M)       # headers
    text = text.replace("`", "")                          # inline code markers
    text = re.sub(r"\*\*?", "", text)                     # bold/italic markers
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.M)  # bullet markers
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)      # blockquotes
    text = re.sub(r"[|→←↔⇐⇒]", " ", text)                # stray structure/arrows
    return text


def _score(sentence, idx, total):
    # Cap each signal so number- or keyword-dense prose can't drown out
    # a short verdict sentence like "Fixed."
    score = min(3 * len(OUTCOME_RE.findall(sentence)), 6)
    score += min(2 * len(NUMBER_RE.findall(sentence)), 4)
    if idx == 0:
        score += 2
    if idx == total - 1:
        score += 1
    if sentence.endswith("?"):
        score -= 4
    if FUTURE_RE.match(sentence):
        score -= 4
    return score


def _compress(sentence, budget):
    """Keep a sentence's outcome-bearing clauses within a word budget.

    Never cuts mid-thought: a clause that doesn't fit whole is packed by
    leading comma-chunks, and if nothing fits, nothing is emitted (the
    caller falls back to a complete generic line).
    """
    s = re.sub(r"\([^)]*\)", " ", sentence)              # drop parentheticals
    s = re.sub(r"\s+", " ", s).strip().rstrip(".!?,;:")
    if not s:
        return "", 0
    clauses = re.split(r"\s+[—–-]{1,2}\s+|;\s+|:\s+|,\s+and\s+", s)
    kept, used = [], 0
    for j, clause in enumerate(clauses):
        clause = clause.strip().strip(",")
        if not clause:
            continue
        relevant = j == 0 or OUTCOME_RE.search(clause) or NUMBER_RE.search(clause)
        if not relevant:
            continue
        n = len(clause.split())
        if used + n <= budget:
            kept.append(clause)
            used += n
            continue
        # Too long as a whole: keep its leading comma-chunks that fit, stop.
        for chunk in re.split(r",\s+", clause):
            chunk = chunk.strip()
            cn = len(chunk.split())
            if not chunk or used + cn > budget:
                break
            kept.append(chunk)
            used += cn
        break
    return ", ".join(kept), used


def summarize(text):
    """Synthesize one line (max MAX_SUMMARY_WORDS words) from a whole message.

    Not a prefix truncation: sentences are scored by outcome keywords and
    numbers, the best are kept in original order, and each is compressed to
    its outcome-bearing clauses — so "61 tests passing, 2 files changed"
    survives even when it sits deep in a long final message.
    """
    if not text:
        return ""
    text = redact(clean_text(text))
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    sentences = [p.strip() for p in parts if p.strip()]
    if not sentences:
        return ""

    budget = MAX_SUMMARY_WORDS
    pieces = []
    # Short leading verdict sentences ("Fixed." "61/61 passing.") are the
    # heart of the summary: seed with them before any ranking.
    while sentences:
        lead = sentences[0]
        words = lead.split()
        if len(pieces) < 2 and len(words) <= 5 and (
            OUTCOME_RE.search(lead) or NUMBER_RE.search(lead)
        ):
            pieces.append(lead.rstrip(".!?,;:"))
            budget -= len(words)
            sentences.pop(0)
        else:
            break
    if not sentences:
        return ". ".join(pieces)

    scored = [(_score(s, i, len(sentences)), i, s) for i, s in enumerate(sentences)]
    ranked = sorted(scored, key=lambda t: (-t[0], t[1]))
    chosen = [t for t in ranked[:3] if t[0] > 0] or ranked[:1]
    chosen.sort(key=lambda t: t[1])                       # back to message order

    for _, _, sentence in chosen:
        phrase, used = _compress(sentence, budget)
        if phrase:
            pieces.append(phrase)
            budget -= used
        if budget < 3:
            break
    return ". ".join(pieces)


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
