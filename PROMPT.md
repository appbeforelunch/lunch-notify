Build me a notification kit for Claude Code so I stop babysitting the terminal.
- Two hooks, wired into .claude/settings.json: a Stop hook that fires when Claude finishes, and a Notification hook that fires when Claude needs my input. Include the settings.json snippet and an install script that adds it without clobbering my existing settings.
- The Stop hook must announce WHAT happened, not just that something did: read the hook's JSON payload from stdin, pull the final assistant message, and produce a one-line summary (max 12 words) for the notification body. "Done: 61 tests passing, 2 files changed" beats "Task complete".
- Deliver on three channels, each toggleable: a macOS desktop notification, a spoken line (the say command), and an optional phone push via ntfy.sh (no account, plain HTTP POST; off by default).
- Different sound and prefix for "finished" vs "needs input", so I know from the next room which one it is.
- Handle the traps: the payload can be huge (do not choke on a long transcript); a summary can contain quotes, backticks and dollar signs (escape for the shell); two sessions can finish at once (say WHICH project, from the cwd in the payload); and never put anything that looks like a key or a token into a notification (redact it).
- No network requests except the optional ntfy channel. No dependencies beyond bash/python already on a Mac.
- Include a test file that pipes at least 8 saved fixture payloads (write the fixtures yourself: normal finish, needs-input, huge transcript, summary with quotes, summary with a fake API key to redact, missing fields, two different cwds) through the hooks and checks the produced notification text. Run the tests in the foreground and wait for them.
- Include a README with install steps and an uninstall line.
