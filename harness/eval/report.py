"""Generates reproducible Markdown evaluation reports."""
from __future__ import annotations

from typing import Any, Dict, List
from .runner import EvalResult


def generate_eval_report(
    results: List[EvalResult],
    model_name: str = "Qwen3.8-Flash-Next",
    hardware: str = "NVIDIA RTX 5090 (sm_120)",
) -> str:
    lines = [
        f"# Basalt Harness Evaluation Report (Phase 0)",
        f"",
        f"- **Model**: {model_name}",
        f"- **Hardware**: {hardware}",
        f"- **Metric Standard**: pass^k (all k trials pass) & pass@k",
        f"",
        f"| Task ID | Category | Trials (k) | pass@k | pass^k | Avg Tokens | Avg Time (s) |",
        f"| :--- | :--- | :---: | :---: | :---: | :---: | :---: |",
    ]

    total_tasks = len(results)
    total_pass_at_k = sum(1 for r in results if r.pass_at_k > 0)
    total_pass_pow_k = sum(1 for r in results if r.pass_pow_k)

    for r in results:
        avg_tokens = sum(run.tokens for run in r.runs) / max(len(r.runs), 1)
        avg_time = sum(run.duration_s for run in r.runs) / max(len(r.runs), 1)
        lines.append(
            f"| `{r.task_id}` | {r.category} | {r.k_trials} | "
            f"{r.pass_at_k:.2f} | {'PASS' if r.pass_pow_k else 'FAIL'} | "
            f"{avg_tokens:.0f} | {avg_time:.3f}s |"
        )

    lines.extend([
        f"",
        f"### Summary",
        f"- **Total Tasks**: {total_tasks}",
        f"- **pass@k Success**: {total_pass_at_k}/{total_tasks} ({(total_pass_at_k/max(total_tasks,1))*100:.1f}%)",
        f"- **pass^k Reliability**: {total_pass_pow_k}/{total_tasks} ({(total_pass_pow_k/max(total_tasks,1))*100:.1f}%)",
    ])

    return "\n".join(lines)
