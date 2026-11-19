# Planner: a complex task — the plan in a draft PR

The planner reads this file only when it decides that the task needs a plan.
The result is a draft PR with a plan. Keep the section headings exactly as in the template.
Write the full plan in English: models read it many times, and English text is shorter for them.
The PR title is in English too. State the task in your own English words: do not quote the task
text in its original language. Keep only user-interface strings in their original language.
The report summary stays in the language of the task text: the user reads it in the panel.

## Branch, draft PR, report

1. Create the branch and the draft PR. The session is already in the task worktree
   (`.claude/worktrees/<id>`). Work only there. Do not go to the main repository folder.
   Release the branch at the end:
   ```
   git fetch origin
   git switch -c ai/<id> origin/<base branch>
   git commit --allow-empty -m "plan: <id>"
   git push -u origin ai/<id>
   gh pr create --draft --base <base branch> --head ai/<id> --title "<short title>" --body-file <file>
   git switch --detach
   ```
   Write the description file in `$CLAUDE_JOB_DIR/tmp/`. The last command is mandatory:
   the worker uses this branch in its own worktree.
2. Write the notes for the worker (section "Notes for the worker") to a file in `$CLAUDE_JOB_DIR/tmp/`.
3. Report, then end your turn:
   `aiflow report <id> --role planner --status plan_ready --pr <number> --complexity simple|complex --summary "<one line>" --details-file <notes file>`
   `--complexity` is your estimate of the implementation, the same value as in the last line of
   the plan. It selects the worker's model: `simple` — the plan leaves only routine work;
   `complex` — the implementation itself is hard (subtle logic, concurrency, a large refactoring,
   many connected changes). A plan is necessary in both cases.
   If you cannot plan the task (a contradiction, no access, a human decision is necessary),
   report `--status blocked` and give the cause in the summary.
4. In your last message, do not say that the plan waits for approval. The user can read your
   message in the task chat before aiflow asks for approval. Say that the plan is written and that
   aiflow now has it reviewed by another model, and that the request to approve the plan comes
   after that review. The same applies each time you report `plan_ready` again.

## Size and level of the plan

The plan records decisions. It is not the implementation written in prose.
- The user reads the plan before approval. The worker reads the code itself and writes the code.
- Write what must be true after the work and each decision that the worker must not make
  differently: contracts, data rules, behaviour, names that other code or the user sees.
- Do not write what the worker finds when it opens the file: code, SQL, the list of call sites,
  line-by-line edits, line numbers.
- Keep the plan below about 8000 characters. A larger plan usually means implementation detail
  or a task that needs a split.

## PR description template

```markdown
<!-- ai-plan v1 -->
## Goal and scope
What is included. What is not included (non-goals).

## Acceptance criteria
- [ ] items that the reviewer and CI can check to decide "done"

## Files
| File | Change |
| --- | --- |
| `path/to/file.ext` | what changes, in one phrase |

Group files that change in the same way into one row (a folder or a glob).

## Interfaces and data
Only new and changed contracts: API, database schema, formats, public signatures.
Name the fields and the rules. Do not write the code.

## Edge cases
Only the cases where the correct behaviour is a decision. One line for each.

## Tests
Which behaviours get a test, one line for each. The check commands (build, linter, tests).

## UI
Only for tasks with an interface: the link to the artboard in the Claude Design project,
one line "what we implement", the mandatory states (empty, loading, error).

## Risks and overlaps
Files that other tasks can also change.

## Implementation order
Three to six steps in the order of work in this PR, one line for each. After each step, the
build and the tests are green.
If the task is clearly too large for one PR (more than about 1500 diff lines, or two or more
independent parts), say so here and propose a split into tasks: name each part in one line and
say which part needs which. The user decides. Until the user agrees, do not split the task and do
not put tasks into the queue. When the user agrees, obey the section "Split into several PRs".

## Options considered
What was rejected and why: one line for each option. Leave the section out if there was no
real alternative.

## Plan change history
- <date>: initial version

## Implementation estimate: simple | complex
```

## Notes for the worker

The worker starts with an empty context. Without notes it repeats your search. The notes are for
the worker only: they do not go into the PR. Write what you learned about the code and what the
plan does not say. Do not repeat the plan. Write the notes in English.

```markdown
Base commit: <output of `git rev-parse origin/<base branch>`>

## Code map
- `path/to/file.ext:120-180` — what is there and why it matters for this task

## Facts
Signatures, data formats, and library or API facts that the subagents found. Give the source.

## Commands
The build, lint and test commands that work in this project, as you ran them or as CLAUDE.md gives them.

## Pitfalls
What looks right but is wrong, and what must not be touched.
```

Keep the notes short: one line for each location. Do not paste file contents: the worker reads
the files that it changes itself. When you revise the plan and the notes become wrong, write
them again and give `--details-file` in the new report. A report without `--details-file` keeps
the old notes.

## Plan check

Before the user sees the plan, aiflow can have it reviewed by another model. Then aiflow wakes you
with the reviewer's findings. This happens one time. The review is advisory: the user decides.

1. Check each finding against the code. The reviewer can be wrong.
2. A finding that is right: fix the plan (`gh pr edit <number> --body-file`).
3. A finding that is wrong: do not change the plan. Say why in your reply.
4. A `question` is a decision for the user. Do not make it yourself: write it in the plan, in the
   section that it concerns, so that the user sees it.
5. Add one line to "Plan change history".
6. Reply in the PR with one comment. Its first line is `<!-- aiflow:planner -->`. Then one line
   for each finding: "fixed: how" or "disagree: why".
7. Report `--status plan_ready` again and end your turn. Give `--details-file` only if the notes
   for the worker changed.

## UI tasks

The design system is in `design/` and `components/ui/`.
- If the task has an artboard in Claude Design, refer to it.
- If there is no design and the Claude Design tools are available (`/design` or the Claude Design
  MCP), make a mockup with the design system and refer to it in the section "UI".
- If the tools are not available, say so in the section "UI" and in the report summary:
  a human will make the mockup.

## Split into several PRs

The user agreed to your proposal of a split, or asks for a split (a PR comment, an amendment, a
message in the chat). Do this in one turn:
1. This task stays the first part. It keeps its id, its branch and this PR. Select as the first
   part the one that the other parts need.
2. Rewrite the plan in this PR to the first part only. In "Goal and scope", list the other parts
   as non-goals and name their task ids.
3. Put the other parts into the queue with one command, a JSON list on stdin:
   `aiflow enqueue --split-of <this task id>`
   `[{"id": "<new id>", "title": "…", "text": "…", "depends_on": ["<id>"]}, …]`
   - `text`, in the user's language: what this part must do, then the decisions of your plan that
     concern this part (contracts, names, behaviour, files). The planner of that part starts
     without your context and must not make these decisions again.
   - `depends_on`: the parts that must be merged first. Give this task's id if the part needs the
     first part, the id of another part if it needs that one, and an empty list if the part is
     independent. A part starts when all the tasks in its `depends_on` are merged.
   - Do not give `ui` or the model fields: aiflow copies them from this task.
4. Add a line to "Plan change history": the split and the ids of the parts.
5. Report `--status plan_ready` again. Name the ids of the parts in the summary. In your last
   message, tell the user the parts, their order, and that each part gets its own plan and PR.

If the user decides to keep one PR, record that in "Plan change history" and change nothing else.

## Plan revision

When aiflow wakes you with comments:
1. Update the PR description (`gh pr edit <number> --body-file`).
2. Add a line to "Plan change history".
3. Reply in the PR with one comment. Its first line is `<!-- aiflow:planner -->`.
4. Report `--status plan_ready` again. Give `--details-file` only if the notes for the worker changed.

A user amendment (`[aiflow] The user amended task …`) can come during your work: after a tool
call or before the end of the turn. Include it in the plan in the same way as a PR comment.
