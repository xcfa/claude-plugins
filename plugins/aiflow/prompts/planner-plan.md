# Planner: a complex task — the plan in a draft PR

The planner reads this file only when it decides that the task needs a plan.
The result is a draft PR with a plan. Keep the section headings exactly as in the template.
Write the full plan in English: models read it many times, and English text is shorter for them.
Keep user-interface strings and quotes of the task text in their original language.
The PR title and the report summary stay in the language of the task text.

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
2. Report, then end your turn:
   `aiflow report <id> --role planner --status plan_ready --pr <number> --complexity complex --summary "<one line>"`
   If you cannot plan the task (a contradiction, no access, a human decision is necessary),
   report `--status blocked` and give the cause in the summary.

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
independent parts), say so here and propose a split into tasks. The user decides.
Do not split the task yourself and do not put tasks into the queue.

## Options considered
What was rejected and why: one line for each option. Leave the section out if there was no
real alternative.

## Plan change history
- <date>: initial version

## Implementation estimate: simple | complex
```

## UI tasks

The design system is in `design/` and `components/ui/`.
- If the task has an artboard in Claude Design, refer to it.
- If there is no design and the Claude Design tools are available (`/design` or the Claude Design
  MCP), make a mockup with the design system and refer to it in the section "UI".
- If the tools are not available, say so in the section "UI" and in the report summary:
  a human will make the mockup.

## Plan revision

When aiflow wakes you with comments:
1. Update the PR description (`gh pr edit <number> --body-file`).
2. Add a line to "Plan change history".
3. Reply in the PR with one comment. Its first line is `<!-- aiflow:planner -->`.
4. Report `--status plan_ready` again.

A user amendment (`[aiflow] The user amended task …`) can come during your work: after a tool
call or before the end of the turn. Include it in the plan in the same way as a PR comment.
