# Skills: install, remove, turn on and off

What is installed is the person's choice, made in one place and reachable three ways. All three call the same code, `tadamoo/skillmanager/`; copies are made by the importer
(`docs/import.md`), which the manager only drives.

| Where | How |
|---|---|
| The app | *Settings > Skills*: one card for each skill, with its state, what it needs, what it downloads, and a button for each action |
| A conversation (Chat, agent sessions) | `/install-skill` draws the same panel in the conversation. Words: `/install-skill` or `list`, `/install-skill <address or a built-in name>`, `/install-skill on\|off\|remove\|update <name>`. Without a conversation it opens *Settings > Skills* |
| A shell | `python -m tadamoo.skillmanager list \| plan \| install \| remove \| enable \| disable \| update \| models` |

`skills/builtin.json` is only the data of what ships with the program (name, kind, title and summary in English and Italian, what it requires, whether the setup ticks it, about how
many GB its models weigh). Nobody edits it to choose: no command installs, imports or downloads a skill that was not asked for. Installing one skill brings only what it requires.

## The states

A skill is **not installed**, **installed** (on) or **installed and off**. The choice is in `skills/choices.json` of the user data folder, one entry for each installed skill (a
skill that is not installed has none): its kind, its source (`builtin`, `folder`, `github` or `web`, with the address and, for a repository, the folder in it), whether it is on, the
version and content hash it was installed with, when, and what is known about its models (`ready`, `missing`, `unknown`). The manager reads the importer's records and the places
that hide a skill each time and makes the file agree with them: a skill imported before the manager existed is taken in as installed, one removed in the Imported tab is dropped,
and a skill turned off in *Settings > Capabilities* shows as off here.

**Off** means the harness no longer sees it: its id (`skill:<name>`) goes in the `off` list of the capability settings, which the capability index already honours (it is left out of
every search and cannot be loaded), and a personal skill of the same name has its folder renamed with a `.off` ending, out of the list of personal skills. Nothing is deleted and
nothing is downloaded again. A plugin that is turned off also turns off its MCP servers; turning it on does not start them (a server is a program, started by its own switch). Its
plans stay in the Plans tab. **On** puts both back. An update while a skill is off leaves it off.

**Remove** takes out the imported copy (a plugin's parts with it, its skills and plan) and the choice. The models are never deleted from here: they belong to the program that runs
them (LTX Studio), and the answer says so, or says that the skill has none of its own. A skill that others require is removed all the same, and the answer names them.

## Installing

Installing is the copy and then the models. For a skill with models the card says before the button how much it downloads ("about 60 GB"); the sizes are the catalogue's
`download_gb`, counted once when two skills share a model. The models are downloaded by `tadamoo/media/components.py` (`install`: only what is missing, LTX Studio's own
downloader). The work runs as a **job**, one at a time: a step counter, the skill, the last lines the installers print, and a Stop button. A job that stops keeps what it did.
The setup (`TaDaMooSetup.exe`) runs `install ... --no-models` so that it only copies; the models come later from the tab (*Download the models*) or `models NAME`.

An address can be:

- a **folder** on this PC: a skill, a plugin, or a folder holding several (a cloned repository: the folders with a `SKILL.md` or a `plugin.json`, four levels down, are listed);
  it is read where it is;
- a **GitHub address**: a repository, `/tree/<branch>/<folder>` or `/blob/<branch>/<folder>/SKILL.md`. The archive of the branch is downloaded (a branch with a slash is found by
  trying the splits) and unpacked without links or paths that leave it;
- a **web address**: a `SKILL.md`, a `.zip`, or a page that links to `SKILL.md` files (on GitHub or beside it). A page that only **describes** a skill is turned into a `SKILL.md` by
  the active model (the same client the other short jobs of the server use); the text is shown, and nothing is imported until it is accepted. The page is given to the model as data;
  a page that describes nothing a model can turn into a skill, or no model, is told in words.

The address is read first (`plan`): the skills found, their size, whether each is already installed, what the model wrote. Nothing is installed by reading. For an address that is not a
folder, the chosen skill's folder is kept in `skills/sources/<name>/` so that `update` can read it again; `update` re-reads the program's folder, the folder it came from, or the address
(a skill the model wrote is installed again from the address instead, not updated). The **Update** button of the tab (and `/install-skill update NAME`) opens the update that keeps what
was changed in the copy: the three versions of each file, a choice for each conflict, instructions to the assistant (`docs/update-merge.md`). The plain `update` replaces the copy,
and refuses one edited here unless `overwrite_local`.

## HTTP

Only the PC the server runs on may use these (an address may be a folder of that PC, and an installation runs programs on it).

```
GET  /api/skills                  every skill, its state, size and models; the job; where things are kept
GET  /api/skills/job              the job: state, phase, step, current skill, lines, result
POST /api/skills/plan             {address} -> {token, candidates}      nothing is installed
POST /api/skills/install          {names, models?} | {token, only?, models?}   starts the job
POST /api/skills/models           {name}      starts the job that downloads the missing models
POST /api/skills/models/check     {name}      reads the disk; downloads nothing
POST /api/skills/remove | enable | disable    {name}
POST /api/skills/update           {name, overwrite_local?, check?}
POST /api/skills/discard          {token}
POST /api/skills/job/cancel
```

Errors are `{"error": {"type": "skills", "code", "message"}}`: 404 `not_imported`, `unknown_skill`, `not_installed`, `no_plan`; 409 `busy`, `local_edits`, `blocked`,
`not_updatable`; 502 `fetch_failed`; 503 `no_model`; 400 the rest.

## Code

`tadamoo/skillmanager/`: `catalogue.py` (the list that ships), `state.py` (the choice and the two places a skill is hidden), `sources.py` (folders, GitHub, the web, archives),
`describe.py` (a page to a `SKILL.md`), `jobs.py`, `manager.py`, `__main__.py`. API `serve/skills_api.py`, tab and command panel `serve/web/skills.js`, command in
`serve/web/slash.js`. Tests: `tests/test_skillmanager.py`, `tests/test_skills_api.py`, `serve/tests/test_skills_page.py` (a real browser at 360x640, 390x844 and 1280x800),
`serve/tests/test_slash_commands.py`.
