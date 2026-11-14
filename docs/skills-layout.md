# Skills: where they live and how they depend on each other

## Where

The skills that ship with the program are written in the repository's `skills/` folder, with the list `skills/builtin.json`. The harness does not read that folder: the skill manager
(`docs/skills-manager.md`) installs what the person chooses by making copies of it with the importer (`docs/import.md`) in `%APPDATA%\TaDaMoo\` (the skills in the capabilities space, the
personal skills in `personal-skills\`, the plans in the planner), and the harness runs from the copies. A skill is installed, turned off or removed in Settings > Skills, with
`/install-skill`, or with `python -m tadamoo.skillmanager`; nothing is installed that was not asked for. Skills from elsewhere (a folder, a GitHub address, a web page) are installed the
same way. Never in a temporary folder. `E:\AI\skill` (singular) is a third-party repository and is not touched.

```
skills\  image-generation\   SKILL.md, scripts\                  pictures from words                          (code: tadamoo/media)
  image-editing\      SKILL.md, scripts\                  edits and restyles from reference photographs (code: tadamoo/media)
  video-generation\   SKILL.md, references\, scripts\  clips from words or a picture, smoother and larger copies (code: tadamoo/media)
  talking-head\       SKILL.md, references\, scripts\  a still that speaks a recording                (code: tadamoo/media)
  video-modification\ SKILL.md, references\, scripts\  changes what a video shows: swap an object, edit by words, restyle (code: tadamoo/media)
  speech-synthesis\   SKILL.md, references\, scripts\  the voice: normaliser, lexicon, takes, joins  (code: tadamoo/speech)
  video-editing\      SKILL.md, references\, scripts\  virtual camera, overlays, joins, seams       (code: tadamoo/videoedit)
  news-research\      SKILL.md, scripts\research.py       the investigative procedure and its guardrails (rules: tadamoo/research)
  plugins\ai-newscast\  plugin.json, README.md, skills\ai-newscast\ (SKILL.md, skill.json, skill.py), plans\, parameters\
  builtin.json           what ships: the data of the list the manager offers (docs/skills-manager.md)
```

Installing the newscast brings the skills it requires (a plugin names them in `dependencies`, and the catalogue says the same in `requires`); each is a separate skill that can also be installed
alone, and its models are downloaded with it.

## The format of a skill

A folder with `SKILL.md` (the agentskills.io format the repository's skills use): a header with `name`, `description` (what it does and when to use it), `tags`, `requires:` (programs it needs) and, for a
skill that uses others, `depends:` (their names); then the sections: when to use it, the steps or parameters, the TaDaMoo tools it uses, limits, and the **heavy-swap behaviour** (whether it holds the GPU,
that it takes one lease for all its jobs, that a heavy job unloads the loaded model). `references/` holds the details, `scripts/` runnable helpers that call the code packages.

## The dependency rule

A skill that depends on another does not contain it: **a capability that other skills may need is developed and saved separately as its own skill and only used by the one that needs it.** Video generation
is not part of the newscast: it is `video-generation`, which any skill can use. The reusable code follows the same rule and lives in its own package with its own tests and docs: `tadamoo/media`,
`tadamoo/speech`, `tadamoo/videoedit`, `tadamoo/research`; `tadamoo/newscast` keeps what is only the newscast's (the schema of its script, the layouts and timings of its graphics, the plan of its shots, the
orchestrator). A runnable skill script that uses another skill finds it next to itself (`find_skill` in the ai-newscast script looks in the folder of skills it is installed in) and imports its script.

## Plugins

A skill that brings MCP servers or plans in TaDaMoo's format is packaged as a **plugin**: `plugin.json` (name, version, description, `dependencies` on skills, the tools it needs, `mcp_servers` (an empty
list says there are none), `plans`, `parameters`, where its output goes), `skills/`, `plans/`, `parameters/` and a README. The newscast plugin brings no MCP server; its plan template is built by
`plans/build_plan.py` with the harness's own plan code (so it is always valid) and put in the plan store by `plans/import_plan.py` through the plans API (idempotent).

## Outputs

An edition's files, work files included, go to the output folder: the `output_dir` setting, default `E:\AI\Output\newscast\<date>\`. Never a temporary folder.
