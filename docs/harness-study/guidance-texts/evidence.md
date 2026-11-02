# Guidance texts: what others do, and what is evidence

Read on 1 November 2026, from the projects' own pages and from search results. Nothing here was run. No prompt text of another project is copied;
each source is described in our words. Where a page could not be read in full, or only a search summary was seen, it says so. A figure from a vendor's
own guide is a vendor claim for its own model, not a result for Qwen.

## 1. How other harnesses write for open-weight and Qwen models

**Qwen Code** (QwenLM/qwen-code, a fork of Gemini CLI; `packages/core/src/core/prompts.ts`, read through a summariser).
- The system prompt is built in code, not a file. Markdown headings, bold labels, bullets; `<example>` blocks; `<system-reminder>` for injected notices.
- Long: the base text is estimated at 25-30k characters, with worked examples as the biggest part. We have 32-65k of window; they assume far more.
- Pieces are conditional: guidance and examples that name a tool the session does not have are filtered out. The tool-call example set is chosen by model name: XML-style for Qwen coder models, JSON for the vision ones.
- Tone: terse, imperative, many "do not / never"; capitals only on a few safety and verification rules, not as a style.
- Lesson for us: the format of a tool call is taught by examples that match the model; guidance that names an absent tool is removed in code (we do this in `prompt_blocks.py`, not in SYSTEM_CORE or the role texts).

**Qwen-Agent** (`nous_fncall_prompt.py`). Tools listed under a "# Tools" heading inside `<tools>`, calls in `<tool_call>`, results in `<tool_response>` carried in a user message. Short sentence-case text, no capitals, no parallel-call advice (parallel calls are simply parsed).

**Qwen3 / Qwen3-Coder model cards.** Thinking models: temperature about 0.6, top-p 0.95, top-k 20, min-p 0, presence penalty only if it repeats; output budget 32k for most queries; keep only the final answers of earlier turns in the history (the shipped template does it unless `preserve_thinking` is on). Coder card: temperature 0.7, top-p 0.8, top-k 20, repetition penalty 1.05, 65k output; no system-prompt advice at all. The cards recommend Qwen-Agent for tool calls because it bundles template and parser: the format is the model's, not the prompt's.
- Our own `serve/chat_template.jinja` (read) writes its tool-format reminder with capitals and an `<IMPORTANT>` block (ONLY, NO suffix, MUST). So capitals about the call format are in the model's training distribution. Capitals about behaviour rules are not shown to be.

**OpenCode.** Model-family prompt files chosen by model id; a `qwen.txt` of about 109 lines exists, concision-focused (seen only as a diff on a mirror; the upstream directory page could not be fetched). Idea: one prompt file per family.

**Aider.** Per-model settings choose the edit format and where the system reminder goes, and flags switch two optional paragraphs (one against lazy stubs, one against changes outside the request). Only the paragraph names were confirmed on the page read; the placement of the reminder at the end of the chat is from memory and not re-checked. Idea: behaviour paragraphs on or off per model, decided by use.

**Cline / Roo Code.** Cline ships a "compact prompt" for local models that cuts the prompt by about 90% and keeps the core coding abilities, at some loss of nuance (its docs and an AMD post). A guide also warns that a small default context silently truncates Cline's large prompt: the same risk applies to a 13k prefix in a 32k window.

**Goose.** A very short template: who it is, the active extensions' own instructions, one formatting rule. No capitals. Tools travel in the tool field only.

**Hermes Agent** (`agent/prompt_builder.py`, read through a summariser). Blocks chosen by model family, qwen included in two of them: an ~800-character "act with tools, keep going" block (one capital MUST), a ~3.5k-character discipline block with XML-style sub-tags and several NEVER/ALWAYS, a ~800-character completion block, ~600 for parallel calls, ~900 for Google models. The earlier study (`hermes.md`) records that Hermes' own A/B found blocks that move nothing for some models.

**Codex CLI.** Could not read the current prompt. Secondary sources say the team removed duplicated tool instructions from the prompt once the tool is declared in the tool field, and that models differ in how much they need. Low confidence.

**Claude Code.** An extraction repository (Piebald-AI) lists about 515 prompt strings (v2.1.316): sub-agent prompts of 860-1,070 tokens, utility prompts under 100, 27 built-in tool descriptions, some large. I did not read the main prompt or the tool descriptions there. From the public record and memory (not re-verified here): harness notes travel as `<system-reminder>` blocks inside user and tool messages, and tool descriptions are long, with when to use and when not to use. Claude is trained on that convention; Qwen is not known to be. Qwen Code copied it.

**Anthropic's own guides.** Say what to do rather than what not to do; use XML tags for mixed content; put long documents first and the question last (up to 30% better in their tests); 3-5 examples; keep the prompt at the right altitude and begin minimal; for newer Claude models, soften "CRITICAL / MUST" language because it now over-triggers. For tools: describe them as to a new colleague, unambiguous parameter names, few tools that do not overlap, consolidate steps, return concise results, test with real tasks and read the failures.

**OpenAI's GPT-4.1 guide.** Declare tools in the tools field, not in the prompt (about +2% on SWE-bench Verified); put long examples in a prompt section, not in a description; repeat instructions at both ends of a long context; avoid capitals and emphasis until plain wording fails; when instructions conflict the later one tends to win; markdown first, XML also good, JSON poor for many documents. Three agentic reminders (keep going, check with tools, plan in text) raised SWE-bench by nearly 20% for that model: a vendor figure for a non-thinking GPT.

**Manus** (context-engineering post). Keep the prefix byte-stable (no second-level timestamps at the start); do not add or remove tools mid-task, mask instead; rewrite a to-do list at the end so the goal stays near the end of the context; keep failed attempts visible; vary the format of repeated steps so the model does not copy a rut.

## 2. Research on instruction style, graded

Grades: **A** replicated or measured on open models; **B** one solid study or consistent vendor guidance; **C** weak, indirect or only a summary seen; **F** folklore.

- Instruction count versus adherence (IFScale, arXiv 2507.11538, 20 models): accuracy falls as the number of simultaneous instructions grows from 10 to 500, the best model reaching about 68% at 500; earlier instructions are favoured; reasoning models hold up better but vary more. Grade B, for a keyword task. We carry roughly 30-40 rules in the main prefix, well inside the flat part: no case for cutting rules for adherence alone. The gain from shorter text is window and prefill, not obedience.
- Input length alone hurts reasoning (Levy et al. 2024, "Same Task, More Tokens"): degradation well below the technical maximum, with irrelevant padding. Grade B. It argues for dropping unused tool schemas, not for dropping rules.
- Position (Liu et al., "Lost in the Middle"): relevant content at the start or end is used better than in the middle. Later work finds the effect depends on length, and one benchmark did not reproduce it. Grade B/C. The cheap, existing mechanism is the tail note (as the role note does); whether it beats the system prompt on Qwen is open.
- Format of the prompt (He et al., arXiv 2411.10541): the same content in plain text, markdown, JSON or YAML moved a GPT-3.5 task by up to 40%; GPT-4 moved less. Grade B for GPT models. It supports testing a smaller model, not any one format. A 2026 preprint seen only in a search summary reports no reliable markdown advantage and one 35B open model preferring plain text. Grade C.
- Negation (Jang et al. 2022; later work): models do not get better at negated instructions as they scale; a 2026 paper, seen only through a news write-up, reports open models more often misreading a prohibition as permission. Grade B for the first, C for the second. A prohibition is safer as a positive instruction plus a check in code, which our harness mostly has.
- Capitals and emphasis: no controlled study of typed emphasis on open models was found. Adjacent work (stakes phrases, urgency framing across Qwen3 and Mistral models) shows wording shifts priority, not that capitals do it. Vendors advise against emphasis by default. Grade F both ways. Our own texts hardly use capitals (see findings), so the question is moot for most of them.
- Examples versus rules: vendors recommend a few diverse examples over long rule lists; Manus warns that uniform examples create ruts. Grade B (guidance), no Qwen data. Our harness uses one Good/Bad example (ask_user) and none elsewhere.
- Number and clarity of tools (RAG-MCP, arXiv 2505.03275): in a stress test, selection accuracy fell as the tool list grew, and retrieving the few relevant tools lifted it from 13.6% to 43.1%; a secondary summary says 85% to 45% from 5 to 20 tools (not verified), and an independent small test found full lists slightly more accurate. Grade B/C. Direction is consistent with Anthropic's advice on overlapping tools; the size for Qwen3.8 at IQ4_XS is unknown.
- Thinking models. Qwen3 reads the system text inside its reasoning, so a rule is applied or ignored there. Planning "in text between calls" (+4% for GPT-4.1) is probably redundant with thinking (inference, F). The Qwen3.8 template writes an effort sentence at the very top of the system message, and a different effort changes the first tokens of the prefix (read in the template). A lower-effort retry therefore cannot reuse the cache of the main request (inference from the template; depends on how the server caches).

## 3. What we can claim

- Supported: keep rules few and unambiguous; keep the prefix stable; do not describe tools the session lacks; put critical, short items last; avoid duplicate wording; measure on the target model.
- Not supported for Qwen: that capitals help or hurt; that positive wording beats negative on this model; that XML beats markdown; that a system-prompt rule is followed better as a tail note. These are the experiments in `experiments.md`.
- Inconsistent with our practice only in one respect: tool count and tool JSON are 86% of the prefix and nobody's guidance defends 60 always-on tools for a 32k model.
