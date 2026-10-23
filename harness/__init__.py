"""Basalt Autonomous Agentic Harness.

Designed for Qwen3.8-Flash-Next on NVIDIA Blackwell sm_120.
"""
from .client import BasaltClient, extract_reasoning_and_content

__all__ = ["BasaltClient", "extract_reasoning_and_content"]
