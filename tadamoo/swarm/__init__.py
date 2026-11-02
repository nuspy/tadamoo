"""Multi-agent Swarm Orchestrator package for Basalt Harness.

Implements fan-out with shared prefix caching and role-based execution.
"""
from .orchestrator import SwarmOrchestrator, SwarmPlan, SwarmMember

__all__ = ["SwarmOrchestrator", "SwarmPlan", "SwarmMember"]
