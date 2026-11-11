# Planner: a question about the code and a discussion of an idea

The planner reads this file when the first message starts with `aiflow question` or
`aiflow discussion`. In these modes, do not create a branch, a PR or files until the work
becomes a plan. When it becomes a plan, obey the plan rules (the first message gives their path).
Reply to the user in the language of the task text.

## Questions

The first message starts with `aiflow question`. The user asks about the code
("where is …", "how does … work").
1. Give a direct answer with paths and line numbers.
2. Report `--status answered`. Put a short answer in the summary and the full answer in `--details-file`.
3. End your turn.

If the user continues and asks for a change, write a plan and report
`--status plan_ready --complexity …`. If the user asks a new question, answer it and report
`--status answered` again. No reply from the user is a normal result: the question closes
automatically, without a plan.

## Discussion mode

The first message starts with `aiflow discussion`. The user wants to discuss how to do the work
first. This is a conversation in your session, not a plan.
- Study the code and the documentation only as much as you need for a specific conversation.
  Send documentation questions to the `aiflow:researcher` subagent. The start of the first
  message tells you how to search the code; if it tells you nothing, obey the project's
  CLAUDE.md. Read the decisive files yourself.
- Tell the user: how you understand the task, the options with their advantages and
  disadvantages, your recommendation, your questions. Then end your turn and wait for the reply.
- Do not create a branch, a PR or files until the user clearly says that you agree.
- Finish in one of the ways from the first message:
  - a plan, then `--status plan_ready --complexity simple|complex`;
  - two or more tasks through `aiflow enqueue`, then `--status done`;
  - only `--status done` with the result of the discussion.
