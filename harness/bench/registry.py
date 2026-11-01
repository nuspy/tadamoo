"""The public benchmarks the harness can be measured with: where each one comes from, what it needs, how it is scored and what its board accepts.

Only facts from the study (docs/harness-study/benchmarks.md) are stated; a detail the study does not give is listed in `to_confirm` and is read again
from the benchmark's own pages when it is imported. The benchmarks themselves are never part of the repository."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Tuple

GIT = "git"
HF_DATASET = "hf_dataset"                       # a Hugging Face dataset repository, fetched with git

LEADERBOARD_KINDS = ("none", "csv_pr", "email", "closed")       # no route / a row in a results file by pull request / a prepared e-mail / a route that is shut


@dataclass(frozen=True)
class Needs:
    docker: bool = False
    wsl2: bool = False                          # needed on Windows only
    internet: bool = True


@dataclass(frozen=True)
class Subset:
    id: str
    label: str


@dataclass(frozen=True)
class BenchSpec:
    id: str
    title: str
    source_kind: str
    source: str                                 # the address git clones
    branch: str                                 # "" is the default branch
    licence: str
    needs: Needs
    subsets: Tuple[Subset, ...]
    scoring: str                                # verifier | judge | verifier+judge
    primary_metric: str                         # the key of the metrics the adapter's score() returns that the ranking compares
    leaderboard: str                            # one of LEADERBOARD_KINDS
    leaderboard_target: str = ""                # who receives an entry, in plain words
    official_cloud_judge: bool = False          # the official judge or user simulator is a cloud model: ours takes its place and the result is provisional
    tools: Tuple[str, ...] = ()                 # programs the benchmark's own install needs on the PATH
    python_min: Tuple[int, ...] = ()
    disk_gb: float = 5.0                        # free space the preflight asks for
    download_size: str = "to confirm on import"
    notes: str = ""
    to_confirm: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def uses_judge(self) -> bool:
        return self.scoring in ("judge", "verifier+judge")

    @property
    def comparable_with_board(self) -> bool:
        """False when a local model replaces the official judge or simulator: such a score is provisional."""
        return not self.official_cloud_judge


PROVISIONAL_LABEL = "unofficial judge: not comparable with the board"
LOCAL_ONLY = "Run only with the local engine and model; no other model and no cloud API."
LOCAL_JUDGE = LOCAL_ONLY + " The official judge or user simulator is a cloud model: the local model takes its place, or the judge-free mode is used, and the score is labelled '" + PROVISIONAL_LABEL + "'."

SPECS: Dict[str, BenchSpec] = {s.id: s for s in (
    BenchSpec(
        id="terminal-bench-2", title="Terminal-Bench 2.0", source_kind=GIT, source="https://github.com/laude-institute/terminal-bench-2", branch="",
        licence="Apache-2.0", needs=Needs(docker=True, wsl2=True, internet=True),
        subsets=(Subset("smoke", "One task with the oracle agent (proves Docker)"), Subset("easy5", "Five easy tasks"), Subset("dev", "A development subset"),
                 Subset("full", "All 89 tasks, 5 trials each")),
        scoring="verifier", primary_metric="resolution_rate", leaderboard="closed",
        leaderboard_target="The Terminal-Bench submission hub (submissions are closed for the moment; the package is prepared, nothing is sent)",
        tools=("uv",), disk_gb=40.0, download_size="a small clone; Docker images are pulled per task, tens of GB for all of them",
        notes="A trial passes only when the hidden test script writes reward 1. Keep timeouts and tasks as they are. Run through Harbor; a custom agent acts through environment.exec. " + LOCAL_ONLY,
        to_confirm=("repository address of the 2.0 task set", "current submission route", "Harbor install command")),
    BenchSpec(
        id="swe-bench-verified", title="SWE-bench Verified (subset)", source_kind=HF_DATASET, source="https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified",
        branch="", licence="MIT", needs=Needs(docker=True, wsl2=True, internet=True),
        subsets=(Subset("smoke", "One instance"), Subset("sample50", "A fixed sample of 50 instances"), Subset("full", "All 500 instances")),
        scoring="verifier", primary_metric="resolution_rate", leaderboard="none",
        leaderboard_target="", tools=("uv",), disk_gb=120.0, download_size="the dataset is small; the Docker images of the instances are large (about 100 GB for all)",
        notes="Scored by running the instance's tests in its Docker image. Hosted boards take self-reported results; no route is written here. " + LOCAL_ONLY,
        to_confirm=("licence of the dataset", "scorer package and version", "board submission rules", "image size per instance")),
    BenchSpec(
        id="clawbench", title="ClawBench", source_kind=GIT, source="https://github.com/TIGER-AI-Lab/ClawBench", branch="", licence="Apache-2.0",
        needs=Needs(docker=True, wsl2=True, internet=True),
        subsets=(Subset("lite", "The 20-task Lite set"), Subset("v1", "Suite V1"), Subset("v2", "Suite V2")),
        scoring="verifier+judge", primary_metric="reward_rate", official_cloud_judge=True,
        leaderboard="csv_pr", leaderboard_target="The ClawBench results file, as a pull request to its repository",
        tools=("uv",), python_min=(3, 11), disk_gb=15.0, download_size="to confirm on import (Docker images and the task set)",
        notes="Two scores: interception rate (the final request was sent) and reward rate (the judge agrees with its body). Live sites change and may block bots. "
              "The public results CSV lets a result be ranked locally. " + LOCAL_JUDGE,
        to_confirm=("results CSV address and column names", "official judge model", "pull-request target")),
    BenchSpec(
        id="bfcl-v4", title="BFCL v4 (function calling)", source_kind=GIT, source="https://github.com/ShishirPatil/gorilla", branch="", licence="Apache-2.0",
        needs=Needs(internet=True), subsets=(Subset("smoke", "A few cases per category"), Subset("simple", "Simple and parallel calls"), Subset("full", "Every category")),
        scoring="verifier", primary_metric="accuracy", leaderboard="none", leaderboard_target="", tools=("uv",), python_min=(3, 10), disk_gb=3.0,
        download_size="to confirm on import (the repository holds more than BFCL)",
        notes="Cheap and local: it checks tool-call formats and the chat template, so it tests the model more than the harness. " + LOCAL_ONLY,
        to_confirm=("folder of the BFCL v4 data inside the repository", "board submission rules")),
    BenchSpec(
        id="tau3-bench", title="tau3-bench", source_kind=GIT, source="https://github.com/sierra-research/tau2-bench", branch="", licence="MIT",
        needs=Needs(internet=True), subsets=(Subset("airline", "Airline"), Subset("retail", "Retail"), Subset("telecom", "Telecom"), Subset("banking", "Banking")),
        scoring="verifier", primary_metric="pass_hat_k", official_cloud_judge=True, leaderboard="none", leaderboard_target="",
        tools=("uv",), python_min=(3, 11), disk_gb=3.0, download_size="to confirm on import",
        notes="Multi-turn dialogs with policy rules, scored pass^k. The official user simulator is an API model. " + LOCAL_JUDGE,
        to_confirm=("repository address of the tau3 release", "licence", "official simulator model", "board submission rules")),
)}

IDS = tuple(SPECS)


def get(spec_id: str) -> BenchSpec:
    try:
        return SPECS[spec_id]
    except KeyError:
        raise KeyError(f"no benchmark called {spec_id!r}; the known ones are {', '.join(IDS)}") from None
