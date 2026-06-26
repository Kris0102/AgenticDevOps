"""TestPhase — the unit of the Tester pipeline.

Each phase is a small class with an injected set of collaborators (`self.`-attrs), a
short `run` entry point, and `_`-prefixed helpers. `should_run` lets a phase opt out
(e.g. skip synthesis on an unsupported project) while keeping the pipeline a flat,
readable sequence.

Check records (`CheckResult`, `make_check`, `skipped_check`) live in
`agents.Implementing.Tester.results`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from agents.Implementing.Tester.context import TestContext


class TestPhase(ABC):
    name: str = "phase"

    def should_run(self, ctx: TestContext) -> bool:
        return True

    @abstractmethod
    def run(self, ctx: TestContext) -> TestContext:
        ...
