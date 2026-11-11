---
name: resume
description: Start the aiflow service (after a machine restart or a session restart) and show the state.
disable-model-invocation: true
---
Run `aiflow up`, then `aiflow status`. `aiflow up` starts the aiflow service (it runs the queue
separately from sessions) and adds this project to it. If the service already runs, the command
changes nothing. If `aiflow up` fails with "command not found", or the service is an old version,
run `"${CLAUDE_PLUGIN_ROOT}/bin/aiflow" up`: PATH can contain an old aiflow version.

Do not start the Monitor tool. Do not run `aiflow run --events` yourself. The user gets the
events from system notifications and the panel.

Then tell the user, in one to three lines and in the user's language, what waits for their
decision. Start nothing else.
