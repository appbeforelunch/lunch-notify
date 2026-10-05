The Stop banner sometimes shows the wrong message. A logged live test of the installed kit (2026-09-29 04:00 to 2026-09-30 18:00 ET, banner only) found that 39 of 231 Stop notifications announced something other than Claude's actual final reply: either the reply from the turn before, or a mid-task line like "Now I'll rebuild the app:". The final reply was always in the hook payload; the kit read the transcript file instead.

The official hooks docs say to do it the other way (Hooks reference, "Stop input" section, https://code.claude.com/docs/en/hooks):

> "For hooks that act on the just-completed turn, such as read-aloud or notification hooks, use this field rather than reading `transcript_path`: the transcript file isn't guaranteed to include the final message at Stop time on all versions."

"This field" is `last_assistant_message`.

Fix it:
- In the Stop hook, use the payload's `last_assistant_message` first. Fall back to reading `transcript_path` only when `last_assistant_message` is missing, not a string, or empty/whitespace-only. Everything after that (summary, redaction, channels, sounds) stays as it is.
- Add fixtures to tests/fixtures and checks to tests/run_tests.sh:
  - Stale transcript, previous reply: the payload's `last_assistant_message` is the real final reply, and the transcript's last assistant message is a different, older reply. The summary must come from `last_assistant_message`, and nothing from the older reply may appear in the output.
  - Stale transcript, interim line: same, but the transcript ends on a short mid-task line ("Now I'll rebuild the app:"). `last_assistant_message` must win.
  - Missing `last_assistant_message`: the key is absent, and the transcript has the final reply. The summary must come from the transcript (fallback).
  - Empty `last_assistant_message`: the key is present but "". Same expectation as missing.
- Every new fixture uses `/Users/demo/` paths for `cwd` and anything else path-like. No real usernames or home paths anywhere in the fixtures.
- Run the full test suite (./tests/run_tests.sh) in the foreground and wait for it. All old tests must still pass, plus the new ones. Report the pass/fail count.
- Then replay every real transcript on hand through the Stop hook in dry-run mode (NOTIFY_KIT_DRY_RUN=1): lunch-notify/session-1.jsonl, session-2.jsonl, session-3.jsonl, and ../lunch-unitprice/session-1.jsonl and session-2.jsonl. Replay each one twice: once with only `transcript_path` (the fallback path), once with `last_assistant_message` set to that transcript's final assistant message (the primary path). Both runs must produce the same summary. Show me every produced line.
- Do not run install.sh and do not touch anything under ~/.claude. Do not change anything else in the kit.
