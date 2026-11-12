# Skills: where they live and how they depend on each other

## Where

All skills live in **one folder of skills**, `E:\AI\skills` by default (`TADAMOO_SKILLS` or `--skills-dir` change it). Never in a temporary folder, and not only in `%APPDATA%\TaDaMoo`. The folder is named in
Settings > Capabilities (`skill_dirs`; `tadamoo/capabilities.py` `add_skill_folders` adds to the list and never replaces it), so the Capabilities page, `find_capability` and `load_capability` find every
skill in it. `E:\AI\skill` (singular) is a third-party repository and is not touched.

```
E:\AI\skills\
  media-generation\      SKILL.md, references\, scripts\     pictures, clips, the talking head          (code: tadamoo/media)
  speech-synthesis\      SKILL.md, references\, scripts\     the voice: normaliser, lexicon, takes, joins (code: tadamoo/speech)
  video-editing\         SKILL.md, references\, scripts\     virtual camera, overlays, joins, seams      (code: tadamoo/videoedit)
  news-research\         SKILL.md, scripts\research.py       the investigative procedure and its guardrails (rules: tadamoo/research)
  plugins\ai-newscast\   plugin.json, README.md, skills\ai-newscast\ (SKILL.md, skill.json, skill.py), plans\, parameters\
  personal\              the runnable personal skills (skill.json + skill.py), `personal-skills-dir.json` in the user data folder points the harness here
```

The sources are in the repository, `skills/`; `python -m tadamoo.newscast install-skill` copies them into the folder of skills, saves the runnable skill in `personal/`, names the folder in the
Capabilities settings, tells the user data folder where the personal skills are and removes the copy an earlier version put in `%APPDATA%\TaDaMoo\personal-skills`. It can be run again: it replaces what it
installed and makes no new version of a skill that did not change.

## The format of a skill

A folder with `SKILL.md` (the agentskills.io format the repository's skills use): a header with `name`, `description` (what it does and when to use it), `tags`, `requires:` (programs it needs) and, for a
skill that uses others, `depends:` (their names); then the sections: when to use it, the steps or parameters, the TaDaMoo tools it uses, limits, and the **heavy-swap behaviour** (whether it holds the GPU,
that it takes one lease for all its jobs, that a heavy job unloads the loaded model). `references/` holds the details, `scripts/` runnable helpers that call the code packages.

## The dependency rule

A skill that depends on another does not contain it: **a capability that other skills may need is developed and saved separately as its own skill and only used by the one that needs it.** Video generation
is not part of the newscast: it is `media-generation`, which any skill can use. The reusable code follows the same rule and lives in its own package with its own tests and docs: `tadamoo/media`,
`tadamoo/speech`, `tadamoo/videoedit`, `tadamoo/research`; `tadamoo/newscast` keeps what is only the newscast's (the schema of its script, the layouts and timings of its graphics, the plan of its shots, the
orchestrator). A runnable skill script that uses another skill finds it next to itself (`find_skill` in the ai-newscast script looks in the folder of skills it is installed in) and imports its script.

## Plugins

A skill that brings MCP servers or plans in TaDaMoo's format is packaged as a **plugin**: `plugin.json` (name, version, description, `dependencies` on skills, the tools it needs, `mcp_servers` (an empty
list says there are none), `plans`, `parameters`, where its output goes), `skills/`, `plans/`, `parameters/` and a README. The newscast plugin brings no MCP server; its plan template is built by
`plans/build_plan.py` with the harness's own plan code (so it is always valid) and put in the plan store by `plans/import_plan.py` through the plans API (idempotent).

## Outputs

An edition's files, work files included, go to the output folder: the `output_dir` setting, default `E:\AI\Output\newscast\<date>\`. Never a temporary folder.
