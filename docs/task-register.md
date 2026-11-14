# The register of open work

What the agent is carrying out **now**, so that it can resume it, organise it and always know what is done and what is missing, without relying on the conversation (which
can be compacted, cleared or lost). It is **not a history**: a finished task is closed, and closing deletes it (with its subtasks). Nothing of a closed task is kept.

## Where it lives

One register for each project folder: `<project>/.tadamoo/tasks.json`. The Chat's own folder has one too. The Tasks tab reads every folder the person added, so all projects
are seen together. The agent (its tools, from the conversation's own process), the person (the tab, `/tasks`) and the server write the same file, one at a time (a lock in the
process and a lock file across processes), the whole file replaced atomically. `tadamoo/taskboard.py` is the one store of tasks: there is no other journal. The agent runs,
the target loop and the parts of a large request report to it too (below).

A task has: `id`, `title`, `description`, `state` (`queued`, `running`, `waiting` for the person, `blocked`), `parent` (subtasks to any depth, at most 10 levels), `order` among its
siblings, `done` (what is done so far), `missing` (what is still missing), `links` (`session`: the conversation that carries it; `plan`; `files`), who changed it last (`by`:
`agent` or `user`) and when.

## Plans, and who owns a task

A task belongs to the conversation that carries it (`links.session`): that conversation is told only its own open tasks. Another conversation takes one over with
`task_resume` (it becomes theirs).

Where a plan exists, **the plan is the register**. `task_link_plan` makes a task one line for a plan: its subtasks are dropped, and from then on its state (running, waiting for
the person, blocked), its progress ("3/10 steps") and what is next are read from the plan's file each time, never stored. A plan's nodes are never copied into the register,
and a plan that has finished (or is gone) takes its line with it. A task that a plan commands takes no subtasks and no state of its own.

## What reports to it

Programs that carry out work say how it goes with `TaskBoard.report(id, status, result, children, metadata)` (over HTTP: `POST /api/tasks`, which names the folder in `project`; a folder that
is not one the person added has no register of its own, and its work goes in the one kept in the data folder, listed as "Other" while it holds something). The reporter chooses the id.

| Reported | In the register |
|---|---|
| `running`, `in_progress` | the task is `running`; `result` is "done so far" |
| `pending`, `queued` | `queued` |
| `waiting`, `blocked` | `waiting` (for the person) or `blocked` |
| `failed` | `blocked`, and `result` is what is missing (the reason) |
| `done`, `closed`, `completed`, `skipped` | the task is **deleted** with its subtasks; ending one that is not there is not an error |
| `metadata.title` or `goal` | the title; the other keys are kept in `meta` |
| `children` | ids of its subtasks, which are put under it whenever they exist, whichever was reported first |

An **agent run** is one task (`agent-<run>`) in the register of its folder: its progress is what is done so far, a run that ends well deletes its task, one that fails or is killed stays
blocked with the reason until someone deletes it, and one stopped from the page is deleted. A **target** reports each of its tasks and sub-tasks (`tg-<target>-<task>`, `kind: target`); the
**parts of a large request** (`req-<turn>`) are the conversation's own subtasks. The steps of a **plan** are never reported: the plan is its own register. `GET /api/tasks` lists every open task of every
folder, flat, with `state` (also as `status`), `children`, `metadata` and the project.

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
