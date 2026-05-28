import os
from typing import Dict, Any, List
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.prompts import PromptTemplate
from langchain_core.tools import tool

# Assuming the state definitions from orchestrator
from orchestrator.state import BlackboardState, RunStatus, Plan, Microtask

class PlannerOutput(BaseModel):
    """The structured output format expected from the Planner LLM."""
    plan: Plan
    microtasks: List[Microtask]

class Planner:
    def __init__(self, tools: List[Any] = None):
        """
        Initializes the Planner Agent.
        In this MAS architecture, environment actions (like semantic search, file reads) 
        must be routed through the Tool Gateway. 
        """
        self.tools = tools or []
        
        # LLM Initialization
        api_key = os.getenv("CLUSTER_API_KEY", "dummy-key")
        self.llm = ChatOpenAI(
            model="google/gemma-4-31B-it", 
            openai_api_key=api_key,
            base_url="http://131.159.60.153:3000/v1",
            temperature=0.0, 
            max_tokens=2048,
            model_kwargs={"top_p": 0.1}
        )
        
        # The planner's ultimate job is to produce a strict plan.
        # If tools are provided, we should use an Agent or run a tool loop first 
        # to gather context, then enforce structured output.
        self.structured_llm = self.llm.with_structured_output(PlannerOutput)
        
    def invoke(self, state: BlackboardState) -> Dict[str, Any]:
        """Runs the planner logic to build the macro Plan and Microtasks."""
        objective = state.get("objective")
        obj_text = ""
        if objective:
            obj_text = f"Objective: {objective.title}\n{objective.description}\nGoals: {objective.definition_of_done}"
        else:
            obj_text = "Objective: Analyze code and prepare next steps."
            
        system_prompt = f"""You are the Planner Agent for an Agentic DevOps System.
Your job is to read the Objective and the existing repository context from your memory or tools, 
then output a strictly structured Plan and an array of Microtasks.

Microtasks MUST be actionable and grounded in the currently existing files.

== OBJECTIVE ==
{obj_text}
"""
        # Note: If this LLM needs to call tools first to fetch architecture context,
        # we would implement a LangGraph inner loop here (like ReAct).
        # For now, we assume the planner builds the plan based on the provided state context.

        print("[Planner] Querying LLM...")
        # Since we use `.with_structured_output`, we can just invoke it with the prompt.
        result: PlannerOutput = self.structured_llm.invoke(system_prompt)
        
        return {
            "status": RunStatus.NEEDS_REVIEW, # We stay in planning loop for Plan Reviewer to check
            "plan": result.plan,
            "microtasks": result.microtasks
        }
