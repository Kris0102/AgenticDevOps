import os
from typing import Dict, Any, List
from pydantic import BaseModel
from langchain_openai import ChatOpenAI


from langchain_core.messages import HumanMessage
from langchain.agents import create_agent
from dotenv import load_dotenv
from openai import APIConnectionError, APITimeoutError

from agents.Planning.planner_tools import PlannerTools
from orchestrator.state import BlackboardState, RunStatus, Plan, Microtask

# Load environment variables from .env if present
load_dotenv()


class PlannerOutput(BaseModel):
    """The structured output format expected from the Planner LLM."""
    plan: Plan
    microtasks: List[Microtask]

class Planner:
    def __init__(self, tools: List[PlannerTools] = None):
        """
        Initializes the Planner Agent.
        In this MAS architecture, environment actions (like semantic search, file reads) 
        must be routed through the Tool Gateway. 
        """
        self.tools = tools or []
        
        # LLM Initialization
        api_key = os.getenv("CLUSTER_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "Missing API key. Set CLUSTER_API_KEY (preferred) or OPENAI_API_KEY before running."
            )
            
        try:     
            self.llm = ChatOpenAI(
                model="google/gemma-4-31B-it", 
                openai_api_key=api_key,
                base_url="http://131.159.60.153:3000/inference",
                temperature=0.0, 
                max_tokens=8192,
                model_kwargs={"top_p": 0.1},
                max_retries=0,
                timeout=60.0
            )
            
            # Enforce structured output at the end of the pipeline
            self.structured_llm = self.llm.with_structured_output(PlannerOutput, method="json_mode")
            
            # Setup the investigator ReAct agent if tools are available
            if self.tools:
                investigator_prompt = (
                    "You are the Lead Solutions Architect for an Agentic DevOps System. "
                    "Your job is to use your tools to explore the codebase and gather concrete context "
                    "for the requested objective. The codebase sandbox is mounted at `/workspace`. "
                    "First, use the 'execute_in_sandbox' tool to run `ls -A /workspace`. This will tell you if the "
                    "repository is empty (meaning you must build from scratch) or if a codebase already exists. "
                    "If files exist, run bash commands like 'find' and 'grep' to discover the structure, "
                    "and 'read_sandbox_file' to read specific files. "
                    "Summarize your findings, detailing whether it is a new project or an existing one, "
                    "and what files, modules, and changes are needed."
                )
                self.investigator_agent = create_agent(self.llm, tools=self.tools, system_prompt=investigator_prompt)
            else:
                self.investigator_agent = None

        except Exception as e :
            print(f"An error ocurred. Error message: {e}")
            raise e
            
                
                    
            
    def invoke(self, state: BlackboardState) -> Dict[str, Any]:
        """Runs the planner logic to build the macro Plan and Microtasks."""
        objective = state.get("objective")
        obj_text = ""
        if objective:
                title = objective.title
                description = objective.description
                dod = objective.definition_of_done
                obj_text = f"Objective: {title}\n{description}\nGoals: {dod}"
        else:
            obj_text = "Objective: Analyze code and prepare next steps."
            
        # 1. Investigation Phase
        investigation_summary = "No investigation performed (no tools provided)."
        if self.investigator_agent:
            print("[Planner] Investigating the codebase using tools...")
            investigation_prompt = (
                f"Perform a thorough investigation for the following objective:\n\n{obj_text}\n\n"
                "Use your tools to find relevant files, understand the architecture, and summarize "
                "exactly what needs to be changed and where. Return a detailed summary."
            )
            
            try:
                investigative_state = self.investigator_agent.invoke({
                    "messages": [HumanMessage(content=investigation_prompt)]
                })
                investigation_summary = investigative_state["messages"][-1].content
                print("[Planner] Investigation complete.")
            except Exception as e:
                print(f"[Planner] Investigation failed: {e}")
                investigation_summary = f"Investigation failed: {e}"
                raise e

        # 2. Synthesis Phase (Structured Output)
        print("[Planner] Synthesizing final structured Plan...")
        schema_json = PlannerOutput.schema_json() if hasattr(PlannerOutput, "schema_json") else PlannerOutput.model_json_schema()
        synthesis_prompt = f"""You are the Planner Agent for an Agentic DevOps System.
Your job is to read the Objective and the Investigation Findings, then output a strictly structured Plan and an array of Microtasks.

Microtasks MUST be actionable and grounded in the investigation findings. All development happens inside the `/workspace` directory of the sandbox.

You must reply with a valid JSON object matching this schema, and nothing else (no markdown wrapping, no extra text):
{schema_json}

== OBJECTIVE ==
{obj_text}

== INVESTIGATION FINDINGS ==
{investigation_summary}
"""
        # Since we use `.with_structured_output`, we can just invoke it with the prompt.
        try:
            result: PlannerOutput = self.structured_llm.invoke(synthesis_prompt)
        except (APIConnectionError, APITimeoutError) as exception:
            print(f"[Planner] Failed to connect to the cluster. Ensure the VPN is connected! Error: {exception}")
            raise exception
        except Exception as e:
            print(f"[Planner] Error during synthesis: {e}")
            raise e
        
        print("[Planner] Synthesis complete. Returning Plan and Microtasks.")
        print(f"Plan Summary: {result.plan.summary}")
        print("\nMicrotasks:")
        for idx, task in enumerate(result.microtasks):
            print(f"  [{idx + 1}] {task.title} (ID: {task.microtask_id})")
            print(f"      Description: {task.description}")
            if task.acceptance_criteria:
                print(f"      Acceptance Criteria: {', '.join(task.acceptance_criteria)}")
        
    
        return {
            "status": RunStatus.NEEDS_REVIEW, # We stay in planning loop for Plan Reviewer to check
            "plan": result.plan,
            "microtasks": result.microtasks
        }
