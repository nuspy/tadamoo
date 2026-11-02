# Guidance texts: inventory

Every text the harness writes to the model, with where it is, how big it is and when it is sent. Sizes are characters / tokens. Tokens are
counted with the Qwen3 tokenizer (151,669 entries) as a stand-in for the Qwen3.8 one: close for prose (about 4.2 characters a token) and for JSON
schemas (about 4.0), but still an estimate. The Qwen3.8 chat template (`serve/chat_template.jinja`) was read, and a real agent was built in a scratch
folder (`C:\TEMP\guidance\dump.py`) to measure the system prompt and the tool list as sent. Nothing was run on the model. Other sessions are editing the working tree, so line numbers are those of 1 November 2026 and may drift.

## 1. Always in the prefix (every request, cached)

What the server renders, in order: the template's reasoning-effort sentence, the template's tool-format block, the tool list as JSON, then our system text.

- Template, effort sentence (xhigh/medium/low): about 35 tokens, at the very top. Not ours; it changes with the effort setting.
- Template, tool-format block (`<tool_call><function=...>` with ONLY, NO, MUST, `<IMPORTANT>`): 817 / 168. Not ours; it is the model's native format text.
- Tool list: see section 4. It is the largest part by far.
- `harness/stack.py:42` SYSTEM_CORE: 951 / 205. Identity, act with tools, read sources for look-ups, untrusted-data rule, secret placeholder, the [harness] time line, the missing-capability hint.
- `harness/tools/registry.py:165` TOOL_CALL_NOTE (joined to SYSTEM_CORE at `stack.py:700`): 64 / 15. The person sees your `description` of each call.
- `harness/awareness.py:28` self_model, pinned as "# About yourself": 1073 / 246. Role and level, room in tokens, masking/fold thresholds, limits, absent features, "a task is yours until done".
- `harness/prompt_blocks.py:10` RULES, "# Working rules": 918 / 220. Opt-in (`--instruction-blocks`, off by default).
- `harness/environment.py:108` environment_paragraph, "# Environment": 660 / 187. OS, shell, Python, working folder, output files.
- `harness/capabilities.py:451` top_of_tree, "# Capabilities": 208 / 66. Only with `--capabilities`.
- `harness/visuals.py:238` guidance, "# Pictures and diagrams": 1148 / 259. Only with images/charts set. `visuals.py:231` FILES_GUIDANCE, "# Files for the person": 545 / 134, only for the page-files chat.
- `harness/questions.py:321` PROMPT_RULES (ask_user rules): 1497 / 340. Appended to the system prompt when the planner is enabled (`session.py:655`).
- Optional pinned blocks whose size is the user's: project AGENTS.md (cut at 32 KiB), reference documents (CAG), the skills catalog, the vault note.

Prefix totals measured (Qwen3 tokenizer):
- Bare agent, 22 tools: system 625, tools 4,361, template block 168. About 5.2k tokens.
- Chat-like agent with planner, capabilities, past search, pictures and role build, 60 tools: system 1,331 + PROMPT_RULES 340, tools 11,530, template
  block 168 and effort sentence 35. About 13.4k tokens: 41% of a 32k window, 21% of a 65k one.
- Of that, our prose is 1.7k tokens (13%). The tool JSON is 86%. Prose rewrites cannot move the total much; the tool surface can.

## 2. Added to the end or the middle of each request

- `agent_loop.py:88` local_time_line: 110 / 43. A separate user message before each request of the person.
- `roles.py:147` RolePolicy.note, appended last, not stored (so the prefix never changes): role text 94 tokens (build), 104 (debug), 92 (plan), 53 (ask), 59 (orchestrate) at `roles.py:47-67`, plus the list of allowed tools (about 150 tokens with 45 names) and the approved checks. The general role adds nothing.
- `context.py:416` memory_message (fenced notes plus a 15-token sentence), `lessons.py:31` HEADER (21 tokens plus lines), `intake.py:311` note_text (NOTE_HEAD `intake.py:40`, then assumptions, look-first list, requirements in the person's words, dates, parts), `session.py:671` plans note, `session.py:530` pending-answer note. Sent only when they have content.
- `context.py:269` fence_untrusted adds a 21-token sentence after every fenced block (web, MCP, verify output, masked heads): about 21 tokens a result, forever in the history.
- `context.py:265` MASK_SAVED_NOTE (14 tokens) on each masked result.

## 3. On an event (a note put in the conversation when something happens)

All start with `[harness]` and are user-role messages unless stated.
- `agent_loop.py:55` BUDGET_WARNING 145 / 33 (at 90% of a budget). `:57` GRACE_NOTE 158 / 40 (budget gone: no tool, final report). `:59` TODO_NOTE 152 / 33 (open checklist items at the end). `:65` CONTINUE_NOTE 147 / 32 (reply cut by the limit). `:67` SHORT_ANSWER_NOTE 151 / 33 (thinking used the whole output).
- `agent_loop.py:1016` skipped-calls note 231 / 58. `:1026` same-error note 157 / 41 (second failure in a row). `:1302` stop-hook denial (hook text plus "Continue the work, then answer again"). `:423` mission card after a fold (code-made).
- `loop_guard.py:103,105,116,128,136`: repeat, repeat-stop, cycle, churn, failure-count notes, 31-50 tokens each, once per pattern.
- `gate.py:147` verify failure 180 / 45 plus the command output (fenced). `completion.py:30,32` NUDGE_ADMITS 190 / 47 and NUDGE_MISSING 113 / 30 ("Work until done").
- `course.py:134` fold-near note 234 / 55. `course.py:150` course check 171 / 41 (plus a lesson when one matches).
- `session.py:77` RESUME_NOTE 102 / 25, `:78` INTERRUPTED_NOTE 155 / 32, `:80` PENDING_NOTE 113 / 25, `:81` UNAVAILABLE_NOTE 102 / 21.
- Tool-result texts the model reads as guidance: role refusal (`roles.py:133`), plan-mode replies (`plan_mode.py:40-55`), ask_user results (`planner_tools.py:734`), edit_file "closest text" answers, `registry.py:277` unknown tool.

## 4. Tool definitions (in the prefix, part of the 13.4k)

60 tools in a full chat. Measured tokens: descriptions 2,603, parameter schemas 7,467 (of which the injected `description` property 2,100), name/wrapper the rest.
- `registry.py:255` injects into every tool a `description` property whose help (`registry.py:166`, 22 tokens) is the same sentence 60 times. Whole property: 35 tokens a tool, 2.1k in all. Each call also writes it as output.
- Largest tools (tokens, whole definition): ask_user 423, edit_file 413, plan_create 400, plan_mark 360, read_file 350, shell_command 315, plan_prepare_call 306, create_skill 293, search_code 289, repo_diagram 284, run_code 283.
- The 15 `plan_*` tools plus ask_user and present_plan: 3,570 tokens (31% of the schema). Registered in every chat (`session.py:656`) whether or not the user ever talks about plans.
- Capability family (find_capability, find_online_capability, call_capability, read_capability, load_capability, explain/ask_install_request, analyze_image): 1,460. search_past, open_past, recall_about_user, create_skill, self_status: 1,025.
- Descriptions by file: `harness/tools/__init__.py:47-` (read, write, edit, search, tree, shell, jobs), `todo.py:58`, `plan_mode.py:57`, `planner_tools.py` (15 tools), `memory.py:296`, `personal_memory.py:560`, `past.py:291`, `attachment_tools.py:151`, `capabilities.py:630`, `lsp.py:399`, `vision_tool.py:109`, `visuals.py:206`, `tools/extra.py:139,167`, `awareness.py:78` self_status, `personal_skills/authoring.py:38` (create_skill, description plus GUIDE 364 chars), `council.py:229`, `integrations/telephony_tools.py:87`, `integrations/wiki_tools.py:51`, `mcp_client.py`.
- Browser (only with `--browser`, `browser.py:1258-1350`): 9 tools, descriptions 29-242 characters each.

## 5. Side-session prompts (separate requests, a different prefix, mostly effort none)

Chars / tokens, file:line. Each is its own request, so none of them touches the main prefix; their cost is a cold prefill each time.
- `memory_extract.py:24` SYSTEM 2058 / 498. `compaction.py:55` SUMMARY_PROMPT 1769 / 404 (+ `compacted.py:24` KEEP_RULES 528, `:19` RULES 171). `target.py:93` JUDGE_SYSTEM 1254 / 305, `:84` DECOMPOSE 1021 / 217, `:104` REPLAN 676 / 154, `:111` APPROACH 631 / 130.
- `intake.py:183` TRIAGE_PROMPT 1103 / 236 (with a strict JSON schema). `planner/replan.py:65` 1065 / 231, `planner/alignment.py:105` 480 / 118. `personal_skills/distill.py:298` SCRIPT_RULES 1437 / 335 (+ `:168` schema, `:195` system).
- `suggestions.py:27` 764 / 181. `serve/longflow.py:69` PLAN 634 / 151, `:75` VERIFY 794 / 173, `:83` FIX 288 / 68. `serve/chatctx.py:19` SUMMARY 840 / 173, `:148` ABSTRACT 564 / 122. `digest.py:42` 522 / 125. `serve/agent_sessions.py:600` merge prompt 265 chars.
- Small judges: `completion.py:18` 666 / 145, `optimize.py:33` 538 / 115, `auto_approve.py:18` 483 / 107, `inject_screen.py:21` 442 / 102 (+ `:17` NOTICE 250), `tool_extract.py:21` 583 / 126, `lessons.py:782` 484 / 104, `router.py:36` 389 / 88, `memory_rank.py:21` 343 / 76, `progress.py:21` 342 / 80.
- `swarm/subagent.py:18` SYSTEM 329 / 72 (a subagent starts with this instead of SYSTEM_CORE). `council.py:46,49` 341 and 475 chars.
- Eval fixtures (`harness/eval/cases/fixtures/`) are test inputs, not guidance, and are left out.
