# Benchmarks for the harness: study

What the person asked (1 November): measure where the harness really stands, with benchmarks that are cited in technical articles; the four sources they found
(ClawBench, Terminal-Bench 2.0, Claw-Eval, agent-eval-harness) analysed, others suggested; the suites integrated outside the commit, started by the user from a
separate tab reachable from Settings; publishing to a central leaderboard only by hand with a double confirmation and only where the benchmark allows it; the place
in the ranking shown before publishing, by comparing with the entries already there and simulating the benchmark's own analysis; and results that say what failed
and how to improve it.

The reports behind this page were written from the projects' own pages on 1 November 2026. Scores and leaderboard rules change often: every number here is to be
read again from the source before it is quoted. Nothing was run. Headline numbers must come from the maintainer's own runs and carry the GPU power limit.

## The four sources

| Source | What it is | Verdict |
|---|---|---|
| ClawBench (TIGER-AI-Lab, Apache-2.0) | 281 everyday tasks on live websites (V1, V2, a 20-task Lite set), a browser agent in Docker; pass = the final request is intercepted and a strict LLM judge agrees with it | Priority 3. Open leaderboard with a downloadable CSV and a pull-request submission. |
| Terminal-Bench 2.0 (Harbor framework, Apache-2.0 dataset) | 89 Linux tasks in Docker graded by hidden tests, 5 trials per task for the board | Priority 3 as a gauge, 1 as a target. The one major board that names the harness. Submissions are closed while a new process is built. |
| Claw-Eval (MIT) | 300 multi-step tasks with mock services (CRM, mail), 3 trials, score = safety x (0.8 completion + 0.2 robustness), ranking by Pass^3 | Priority 2. Judges are cloud models; it measures the model in its own loop unless the loop is replaced. Entries go in by email. |
| agent-eval-harness (linny006) | Not a benchmark: a script that lists GitHub repos with the topic llm-eval. No tasks, no scorer, no board; the README promises a leaderboard that does not exist; the licence file it names is missing | Ignore. |

(The table is the one exception to the no-tables habit of the chat; it is a document.)

### ClawBench in detail
- Needs Python 3.11, uv, Docker or Podman, live internet. The default judge is a cloud model and needs a key; `--no-judge` skips it. Subsets by suite or by case.
- Two scores: interception rate (the agent sent the final request the task names) and reward rate (the judge says the request body matches the instruction). One run per task.
- The public CSV (leaderboard results of every entry) lets a new result be ranked locally. Every entry uses the same harness (Hermes); small free models sit at 2 to 5%
  interception, the best cloud models around 45 to 60%.
- A custom harness is a registry entry (Dockerfile, setup and run scripts, usage emitter) that receives the instruction and a browser URL to drive; there is no written
  spec, so the Hermes entry is the model to copy. Needs `harness/browser.py` to attach to an external browser: to check. Effort M, or L if it cannot.
- Per run it keeps video, actions, messages, HTTP requests and the judge's short reason: enough to tell a login wall or a CAPTCHA from a wrong page.
- Risks: live sites change and block bots; paywalled tasks can never pass; a local 30B model with 32-65k context will probably sit at the floor; the official judge is a cloud
  model, so a local judge would make the number not comparable.

### Terminal-Bench 2.0 in detail
- Harbor runs it: `harbor run -d terminal-bench@2.0 -a ... -m ... -k 5`. Linux containers; on this PC the route is WSL2 with Docker (the Docker client is installed; whether the
  daemon runs was not checked). Typical limits 900 s for the agent and 900 s for the verifier, 1 CPU, 2 GB.
- A trial passes only when the hidden test script writes reward 1: the final container state is checked, no partial credit. The result is a resolution rate with a 95%
  interval. The board asks for at least 5 trials per task over all tasks with default limits: 445 trials, days on one GPU.
- A custom agent is a `BaseAgent` subclass that acts through `environment.exec`; a host-side Python harness like ours fits (effort M, a hello-world is S).
- The paper classes failures as execution, coherence and verification; the most common single failure is calling a missing executable. Trajectories are kept in a standard
  format, so failed trials can feed the lessons.
- Newer versions exist (2.1, 3.0, 4.0); 3.0 and 4.0 are too hard for a 30B model. Submissions are closed for the moment: the new route is an upload to a public hub with CI
  checks and a judge that reads trajectories. No score file can be downloaded; the scores seen come from mirrors.
- Rules: do not change timeouts or tasks; keep task text out of memory and training data.

### Claw-Eval in detail
- Python 3.11, Docker (a fresh container per task), bash scripts, an OpenRouter key for the judges (a Gemini Flash model for general and multimodal, a Claude Opus model for the
  multi-turn dialogs). Our model can be the agent through any OpenAI-compatible address.
- Route A runs its own agent loop against our model server: it tests the model, not our harness (effort S). Route B replaces the loop with ours: bridge the task tools (HTTP
  mock services) and write its trace format (effort L).
- Traces are rich (tool calls, compaction events, token and time breakdown, failure modes, per-dimension scores). The board is a web page whose data is bundled in a script, with
  no clean download; the code was still being audited at the time of the report.

## The principal benchmarks for a harness (survey, ranked for us)

Fit for a local Python harness (coding, chat, office, browser, MCP) with a ~30B MoE model, 32-65k context, one consumer GPU, Windows with WSL2.

1. **Terminal-Bench 2.0/2.1**: the board names the harness; terminal work is our core. Reference points for 30B-class models in the paper's own scaffold are low (open
   models between about 19 and 36 on the largest ones); our guess for this model is 5 to 20%.
2. **SWE-bench Verified (a subset) with Multilingual**: still the most cited coding test; contaminated and saturated at the top, but at 30B it separates scaffolds. The same model
   scores about 49 to 51 with an OpenHands-style scaffold and about 19 with a bash-only mini agent: that gap is the harness effect, the thing we want to see. Docker; hosted
   boards take self-reported results.
3. **tau3-bench (airline, retail, telecom, banking)**: multi-turn dialog with policy rules, pass^k. No Docker. Cost is the user simulator (an API model).
4. **BFCL v4**: cheap, local check of tool-call formats and the chat template. Tests the model more than the harness.
5. **MCPMark, then Toolathlon**: MCP and office-style tasks verified by scripts (pass or fail), the closest to our MCP use. MCP-Atlas and LiveMCPBench depend on an LLM judge.
6. **WebArena-Verified (Hard subset) and GAIA text levels 1-2**: web work, reproducible and cheap. BrowseComp is too hard for a general 30B model.
7. **Claw-Eval**: the closest to a personal agent, with the cautions above. **Aider polyglot**: small, quick code-editing check.
- Too heavy or off target for now: OSWorld, Mind2Web 2, SWE-Lancer, CORE-Bench, Vending-Bench, AgentBench, ToolBench. SWE-bench Pro is the successor of Verified but heavy.
- Traps, for every suite: contamination (keep task text out of memory and out of the lessons); scaffold-versus-model attribution (10 to 20 points swing); pass@1 against pass^k
  against five-run averages; judge drift and user-simulator drift; the version of the task set; vendor scores with 200-256k contexts that are upper bounds for a 32-65k window;
  cost reporting (tokens, wall-clock, GPU power limit).

## What a result is worth here

Only ClawBench publishes a result file that anyone can rank against. For Terminal-Bench the exact ranking cannot be simulated until the new submission route opens (the
official score is the resolution rate, which the local verifier does reproduce, but the entry list is not downloadable). Claw-Eval's entries are bundled in a web page.
So the "place before publishing" is exact for ClawBench, a local reproduction of the benchmark's own score plus a comparison with published numbers for Terminal-Bench, and an
estimate for the others; the tab must say which one it is.

## Design of the integration

Outside the commit: the benchmarks themselves (their code, datasets, Docker images, results) are never in the repository. They are fetched on demand, at a pinned version, into a
folder of the user data folder (`benchmarks/<id>/`), with their licence kept beside them; the repository holds only our adapters and manifests, which are small.

- **Registry** (`harness/bench/registry.py`): one entry per benchmark: source address, pinned commit or version, licence, what it needs (Docker, WSL2, a judge key, internet),
  how to install, how to run a subset or all, how to read its results, how to score them locally, what the board accepts and how.
- **Preflight**: the tab checks what is missing before anything is started (the Docker daemon, WSL2, disk, a free GPU, the key for a judge) and says how to fix it.
- **Runner** (`harness/bench/runner.py`): a background job with its own folder (`benchmarks/runs/<id>/`): settings, one line per finished trial (the same journal and fingerprint
  as the experiments, so an interrupted run resumes), the logs and the trajectories. Cost is recorded: tokens, seconds, power limit, model file, commit.
- **Adapters**: one per benchmark, written against its own agent interface (Harbor `BaseAgent`, ClawBench harness entry, the Claw-Eval loop), running our harness headless.
- **Analysis** (`harness/bench/analysis.py`): reads the trajectories and the verifier output and classes every failure (wrong final state, timeout, context overflow, tool
  format error, missing executable, refusal by our own permission layer, loop, bad plan, environment problem, a site that blocked us), then maps each class to a part of the
  harness and writes the "what failed and what to change" report. Repeated classes become candidate lessons (never the task text), and the report lists the tasks that passed
  only sometimes.
- **Ranking** (`harness/bench/ranking.py`): the benchmark's own scorer on our results; the published entries when the benchmark allows them to be downloaded; our simulated rank among
  them, with the confidence interval and a plain note when the score was made without the official judge (then the rank is marked provisional).
- **Tab** (Settings > Benchmarks, `serve/web/bench.js`, `serve/bench_api.py`): the list with status, preflight, a start form (subset, trials, which model), live progress, results
  with the failure report and the rank preview, and the publish step.
- **Publishing**: always two separate confirmations, each showing exactly what leaves the PC (files, sizes, destination, the text of the entry) and who it goes to; never automatic;
  offered only where the benchmark has a route (ClawBench: a row in a results file through a pull request; Claw-Eval: a prepared e-mail; Terminal-Bench: disabled until its route
  is open). Otherwise the tab prepares the package and stops. Credentials for a destination come from the vault, never from the page.
- **Rules**: one GPU process at a time and the other session's protocol; no cloud judge or key is used without the person's approval of the cost; benchmark task text is never
  stored in memory, lessons or training; a result is labelled with the power limit and the harness version.

## Phases

1. The framework (registry, preflight, runner with journal, analysis skeleton, tab) and the smoke test of Terminal-Bench with its oracle agent on one task: proves Docker/WSL2.
2. The Terminal-Bench adapter: five easy tasks with one trial, then a development subset; the failure report; fixes; only then the full 89 x 5 on a quiet machine.
3. SWE-bench Verified subset with a bash-only baseline and our scaffold; BFCL v4 as a quick check; tau3-bench once an API simulator is approved.
4. ClawBench: the null and random-click harnesses on two Lite cases to prove the pipeline, then the adapter if the browser can attach to an external one.
5. MCPMark/Toolathlon and Claw-Eval route A.

## Questions for the owner

Which suites first; whether benchmark code stays out of the repository as read here (fetched and pinned in the user data folder); whether a cloud judge key may be used and with what
cost cap; whether WSL2 and Docker may be set up on this PC.
