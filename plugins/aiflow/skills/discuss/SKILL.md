---
name: discuss
description: Discuss and refine the plan of an aiflow task in a fresh session, from the PR description and comments.
disable-model-invocation: true
argument-hint: <PR number or task id>
model: opus
---
We discuss the plan: $ARGUMENTS

1. If a task id was given, get the PR number from `runs/<id>/task.json`.
2. Read the plan and the discussion: `gh pr view <number> --comments`. Read code only at specific
   points, when you need it to answer the user's question.
3. Discuss the plan with the user, in the user's language. Propose specific changes.
4. When the user agrees: update the PR description (`gh pr edit <number> --body-file`; write the
   file in a temporary directory), add a line to "Plan change history", and add a comment whose
   first line is `<!-- aiflow:planner -->` with a short "what changed and why".
5. Only the user approves the plan: with the button in the panel, the label `plan-approved`, or
   the command `! aiflow approve <id>`. Do not add the label yourself.
