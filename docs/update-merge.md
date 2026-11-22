# Updates that keep what you changed

A skill, a plugin or an MCP server that was imported or installed is a copy, and a copy is often edited here: a skill adapted to one's own needs, a plugin's default changed, a
server's setting tuned. When the source moves on, replacing the copy would lose those edits. An update therefore merges three versions instead:

| Version | Where it comes from |
|---|---|
| installed | the copy as it is now, with your changes (for a skill that is also a personal skill, the one saved in the personal skills: that is where it is edited) |
| new | the source read again |
| original | the source as it was when the copy was made or last brought up to date, kept apart and never edited |

Skills, plugins and MCP servers imported from a folder (Settings > Imported, Settings > Skills, `python -m tadamoo.importer`, `python -m tadamoo.skillmanager`) and skills installed
from GitHub (Connectors) all go through it. The source can be anything those can read: a folder, a GitHub address, a web address, the program's own folder.

## What the merge does with each file

| Outcome | When | Result |
|---|---|---|
| same | the installed copy already matches the new version | nothing |
| upstream | only the new version changed it (added, changed or removed) | the new one is taken |
| kept | only you changed it | it stays as it is |
| merged | both changed it, in different places of a text file | the two sets of changes are joined line by line |
| conflict | both changed the same place, or a file that is not text, or one side removed it and the other changed it | you choose: the installed one, the new one, or the merge once it holds no conflict marker |

The merge works **inside** the file, not only on whole files. Without the original (a copy imported before originals were kept, with edits) nothing can be told apart: a file that is
only on one side is kept or taken, and every file that differs is a conflict. A copy that was never edited uses the imported copy as the original.

A personal skill's `skill.json` is compared as its definition (name, description, parameters, tools, time limit), whatever else the store writes beside it. An MCP server is compared as
its record (command, address, variables). A plugin is merged as its folder, and each skill or server it brought is merged on its own copy. A plan is not merged: a re-import replaces its
steps and keeps what it did.

## The window

The update opens one window (`serve/web/updatemerge.js`), the same from Imported, Skills and Connectors:

- the update in plain words, written by the model when one is available (without one the window says so and the files are the update);
- every file that differs, with its three versions: installed, new, merged (tabs on a phone, side by side from 900 px);
- a choice for each conflict; the merge cannot be applied until every one has one;
- a box to tell the assistant how to change the merge;
- three ways out: **Apply the merge**, **Replace with the new version** (two presses; your changes are lost), **Cancel the update**.

Nothing is written before the person applies. The proposal is kept under a token for six hours; if the copy or the source changes meanwhile, applying is refused (`stale`) and the update
is read again.

## Telling the assistant how to change the merge

In the box, write what to change: *keep my default language*, *take the new version of this file but keep my last paragraph*. The assistant (the model the server uses for short jobs)
redoes the merge for the files the update changes, each file with the three versions and the proposal in front of it, and the window shows the result again, with a line for each file
(changed as you said, no change needed, left as proposed). A file that is not text or too long for the model is left as proposed. An answer that holds conflict markers or, for a JSON
file, is not valid JSON, is asked again once and then left as proposed.

The instructions belong to this update only: more than one can be given in a row and they add up, but nothing is saved, and the next update of the same item starts without them.
Instructions need a model: without one they are refused and nothing is proposed.

## HTTP, command line, code

```
POST /api/import/<skill|plugin|mcp>/update-plan    {name, instructions?, language?}   -> a proposal with a `token`
POST /api/import/<kind>/update-apply               {token, choice?, resolutions?}   choice: merge | overwrite | cancel
POST /api/skills/update/plan | apply            the same, by skill name (the source is the program's folder, the folder or the address it came from)
POST /api/connectors/update/plan | apply        the same for a skill installed from GitHub ({id} instead of {name}), with the model's risk review of what changes
```

`resolutions` is `{path: "ours" | "theirs" | "merged"}` (with several parts, `"unit::path"`). Errors: 409 `conflicts` (files that need a choice), `stale`, `not_mergeable`; 503 `no_model`.
Command line: `python -m tadamoo.importer update-plan KIND NAME` then `update-apply TOKEN [--resolve FILE=ours|theirs]`; the same two commands in `python -m tadamoo.skillmanager`.

The originals are in `capabilities/imported/.originals/<kind>/<name>/` (skills, plugins, MCP records) and, for skills installed from GitHub, in the Connectors originals folder (the one
chosen in Connectors > Providers, else `connectors/originals`). After a merge the original is the new source, never what was merged.

Code: `tadamoo/merge/` (`text.py` line merge, `tree.py` folders, `assist.py` the model's part, `session.py` the proposal kept under a token), `tadamoo/importer/updates.py`,
`serve/connectors_install.py`. Tests: `tests/test_merge.py`, `tests/test_importer_update.py`, `tests/test_skills_update_merge.py`, `tests/test_connectors_install.py`,
`serve/tests/test_updatemerge_page.py` (a real browser at 360x640, 390x844 and 1280x800).

## Not covered

An MCP server installed from a registry (npm, PyPI, remote) has no files of its own to merge: an update replaces it and carries its accounts and values over. An MCP server installed from a
GitHub repository is replaced whole too (its dependencies are installed again). Sources other than the ones listed above (another git host) are not read yet.
