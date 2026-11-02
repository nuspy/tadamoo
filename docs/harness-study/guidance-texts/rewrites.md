# Guidance texts: candidate rewrites

Ranked. Nothing here is applied. Each item: now, proposed, effect, risk, how to test (experiment ids are in [experiments.md](experiments.md)). Wording is described, not drafted, so that the final text is written
once the experiment says it is worth it.

1. Injected `description` property (F2). Now a 22-token help sentence on each tool. Proposed: none, or nine tokens ("what this call does, for the person"); TOOL_CALL_NOTE takes the language rule.
   Effect: -0.8k to -1.3k tokens, possibly shorter outputs. Risk: the model omits the sentence or makes it long; display-only, so safety is unaffected. Test: G1 arms B and C.
2. Plan family diet (F3, F17). Say the contract once, drop the Italian example, trim ask_user's field descriptions. Effect: -1.0k to -1.5k tokens. Risk: medium; planner semantics are subtle and there are
   Italian users. Test: G1 arm C plus planner_chat.
3. `present_plan` state-neutral (F5): "presents a plan for approval when plan mode asks for one". Effect: removes a contradiction. Risk: very low. Test: G4.
4. (Code) Count the prefix in the budget (F4): subtract tool-schema tokens, the template block and the effort sentence from the room, then re-derive the thresholds. Effect: a true number; masking and folding
   start when they should. Risk: changes behaviour at 32k, needs the owner's say. Test: compare `prompt_tokens` with the estimate on 20 runs.
5. Self block (F6, F7): drop the Role and Permission lines; replace the threshold sentence with "put what must survive in notes or the plan; old results become a file marker". Effect: about -80 tokens and
   the contradiction gone. Risk: low. Test: G1 non-inferiority.
6. `[harness]` convention (F10): one SYSTEM_CORE sentence saying that `[harness]` lines come from the program and not the person, to be applied as working instructions and not answered as a request.
   Effect: +30 tokens. Risk: low. Test: G2 probe P4.
7. Positive event notes (F12): GRACE "reply in plain text only; say what is done, what is not, the next step"; CONTINUE "continue from the last word written"; verify "fix the cause in the code, then answer
   again". Effect: small. Risk: low. Test: G2.
8. "When not" lines for confusable tools (F15): 12-20 tokens per pair; reword the repo_map/tree use-when sentences. Effect: +150 tokens, fewer wrong-tool calls. Risk: low. Test: G6.
9. Build SYSTEM_CORE like `prompt_blocks.py`, dropping text about absent tools, and test an identity line "assistant on this PC; coding is one of its jobs" (F9, F11). Effect: -35 tokens when absent.
   Risk: the identity change is medium. Test: G4.
10. Tail note for three critical rules: a claim of done needs a tool result; do the work in this turn; say what is not done. Effect: fewer false "done" claims, if recency helps Qwen. Risk: the model may
    answer the note as a new request. Test: G3.
11. Move implementation detail out of long descriptions into results or errors (F16). Effect: -300 to -400 tokens. Risk: low to medium for create_skill. Test: G1.
12. One source for the dynamic counts (F8). Effect: no cache breaks. Risk: low.
13. "Person" or "user" (F13): the owner's choice.
14. (Code) Effort-retry cache (F21): if M1 shows a re-prefill, the retry keeps the main effort and relies on SHORT_ANSWER_NOTE and a smaller `max_tokens`. Test: M1.
15. Shorten the untrusted trailer on low-risk sources (F19). Effect: -21 tokens a result. Risk: security; the owner decides. Test: the E8 injection suite.
16. A capitals A/B (G5), only if a rule is still violated after the above.

## Open points for the owner

Should the budget count the tool JSON (4)? "Person" or "user" (13)? May the untrusted trailer be shortened (15, a security decision)? May the Chat's identity stop saying "coding agent" (9)? May the plan tools
become conditional on the planner being in use, which changes the cached prefix once per session?
