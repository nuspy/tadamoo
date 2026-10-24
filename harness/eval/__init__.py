"""Basalt harness evaluation: task sets, a runner with isolated trials, and reproducible reports."""
from .report import generate_eval_report, results_to_json, summarize
from .runner import EvalResult, EvalRunner, Trial, collect_environment
from .tasks import Task, expand_strategies, load_task_set

__all__ = ["EvalRunner", "EvalResult", "Trial", "Task", "load_task_set", "expand_strategies", "collect_environment",
           "generate_eval_report", "results_to_json", "summarize"]
