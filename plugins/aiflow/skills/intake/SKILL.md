---
name: intake
description: Split the user's message into tasks and put them into the aiflow queue.
disable-model-invocation: true
argument-hint: <tasks in one message>
---
The user's tasks:

$ARGUMENTS

You split the message into tasks and put them into the queue. Do not read code. Do not start
agents. Reply to the user in the user's language. Keep the task `text` and `title` in the
user's language and words.

1. Run `aiflow doctor --quick`. If it prints lines with ✗, show them and stop.
   Then decide what kind of request this is:
   - **Discussion.** The user asks to discuss, not to do ("let us discuss how to …", "I have an
     idea, let us talk"). Create one task with `"kind": "discuss"`. Ask no questions, do not split
     it, and give the user's full text. An Opus session starts for the conversation. A PR comes
     only when they agree. Tell the user that the discussion session is starting and that they
     can open it with the button «Открыть сессию» in the panel when it writes its first thoughts.
     Example: `{"id": "search-design", "title": "Discuss search", "kind": "discuss", "text": "…"}`.
   - **Question.** The user asks about the code and does not ask to do something ("where is …",
     "how does … work", "why …"). Create a task with `"kind": "ask"`. A planner session answers,
     and the answer comes as an event. There is no plan until the user asks for a change.
   - **Continuation.** The message continues a recent question or discussion ("move it to …"
     after an answer about where the feature is). Do not create a new task. Run
     `aiflow amend <id> --file <file>` for that task (stage «ответ дан» or «обсуждение идеи» in
     `aiflow status`). Its session already has the context and writes the plan.
2. If a task is so unclear that it cannot be planned, ask 1 to 3 questions in one message and
   wait for the reply. If not, do not ask.
3. Decide how many tasks there are. One aiflow task is one PR: its own planner session, code
   reading, review and CI run. For a small change this fixed cost is much larger than the change.
   Thus the default is: one message is one task, also when it lists several unrelated things.
   - A list of small unrelated things (fixes, renames, small UI changes) is one task. Write its
     `text` as a numbered list, one item for each thing, in the user's words. The planner reads the code one time and all items go into one PR.
   - Split only if the user asks for separate tasks ("as separate tasks", "each in its own PR"),
     or if an item is clearly a feature of its own (a new screen, a new integration, a schema
     change). Then that item is a separate task and the small items stay together.
   - Do not split one feature into layers (config, backend, UI), variants (PostgreSQL, SQLite,
     SQL Server are one task "support for several databases"), stages, or "preparation +
     implementation". If the parts change the same files or abstractions, or one part has no
     value without the other, it is one task. The planner divides it into steps in one PR.
   - If you are not sure, make one task. If the task looks too large for one PR, still make one:
     the planner proposes a split in the plan, or moves a heavy item into a task of its own.
   Give each task an id: lowercase Latin letters, digits, hyphens, 48 characters maximum.
   Use `depends_on` only between tasks that the user listed separately, when one needs the
   merged result of the other.
4. Do not assess complexity and do not set the field `complexity`. The planner (Opus) gets every
   task first. It studies the code and decides: a plan for discussion, a delegation to the worker
   (Sonnet), or its own work. Also put "start the container" in as an ordinary task.
5. For tasks with an interface, fill in `ui`: the link to the Claude Design artboard if the user
   gave one; if not, `"нет макета"`.
6. Models and effort come from the project settings. If the user directly named a model for a
   task ("let fable write the plan", "worker on opus", "think at max"), put it into the task:
   `"planner_model"` and `"planner_effort"` for the session that plans (it also answers questions
   and holds discussions), `"worker_model"` and `"worker_effort"` for the session that implements.
   A model is `fable`, `opus`, `sonnet`, `haiku` or a full id. An effort is `low`, `medium`,
   `high`, `xhigh` or `max`. Without such a request, do not set these fields and do not select a model.
   Example: `{"id": "search", "text": "…", "planner_model": "fable"}`.
   For a task that is already in the queue, use `aiflow model <id> --planner fable`.
7. Put the tasks into the queue with one command:
   ```
   aiflow enqueue <<'JSON'
   [
     {"id": "csv-export", "title": "Экспорт отчётов в CSV",
      "text": "The full task text from the message, plus the clarifications", "depends_on": [],
      "ui": null}
   ]
   JSON
   ```
8. Tell the user in one or two lines how many tasks are in the queue. The planner decides what
   happens to them (a plan for discussion, or work immediately). If you put several things into
   one task, say so. If there are two or more tasks, say in one sentence why you split them. If the user disagrees, `aiflow cancel <id>…`
   cancels tasks that have not started; then put them in again as one task.
