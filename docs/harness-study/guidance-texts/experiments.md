# Guidance texts: experiments

To run when the GPU is free, in the style of `harness/eval/cases`. Common rules: arms are applied by patching module constants or the registry's tool descriptions inside the experiment process, then restoring
them; no production prompt changes until something wins. Each experiment has an A/A control (`with_control`, arm `N~aa`). Tasks come from `core.json` (10), `toolcalls.json` (8), `planner_chat.json` (6) and
the fixtures `derail_tasks` (6), `lesson_tasks`, `gate_tasks`, `error_scenarios`. Checks are scripts; no model judges. k is at least 5 for task runs and 20 for single-request probes. Report cached and uncached
prompt tokens and the GPU power limit with every number. Win rule: the challenger must beat the baseline by more than the A/A gap and the stated minimum effect, with no guard broken.

- **G1 tool-surface weight** (rewrites 1, 2, 5, 11). Arms: A current; B short `description` help; C = B plus plan diet and trimmed long descriptions; D = A without the 17 plan/ask tools, as an upper bound for
  tasks that do not need plans (reported, not decided on); A~aa. Tasks: core 10, toolcalls 8, planner_chat 6 (not for D), k=5. Primary metric: mean total tokens per task. Guards: success share, invalid or
  unknown call share, share of calls carrying the `description` sentence. Win: B or C beats A when tokens fall by more than the A/A gap and at least 8%, success is no lower by more than max(10 points, the
  gap), invalid calls no higher by more than 3 points.
- **G2 note-compliance probes** (rewrites 6, 7). Scripted single-request states: P1 grace, P2 cut reply, P3 repeat guard, P4 stray `[harness]` note, P5 verify failure. Arms: A current; B positive
  wordings; C = B plus the convention sentence; A~aa. k=20 per probe per arm. Metrics: P1 tool-call share and whether planted facts are reported; P2 repetition of the last 40 characters; P3 the next call
  differs or the block is named; P4 replies that acknowledge the note as a request without progress; P5 check files untouched and a fix attempted. Win: B or C improves the primary metric by at least 10
  points and more than the A/A gap, and no other probe worsens by more than the gap.
- **G3 placement** (rewrite 10). Arms: N none; S in the system block; T tail note on every request; ST both; N~aa. Tasks: derail_tasks 6 and gate_tasks, k=6. Primary: false-success share (the answer
  claims done while the check script says no). Guards: completed share no lower by more than 10 points; spurious acknowledgements no higher by more than 3 points. Win: T or ST beats S when false-success
  falls by more than the A/A gap and at least 10 points. If S does not beat N, the rules do nothing on Qwen.
- **G4 identity, convention sentence, present_plan** (rewrites 3, 6, 9). A 2x2 of identity (coding agent / assistant on this PC) by convention sentence (absent / present), plus a fixed-`present_plan` arm
  and a control. Tasks: planner_chat 6, core 10 and 6 new non-code requests, k=5. Metrics: code tools used on non-code requests; ask_user over-asking; `present_plan` calls in default mode; success. Win:
  wrong-mode behaviour falls by more than the A/A gap and at least 1 call per 10 tasks, with success no lower by more than 10 points.
- **G5 capitals and negation** (rewrites 7, 16; low priority). Arms: A plain; B capitals on four rules the probes show are violated; C positive restatement; A~aa. Probes: ask_user over-asking, secret
  placeholder, injection set, unsupported claim; k=10 each. Win: compliance up by at least 10 points and more than the A/A gap on at least two of the four rules, with no other rule down by more than the gap.
- **G6 confusable tools** (rewrite 8). Arms: A current; B with "when not" lines for eight pairs; C = B plus reworded use-when sentences for repo_map/tree/search_code; A~aa. Tasks: 16 new fixtures (two per
  pair), script-checked right tool, k=5. Metric: right-tool share on the first call. Guard: tokens per task no higher by more than 3%. Win: up by at least 8 points and more than the A/A gap.
- **M1 effort and cache** (rewrite 14). Two requests with an identical long prefix, the second at another `reasoning_effort`; compare `cached_tokens` through CountingClient. About one GPU-minute. If the
  second reuses less than the prefix, rewrite 14 stands.

Order when the GPU is free: M1, G2, G4, G1, G6, G3, G5.
