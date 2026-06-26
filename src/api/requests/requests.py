from pydantic import BaseModel

from orchestrator.state import HITL, Constraints, Objective, Policy, Repo, Trigger



class RunRequest(BaseModel):
    
    objective: Objective
    trigger: Trigger
    repo: Repo
    constraints: Constraints
    policy: Policy
    hitl: HITL
    
    
    
    
    



