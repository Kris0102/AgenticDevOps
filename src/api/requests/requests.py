from typing import Optional

from pydantic import BaseModel

from orchestrator.state import HITL, Constraints, LLMConfig, Objective, Policy, Repo, Trigger


class RunRequest(BaseModel):

    objective: Objective
    trigger: Trigger
    repo: Repo
    constraints: Constraints
    policy: Policy
    hitl: HITL
    # Per-run LLM override (settings hub). None → the global selected model. Caps/gates are
    # already carried by `constraints`/`hitl`, pre-filled from global defaults by the UI.
    llm: Optional[LLMConfig] = None
