"""Ordered Tester phases. The pipeline runs them in this sequence; see each module."""
from agents.Implementing.Tester.phases.base import TestPhase
from agents.Implementing.Tester.phases.detect_phase import DetectProjectPhase
from agents.Implementing.Tester.phases.deterministic_checks_phase import DeterministicChecksPhase
from agents.Implementing.Tester.phases.execute_phase import ExecutePhase
from agents.Implementing.Tester.phases.guard_phase import TrivialGuardPhase
from agents.Implementing.Tester.phases.synthesis_phase import SynthesisPhase
from agents.Implementing.Tester.phases.verdict_phase import VerdictPhase

__all__ = [
    "TestPhase",
    "DetectProjectPhase",
    "DeterministicChecksPhase",
    "SynthesisPhase",
    "TrivialGuardPhase",
    "ExecutePhase",
    "VerdictPhase",
]
