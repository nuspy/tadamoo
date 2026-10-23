"""Evaluation runner: executes task sets with k repetitions and calculates pass^k."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.client import BasaltClient, extract_reasoning_and_content


@dataclass
class EvalRun:
    repetition: int
    passed: bool
    output: str
    reasoning: str
    tokens: int
    duration_s: float


@dataclass
class EvalResult:
    task_id: str
    category: str
    k_trials: int
    pass_at_k: float
    pass_pow_k: bool
    runs: List[EvalRun] = field(default_factory=list)


class EvalRunner:
    def __init__(self, client: Optional[BasaltClient] = None) -> None:
        self.client = client

    def load_task_set(self, path: str | Path) -> List[Dict[str, Any]]:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data.get("tasks", [])

    def evaluate_task(
        self,
        task: Dict[str, Any],
        k: int = 5,
        mock_output_fn: Optional[Any] = None,
    ) -> EvalResult:
        task_id = task["id"]
        category = task.get("category", "general")
        runs: List[EvalRun] = []

        for i in range(k):
            t0 = time.time()
            if mock_output_fn is not None:
                output, reasoning, tokens = mock_output_fn(task, i)
            elif self.client is not None:
                resp = self.client.chat_completion(
                    messages=[{"role": "user", "content": task["prompt"]}],
                    temperature=0.0,
                )
                choice = resp["choices"][0]["message"]
                reasoning, output = extract_reasoning_and_content(choice)
                tokens = resp.get("usage", {}).get("total_tokens", 0)
            else:
                raise RuntimeError("Neither client nor mock_output_fn provided")

            duration = time.time() - t0

            # Verify correctness
            passed = True
            expected_kw = task.get("expected_keyword")
            if expected_kw and expected_kw not in output:
                passed = False

            runs.append(
                EvalRun(
                    repetition=i + 1,
                    passed=passed,
                    output=output,
                    reasoning=reasoning,
                    tokens=tokens,
                    duration_s=duration,
                )
            )

        passed_count = sum(1 for r in runs if r.passed)
        pass_at_k = 1.0 if passed_count > 0 else 0.0
        pass_pow_k = passed_count == k

        return EvalResult(
            task_id=task_id,
            category=category,
            k_trials=k,
            pass_at_k=pass_at_k,
            pass_pow_k=pass_pow_k,
            runs=runs,
        )
