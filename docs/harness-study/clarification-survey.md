# Clarifying questions: how other agents ask (survey)

Research for `ask_user` v2 and next-prompt suggestions. Read-only study of primary sources and the cloned harnesses; the design that follows from it is in [ask-user-v2.md](ask-user-v2.md). "Verified" means read in source or primary docs.


Scope: read-only research. Nothing in E:\Projects\basalt was changed. "Verified" means I read the source or primary doc myself. "Unverified" is listed in section 0.

0. HOW SURE I AM (read this first)
Verified in primary sources:
- Claude Agent SDK page "Handle approvals and user input" (code.claude.com/docs/en/agent-sdk/user-input): AskUserQuestion schema and limits.
- Claude Code "Interactive mode" docs: next-prompt suggestions.
- MCP spec 2025-11-25, elicitation page.
- Gemini CLI docs/tools/ask-user.md (via a summariser).
- Source of Roo Code, Hermes, OpenClaw and Goose, read from the local clones.
- Our own code.

Not verified, and treated as hints only:
- Claude Code's own system-prompt text for AskUserQuestion. The "put the recommended option first and add (Recommended)" convention is implemented by both the Hermes and OpenClaw clones, so it is clearly copied from Claude Code. I did not see the original wording.
- OpenAI Codex CLI `request_user_input`: only community posts and fork commits. It is Plan-mode only unless a hidden flag `default_mode_request_user_input` is set.
- Cursor "Questions" in Plan mode: only forum posts and the blog. I saw no schema.
- Cline's parameters: its docs page was empty on this. I read Roo Code instead, which is a Cline fork.
- Kiro: blog and doc summaries only (2-4 up-front questions in Quick Spec, streamed questions in "Analyze requirements").
- OpenAI Deep Research's clarifying step: secondary sources only.
- ChatGPT follow-up suggestions: secondary sources only. Perplexity related questions and Gemini suggestions: not researched. I do not describe them.
- Papers: abstracts only (Ambig-SWE arXiv 2502.13069, "Ask or Assume?" arXiv 2603.26233, ClarEval 2603.00187, HumanEvalComm 2406.00215, ClarifyCodeBench 2607.00711). I did not read the full texts. I did not verify the ClarifyCodeBench figures quoted in a blog.
- Goose: I found no dedicated ask tool, only MCP elicitation plumbing (crates/goose/src/elicitation.rs, action_required_manager.rs). Its timeouts were not confirmed.
- Nothing about local Qwen behaviour is measured. All "a Qwen-class model will..." statements are expectations to test with `python -m tadamoo.eval`, not results.

1. WHAT WE HAVE TODAY (verified in the code)
- `ask_user(question_text, options[{label,value}])` is in `tadamoo/planner_tools.py:729-756`. Limits: 6 options, label 60 characters, value 300, question 600.
  - The tool puts the question to the person and calls `agent.request_end_turn()`. The turn ends with `stop_reason "asked"`.
  - The button text, or whatever the person types, becomes their next user message. The model is not called again until then.
  - It is registered only in conversations that have the planner (`Session.enable_planner`, `tadamoo/session.py:565`), so only server Chat/Agent sessions, not the plain headless CLI or ACP.
- Unattended: `Session._question` (`session.py:490`) emits `auto_answered` with an empty value. The tool then returns "Nobody can answer in this run: decide with what you know". There is no recommended-default pick today.
- The blocking path already exists for confirmations, but not for ask_user:
  - `Session._ask` (`session.py:406`) puts the question in a slot, waits under `clock_paused()` with no timeout, and returns the value to the tool.
  - It validates the answer against the option values, or a sentence after `free_prefix` of at most 500 characters.
  - It has the policy wait / approve / unavailable / park (`_policy`, `question_source`).
  - `question_source` already reads `facts.involves` (people, booking, resource) to name the park exceptions.
- Server: `serve/agent_sessions.py`.
  - `ASK_FIELDS` is id, mode, kind, question, options, facts, tool, arguments, free, free_prefix, explain.
  - A confirm-mode ask sets `rec["ask"]` (state "asking"). A question-mode ask sets `rec["question"]` and is not waiting in the process.
  - `answer()` (line 1383) validates against `ask.options` and the 500-character prefix rule, then writes `{"cmd":"answer"}` to the process.
  - Parked questions (`pending`) are answered whenever, via `_answer_pending`.
- GUI: `serve/web/agentchat.js` is shared by the Chat tab and the Agent tab (`inAgent()`).
  - `ask` events become a block `{t:"ask", mode, kind, options}`.
  - Question mode (line 620-636) draws the question plus one `.btn` per option. A click calls `answer()` which, for mode "question", does `send(value)`.
  - There is already a form widget for MCP elicitation (lines 541-601): fields, a Send button, Decline/Cancel, answer as `elicit:` + JSON of at most 500 characters, validated by `tadamoo/mcp_elicit.py`.
  - A capability_choice widget (602-619) already has an "Other" input.
  - The `tool_start`/`tool_end`/history code hides ask_user cards (lines 356, 364, 411).
  - CSS is `.aask*` in `serve/web/app.css:953-966`. Buttons use `--btn-h: 30px`, inputs 44px.
- i18n: `serve/web/i18n.js` has an EN block and an IT block. `serve/tests/test_web_static.py` checks key sets (e.g. the `ch.q.` list at line 40), placeholder parity, and that EN differs from IT.
- Mobile: `mobile/lib/src/chat_model.dart` (Block with `options`, `answer()` posts `/api/agent/sessions/$sid/answer`) and `chat_screen.dart:_ask` (a Card with a Wrap of OutlinedButtons). It also ignores ask_user tool cards (chat_model.dart 274, 279, 350). Texts are in `mobile/lib/src/i18n.dart` as [it, en] pairs.
- Tool-call JSON can already be grammar-constrained. `serve/api/grammar.py` sends a grammar for any request with tools when the server option is on, or when the tool's function has `strict: true`. A one-shot JSON call with `response_format json_schema` is used in `tadamoo/planner/replan.py:62`. So both the question payload and the suggestions can be made parse-proof. The engine's actual grammar subset (pattern, maxLength, oneOf) is not verified. Keep the schema simple: enums, minItems/maxItems, short strings.
- Existing studies (`docs/harness-study/vibe-coding.md`) already plan "Plan asks at most three questions as buttons". No other study covers clarification.

2. (a) SURVEY: HOW OTHER SYSTEMS ASK

Claude Code / Agent SDK AskUserQuestion (verified)
- Payload: `questions[]` with 1 to 4 items. Each item has `question` (full text), `header` (short chip, max 12 characters), `options[]` (2 to 4, each `{label, description}`), `multiSelect` (bool).
- Optional `preview` per option: markdown or HTML mockup, only when the host opts in with `toolConfig.askUserQuestion.previewFormat`. The SDK rejects script/style in HTML.
- "Other" is automatic: the host shows an extra free-text row. The text is returned as the value, not the word "Other".
- Answer: `{questions, answers}` where `answers` maps question TEXT to the selected label, an array or comma-joined string for multi-select, or the typed text. An optional `response` is a free reply that dismisses the card; the model then sees "The user responded: ...".
- Blocking: the `canUseTool` callback pauses execution and "can stay pending indefinitely". For long waits the docs suggest a PreToolUse `defer` so the process can exit and resume. There is no default timeout in the doc.
- Not available in subagents.
- When to ask: "when Claude needs more direction on a task with multiple valid approaches". Especially common in plan mode.
- Note the weak point: answers are keyed by question text, which is fragile.

Prompt suggestions in Claude Code (verified)
- A grayed-out ghost text in the empty input box. It appears at session start (from git history) and after a reply (a next step or natural continuation).
- Tab or Right-arrow puts it in the box, Enter submits it, typing dismisses it.
- Generated by "a short background request to the same model your session is using", counted against usage. It reuses the prompt cache, so it is "mostly cache reads plus a few output tokens".
- Skipped when: the cache is cold, the previous response errored, in plan mode, near the usage limit, and in some sessions after the first turn.
- Backs off: "Showing fewer prompt suggestions" when many are left unused. Off via /config, a setting, or an env var.
- Users report that discoverability is poor (an issue asks for an inline Tab hint). Source is a community issue, not official.

Roo Code (read in source, `src/core/...`)
- The tool `ask_followup_question{question, follow_up[1..4]{text, mode|null}}`. It is `strict`, so its schema is grammar-friendly.
- The description says suggestions must be "complete, actionable answers without placeholders". The prompt rules say 2-4 suggested answers, ordered by priority, and "if you can use the available tools to avoid having to ask the user questions, you should do so". Also "Do not ask for more information than necessary" and "Your goal is to try to accomplish the user's task, NOT engage in a back and forth conversation".
- A missing required tool parameter is the main trigger: "if one of the values for a required parameter is missing, DO NOT invoke the tool and instead ask".
- The `mode` field lets a suggestion switch mode (e.g. "implement it now" switches to code).
- Return: `<user_message>text</user_message>`.
- UI (`FollowUpSuggest.tsx`): one full-width button per suggestion. Click sends it; Shift-click or a hover icon copies it into the box. There is an optional auto-approve of the FIRST suggestion after a countdown (default 60 s, off unless "always allow follow-up questions" is on). The countdown pauses when the user types and shows a visible timer.

Hermes Agent `clarify` (source, `tools/clarify_tool.py`) - the closest design to ours
- `questions[1..5]`, each `{question, choices[<=4], multi_select}`. No choices means open-ended. The UI appends "Other (type your answer)".
- The first choice is the recommended one. The harness appends "(Recommended)" and strips it from the answer.
- Rules in the tool description: put every INDEPENDENT question in one call; ask separately if one answer changes another; options go only in `choices`, never enumerated in the question text; prefer deciding low-stakes questions yourself; do not use it for dangerous-command confirmation.
- Result JSON: `{responses:[{question, choices_offered, status: answered|skipped|unanswered, user_response}], outcome: submitted|cancelled|timed_out|undelivered, notice}`.
- Timeout: 3600 s default, 0 means unlimited. It was raised from 600 s because "a late tap landed on a dead entry".
- Headless: the system prompt says "Do not call clarify... there is no live user", and tells the model to comment and block the task instead.
- Prompt blocks:
  - `<act_dont_ask>`: act on an obvious default interpretation; "Only ask for clarification when the ambiguity genuinely changes what tool you would call."
  - `plan_prompt.py`: ask a brief question only if "genuinely underspecified".
- Surfaces: CLI; gateway buttons with text-reply interception; ACP, where `elicitation.form` capable editors get a form (string enum, or array of enum for multi) and others get a permission card with the choices plus Skip.

OpenClaw `ask_user` (source, `src/agents/tools/ask-user-tool-normalization.ts`, `tool-description-presets.ts`)
- It is the Claude Code design: `questions[1..3]`, each `{id (snake_case), header (<=12), question (single sentence), options[2..4]{label, description}, multiSelect}`. `isOther` is always true. `timeoutSeconds` is 30 to 3600, default 900.
- Semantic checks: duplicate ids, duplicate labels, one option only, more than 4 options.
- Policy text: "Use only when blocked on a decision genuinely theirs that cannot be resolved from the request, code, or sensible defaults; never ask whether to proceed or confirm a plan. Ask exactly one question per call unless several answers must be submitted together... Put the recommended option first and suffix its label with (Recommended). Do not include an Other option... If the result is no_answer, continue with best judgment."
- The result is per question id, as a list of values. Phases (reserved, registering, prompting, answerable) support delivery to messaging channels.

Gemini CLI `ask_user` (primary doc)
- `questions[1..4]`, each `{question, header (<=16), type: choice|text|yesno (default choice), options[2..4]{label (1-5 words), description}, multiSelect, placeholder}`. An "All the above" option is added automatically for multi-select. The dialog pauses until answered or dismissed.
- Returns `{"answers": {"0": "Option A", "1": "text"}}` keyed by position.
- It has a first-class text type and yes/no type, which maps to our kind free/confirm.

Cursor / Codex / Cline / Kiro (secondary, see section 0)
- Cursor Plan mode asks clarifying questions with pickers before writing the plan. A forum bug shows multi-select questions auto-advancing after ONE click, which is an anti-pattern for multi-select. The picker disappeared for one model, so the agent asked in prose.
- Codex `request_user_input` is Plan-mode only by default. Its plan prompt says no questions in free text, only via the tool, and never mix a call with plan content. A TUI change added "Other/None". Users say it rarely calls the tool unless AGENTS.md tells it to.
- Kiro: Quick Spec asks about 2-4 questions up front, specific to the detected stack. "Analyze requirements" streams questions as they are found.

MCP elicitation, spec 2025-11-25 (verified, and we already implement the client side)
- `elicitation/create{mode: form|url, message, requestedSchema}`.
- The form schema is a flat object of primitives only: string (minLength, maxLength, pattern, format email/uri/date/date-time), number/integer (min, max), boolean, enum (`enum`, or `oneOf` with `const`+`title`), multi-select (`array` of enum, minItems/maxItems), and `default` on all of them (clients SHOULD pre-fill). No nesting.
- Response: `action: accept (+content) | decline | cancel`. Cancel means dismissed without a choice (Esc or closed dialog). Decline is an explicit no.
- Client MUSTs:
  - Make clear WHICH server asks.
  - Offer clear decline and cancel.
  - Let the user review and edit before sending.
  - Never ask for secrets in form mode (use URL mode).
- SHOULDs: validate responses against the schema, rate-limit, and do not render URLs as clickable in form fields.
- Useful for us: the tri-state outcome and the review-before-send rule. Our `ask` should use the same three outcomes.

Deep Research / ChatGPT / papers
- Deep Research: a clarifying step before long work, because wrong assumptions are costly. Details unverified.
- Open WebUI (primary docs): follow-up chips come from the "task model" after each reply. By default a click sends; a setting "Insert Follow-Up Prompt to Input" puts it in the box. The first one also shows as ghost text (Tab inserts). There is a per-user toggle and an admin template. This is the closest local-model precedent for suggestions.
- Ambig-SWE (abstract): models "struggle to distinguish between well-specified and underspecified instructions"; asking improves results by up to 74% on underspecified tasks.
- "Ask or Assume?" (abstract): a scaffold that separates detecting underspecification from executing reached a 69.40% resolve rate, and asked few or no questions on simple tasks and more on complex ones.
- ClarEval: agents struggle most with ambiguous terminology and use inefficient questioning.
- Reading: the model's own judgement of when to ask is the weak link. Both over-assuming and over-asking happen. Hence harness-side nudges (section 4).

3. (b) BEST PRACTICES AND ANTI-PATTERNS
Practices that appear in several systems:
- Ask only for decisions that are the user's and that tools cannot answer (files, search, git) (Roo, Hermes, OpenClaw).
- Batch independent questions into one call; sequence dependent ones (Hermes, OpenClaw). Hard caps: 3 to 5 questions, 2 to 4 options.
- Options are mutually exclusive, complete, without placeholders. The recommended option comes first and carries a visible, non-colour "Recommended" tag. A free-text "Other" is always available and added by the harness, not by the model.
- A short chip/header plus a full question plus a one-line description per option (Claude Code, Gemini).
- A tri-state outcome (answered, declined/skipped, cancelled/timed out) with a plain-language note to the model. The model must be told when the user did not answer.
- Never use the question tool for risky-action permission (Hermes, MCP rules). Never ask for secrets in a form.
- Headless runs never block: tell the model, or park it (Hermes, ours).

Anti-patterns to avoid:
- Options written into the question text (dead prose, unclickable).
- "Do you want me to proceed?" and plan-confirmation questions (OpenClaw forbids them).
- A chain of one-question calls where one form would do.
- A model-added "Other/None" option (duplicates the automatic one).
- A multi-select that advances after one click (Cursor bug).
- Keying answers by question text or label (Claude Code): use ids.
- Silent timeouts: a late tap on a dead card (Hermes found this at 600 s).
- Suggestions that auto-send (the user loses control, and a model-written message could be steered by page content).
- Ask loops: the same question asked again after being answered.
- An always-on questioning style. Roo and Hermes both add "do not engage in back and forth".
- Colour-only "recommended" marks (our accessibility rule).

