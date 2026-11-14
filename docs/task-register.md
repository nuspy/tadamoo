# The register of open work

What the agent is carrying out **now**, so that it can resume it, organise it and always know what is done and what is missing, without relying on the conversation (which
can be compacted, cleared or lost). It is **not a history**: a finished task is closed, and closing deletes it (with its subtasks). Nothing of a closed task is kept.

## Where it lives

One register for each project folder: `<project>/.tadamoo/tasks.json`. The Chat's own folder has one too. The Tasks tab reads every folder the person added, so all projects
are seen together. The agent (its tools, from the conversation's own process), the person (the tab, `/tasks`) and the server write the same file, one at a time (a lock in the
process and a lock file across processes), the whole file replaced atomically. `tadamoo/taskboard.py` is the one store; `serve/tasks.py` (`TaskJournal`) is something else, the
progress of an agent *run* that the Monitor shows, and is not the register.

A task has: `id`, `title`, `description`, `state` (`queued`, `running`, `waiting` for the person, `blocked`), `parent` (subtasks to any depth, at most 10 levels), `order` among its
siblings, `done` (what is done so far), `missing` (what is still missing), `links` (`session`: the conversation that carries it; `plan`; `files`), who changed it last (`by`:
`agent` or `user`) and when.

## Plans, and who owns a task

A task belongs to the conversation that carries it (`links.session`): that conversation is told only its own open tasks. Another conversation takes one over with
`task_resume` (it becomes theirs).

Where a plan exists, **the plan is the register**. `task_link_plan` makes a task one line for a plan: its subtasks are dropped, and from then on its state (running, waiting for
the person, blocked), its progress ("3/10 steps") and what is next are read from the plan's file each time, never stored. A plan's nodes are never copied into the register,
and a plan that has finished (or is gone) takes its line with it. A task that a plan commands takes no subtasks and no state of its own.

## What the agent is told, and when

Not the whole register on every turn (that would break the cached start of the request). The conversation gets a short list of its own open tasks (the top ones, their subtasks
down to two levels, one line each) **once at its start and again after a compaction**, as a `[harness]` note before the person's message, like the plans summary. After that, one line
(`Your open tasks were changed outside you: task_list shows them now.`) only when someone else changed them (the person in the tab, another conversation); the agent's own changes
are not news to it. The tools are in the session's own group of the tool list, after the fixed ones, so adding them moves nothing that is cached; the fixed "what you are" block
says when to use them: for long work (several steps, or work that can be interrupted), not for a short question.

Tools: `task_add` (with subtasks), `task_update` (state, text, done, missing, files), `task_subtasks` (the subtasks become exactly this list: rewrite them freely when one is no
longer current, is wrong or has to be done another way), `task_move`, `task_close`, `task_list` (`mine` or the whole `project`), `task_next`, `task_resume`, `task_link_plan`.
Resume hands the agent the task with its subtasks, notes and files, and it continues from what is missing; it works after a restart, a cleared conversation or in a new one.

## The person's side

- **Tasks tab** (`serve/web/tasks.js`): a tree of the open tasks of every project (filter by project and state), the ones waiting for the person highlighted with a count; edit title,
  description, state and notes, add a task or a subtask, move up and down, delete (a question first, saying what goes with it), **Resume in chat** (opens a conversation in that
  project with the words that make the agent take the task over; for a plan line, resume the plan). Everything the person changes is marked as theirs and the agent is told once.
- **`/tasks`** in the Chat and agent sessions: the open tasks of the project, this conversation's marked; `/tasks add <title>`; `/tasks resume <id>` asks the agent to take it
  over. With no conversation open it opens the tab.
- Shell: `python -m tadamoo.taskboard [--project FOLDER] list | add TITLE | update ID [--state --done --missing --title] | close ID`.
- HTTP (`serve/register_api.py`): `GET /api/register?project=<folder>|all&state=`, and `POST /api/register/add | update | delete | move | subtasks | link-plan | resume`. Only
  the folders the person added (and the Chat's) can be written.

Code: `tadamoo/taskboard.py`, `tadamoo/task_tools.py`, the notes in `tadamoo/session.py` (`_tasks_note`), `/tasks` in `tadamoo/commands.py`. Tests: `tests/test_taskboard.py`,
`tests/test_task_tools.py`, `tests/test_tasks_command.py`, `tests/test_register_api.py`, `serve/tests/test_tasks_page.py` (a real browser at 360x640, 390x844 and 1280x800),
`serve/tests/test_slash_commands.py`.
