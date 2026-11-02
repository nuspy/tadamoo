# Guidance texts: findings

Read with [inventory.md](inventory.md) (what each text is, where, how big) and [evidence.md](evidence.md) (what is known about how a Qwen-class model reads guidance, graded A to F).
Line numbers date from 1-2 November 2026 and move. Token counts use the Qwen3 tokenizer as a stand-in.

## The premise, checked

The owner's worry was that texts written for GPT or Claude style harnesses (capital-letter emphasis, names of other tools) are unproven on Qwen. Scan result: no guidance text uses MUST, NEVER, ALWAYS
or CRITICAL; no text names another harness's tools. The capital-heavy text a Qwen model sees is its own chat template (ONLY, NO, MUST and an IMPORTANT block for the call format). So the problem is
not tone; it is weight.

## Weight

- The always-on prefix of a Chat with the planner, capabilities and the build role is about 13.4k tokens: 41% of a 32k window and 21% of 65k. A bare agent is about 5.2k.
- Tool JSON is 86% of the Chat prefix (11.5k tokens for 60 tools). Our prose is 1.7k, so rewriting prose saves at most about 0.6k; trimming tools can save 2-3k.
- F1. The tool JSON is the weight. F2. The injected `description` property (`tools/registry.py`) costs about 35 tokens a tool, 2.1k in all, and is also written as output on every call; TOOL_CALL_NOTE
  already states the rule. F3. The plan family (15 `plan_*` tools plus ask_user and present_plan) is 3.6k tokens in every chat and repeats the same contract in eight tools; plan_mark carries an
  Italian example inside English text. F17. ask_user is the heaviest tool (423 tokens plus 340 of rules); its limits are stated in three places and it names the forbidden "Other option".
- F16. Long descriptions with implementation detail: run_code 606 characters, plan_mark 501, create_skill 466 (its guide is returned again on a failed save), find_online_capability 377, edit_file
  348, spawn_subagent 343, self_status 292. About 300-400 tokens could move to result or error text.

## Correctness of what the model is told

- F4 (code). `estimate_tokens` (`context.py`) counts message text only. The tool JSON and the template text are not in the budget, so "about N tokens of room" in the self block, and the mask and
  fold thresholds, are overstated by up to a third of a 32k window. Most consequential finding.
- F5. `present_plan` always says "Plan mode is on" although it is registered in every mode; its own reply says it is not on.
- F6. The self block freezes Role and Permission level at build time while both change in session; the tail role note then disagrees with it.
- F7. The self block explains the 70% and 85% thresholds, which the model cannot act on (about 70 of its 246 tokens); the course watch repeats the one actionable sentence at the right moment.
- F8. Capability counts appear twice and are dynamic text inside a cached schema, as is "Providers: duckduckgo." in search_web.
- F9. SYSTEM_CORE always names find_online_capability and MCP/skills (about 35 tokens). `prompt_blocks.py` already drops rules whose tools are absent; SYSTEM_CORE and the role texts do not.
- F11. "Autonomous coding agent" is the first line, but the Chat drives plans, phone, memory, pictures and documents. Inference, to be tested.

## Style and structure

- F10. About 25 `[harness]` event notes are user-role messages and SYSTEM_CORE explains only the time line. Whether Qwen takes them for the person speaking is untested. With `preserve_thinking` off, the
  template treats a mid-turn note as a new query and drops earlier reasoning.
- F12. Several notes are negations (GRACE_NOTE "do not call any tool", CONTINUE_NOTE "do not repeat", PROMPT_RULES with 12 negation words in 1,497 characters). In grace mode the tools stay in the
  request and code drops a call, so the text is the only soft guard.
- F13. "The person" and "the user" are both used. The owner's choice.
- F14. Good descriptions to keep as models: todo_write, edit_file, read_file, write_file, search_code. F18. Terse tools are fine: recall, list_directory, forget, job_kill, go_to_definition.
- F15. Overlapping tools lack a "when not": repo_map/tree/search_code, recall/recall_about_user/search_past, read_file/read_attachment, write_file/write_output_file/publish_output_file,
  shell_command/run_code, the capability family. repo_map and tree have near-identical use-when sentences.
- F19. The untrusted-data rule is stated about nine times. The 21-token trailer next to the data is defensible; the prefix copies add little. Any cut needs the injection suite and the owner's decision.
- F20. "Finish honestly" appears five times. F22. Side prompts are in good shape; target.py packs eight numbered rules into one line.

## Cache

F21. Cache hygiene is mostly sound. Two leaks: the Qwen template puts the reasoning-effort sentence at the very top, so a retry at a lower effort starts with different tokens; and the dynamic capability
counts of F8.

## Likely honest outcome

As in Hermes' own A/B, most prose changes will move nothing on Qwen. The measurable gain is tokens and window, not obedience.
