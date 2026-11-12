# Import: skills, plugins, MCP servers and plans as copies

TaDaMoo has no live link to the place a skill, a plugin, an MCP server or a plan came from. When it imports one, it **copies** it into its
own folder and uses the copy from then on. The original is read when you import or re-import and is never written to, so editing the
original (or its repository) changes nothing in TaDaMoo until you decide to re-import.

## Where the copies live

All under the user data folder (`TADAMOO_DATA`, else `%APPDATA%\TaDaMoo` on Windows).

| What | Where |
|---|---|
| Skills with a `SKILL.md` | `capabilities/imported/skills/<name>/` |
| Personal skills (`skill.json` + `skill.py`) | `personal-skills/<name>/` (the usual store; a re-import keeps the previous version in `.versions/`) |
| Plugins (the whole folder) | `capabilities/imported/plugins/<name>/` |
| MCP servers | `capabilities/imported/mcp/<name>.json` |
| Plans | the planner's plans, `planner/plans/<plan_id>.json` (what the Plans tab shows) |
| What was imported | `capabilities/imported/manifest.json` |

The capability index reads `imported/skills` and `imported/mcp` before anything else, so an imported copy wins over a same-named skill
or server found elsewhere. A skill folder named in *Settings > Capabilities > Skill folders* (or with `--skills`) still works: it is read
directly from where it is, which is your explicit choice; importing is the recommended way.

## What the manifest records

One entry per item (`skill:<name>`, `plugin:<name>`, `mcp:<name>`, `plan:<id>`): name, version, description, **source** (where it came
from), `imported_at`, `updated_at`, **hash** (the content of the source when it was imported), `copy_hash` (the content of the copy as
written, to see a local edit), file count and size, and, for a plugin, its `parts`. A part also records `parent: "plugin:<name>"`.

The hash of a folder covers the relative path and content of every file copied. Links and junctions are never followed (they are
listed as skipped), `.git`, `__pycache__`, `node_modules`, virtual environments and `.pyc` files are not copied, and a source over 2000
files, 100 MB or with a file over 50 MB is refused.

## The four operations

**Verify** reads the source and says what importing it would copy and register. Nothing is written.

**Import** copies the source into the global space and records it. A name that is already imported is refused (`exists`), as is one that
would overwrite something that was not imported by this service (a personal skill or a plan of the same id made in the app).

**Re-import** reads the recorded source again. Without confirmation it only returns a short summary: files added, removed, changed (with
lines added and removed in text files), and what is special to the kind. It replaces the copy only on confirmation. If the copy was
changed here since it was imported (a file edited, a personal skill saved again, a plan edited in the app), the replacement is refused
(`local_edits`) unless the person also confirms that those changes may be discarded. A source that is gone is reported
(`source_missing`) and the copy stays.

**Delete** removes the copy and its manifest entry. The source is never touched. A part of a plugin cannot be deleted alone: delete
the plugin, and its parts go with it.

Every change is one transaction: the copies are built in a work folder and moved into place, the manifest is written last, and if any
step fails everything done is undone, so a failure leaves nothing half-written. Imported code is never executed, at import or
afterwards by the import service: sources are only read, hashed and copied.

## Per kind

**Skills.** A folder with a `SKILL.md` is copied whole to `imported/skills`; the agent loads it from there. A folder with `skill.json` and
`skill.py` is saved as a personal skill (the same checks as saving one in the app). A folder with both is one skill in both places.
The skill's name is the `name` in the `SKILL.md` header when it is a safe name, else the folder's name; a name that could leave the
target folder is never used.

**MCP servers.** The source is a config file that lists servers (`.mcp.json`, `claude_desktop_config.json`, a file that is one
server's own definition); when it lists several, the person chooses one. An MCP server is a program the harness starts on this PC, so
the copy is imported **turned off** (`mcp:<name>` in the `off` list of the capability settings) and starts only after the person turns it on.
The copy keeps the command, arguments and address. Of the variables the server reads, a value that looks like a key is **not kept**:
the copy names the vault entry it is read from when the server starts (the same mechanism as servers installed from the search online).
Headers of a remote server are not copied. A re-import that changes what the server starts turns it off again.

**Plans.** The source is a plan file (a bare plan, or a plan file saved by the planner). The imported plan is an ordinary plan of the
Plans tab, with a history of its own, and is the one that runs. A schedule in the file is not imported. A re-import replaces the steps
with the source's and keeps what the plan did: its history, journal and values, and the state of every step whose definition did not
change. It asks first, and asks again when the plan was edited in the app. A plan with the same id that was not imported is not taken
over, unless the import is asked to *adopt* it (the migration does): it then becomes the imported copy as it is.

**Plugins.** A folder with a `plugin.json`. The folder is copied to `imported/plugins/<name>/`, and its parts are imported as copies in
their own places: the skills it lists (or `skills/*`), the plans (`plans/*.plan.json`), the MCP servers (`mcp_servers` or `.mcp.json`),
all registered as parts of the plugin, so deleting the plugin removes them. A part that is already imported on its own with the same
content is reused and left alone (deleting the plugin does not remove it); one imported with other content blocks the import and says
which. The skills named under `dependencies.skills` are **reported** (imported, importable at the path named, or not found) and the
dialog offers to import the importable ones in the same step; they are imported as ordinary skills and stay when the plugin goes. A
re-import of a plugin updates the parts that changed, adds new ones and removes those the plugin no longer lists.

## The page

*Settings > Models and capabilities > Imported* has one list per kind with **Import**, **Re-import**, **Delete** (and **Turn on / off** for
servers). Import asks for a path on this PC and has a **Verify** step that shows what will be copied; an MCP server is shown with the
command it will run and a warning that it is a program. Re-import shows the summary and, for a locally changed copy, a checkbox that must
be ticked to replace it. Delete says what is removed and that the source is untouched. The Plans tab has an **Import plan** button
above the plan list. Everything works on a phone: one column, 44 px targets, no hover, keyboard and screen reader friendly.

## HTTP

All routes are for the PC the server runs on (a path is a path on that PC); answers are JSON.

```
GET  /api/import                      everything imported, by kind, and where the copies live
GET  /api/import/<kind>               one kind: skill | plugin | mcp | plan
POST /api/import/<kind>/verify        {path, server?, adopt?}
POST /api/import/<kind>/import        {path, server?, with_dependencies?, adopt?}
POST /api/import/<kind>/reimport      {name, confirm?, overwrite_local?, with_dependencies?}
POST /api/import/<kind>/delete        {name}
POST /api/import/mcp/enable           {name, enabled}
```

A problem answers `{"error": {"type": "import", "code", "message", ...}}`: 404 `not_imported`, 409 `exists`, `blocked`, `local_edits`,
`part`, `source_missing`, `renamed`, 400 for the rest (`no_source`, `bad_name`, `outside`, `choose_server`, `bad_plan`...).

## Command line

`python -m tadamoo.importer [--data-dir DIR] ...` does the same without the server:

```
list [kind] | verify KIND PATH [--server NAME] | import KIND PATH [--server NAME] [--with-dependencies] [--adopt]
reimport KIND NAME [--yes] [--overwrite-local] | delete KIND NAME | enable NAME | disable NAME | migrate SKILLS_ROOT [--dry-run]
```

**migrate** moves a folder of skills that was read directly (for example `D:/skills`, with `plugins/` and `personal/` inside) to
imported copies: its skills, its plugins with their parts and dependencies, its personal skills, then it takes the folder out of
`skill_dirs` and sets aside `personal-skills-dir.json` if it pointed there. A plan the planner already has under the same id is adopted.
Run it with `--dry-run` first. The folder is not changed. Running it again imports nothing twice.

## Code

`tadamoo/importer/`: `common.py` (the scan, the hash, the transaction, the manifest), `skills.py`, `plugins.py`, `mcp.py`, `plans.py`,
`service.py` (the four operations), `migrate.py`, `__main__.py`. The routes are in `serve/import_api.py`, the page in
`serve/web/imported.js`. Tests: `tests/test_importer.py`, `tests/test_importer_migrate.py`, `tests/test_import_api.py`.

## From other code

A command that installs something (for example the newscast skills) does not copy into the settings itself; it hands a folder to the service:

```python
from serve import userdata
from tadamoo.importer import Importer, Spaces
from tadamoo.importer.migrate import migrate
imp = Importer(Spaces.from_data(userdata.user_data_dir()))
report = migrate(imp, source_folder, update=True)      # skills, plugins (with parts), personal skills; re-imports what changed
```
