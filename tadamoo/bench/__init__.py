"""Public benchmarks run against the harness, with the engine and model served locally. The benchmarks themselves live outside the repository."""
from .service import BenchService

__all__ = ["BenchService"]
