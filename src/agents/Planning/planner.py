import os
from typing import Dict, Any, List
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.prompts import PromptTemplate
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langchain.agents import create_agent
from dotenv import load_dotenv
from openai import APIConnectionError, APITimeoutError




# Assuming the state definitions from orchestrator
from orchestrator.state import BlackboardState, RunStatus, Plan, Microtask

# Assuming we have the actual Tool Gateway and Docker Sandbox tools
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tool_gateway.gateway import ToolGateway
from sandbox.sandbox_tools import DockerSandboxTools

# For PoC, we create a rapid local sqlite session to satisfy gateway DB logging.
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db.models import Base

# Load environment variables from .env if present
load_dotenv()
engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)
db_session = SessionLocal()

# Instantiate the sandbox tools (we can mock the Docker container ID logic by simply executing locally in a mocked way, or if Docker is running, use it)
# We will assume `DockerSandboxTools` needs to be initialized. For a true PoC without breaking execution, let's setup a placeholder if Docker fails.
try:
    sandbox_tools = DockerSandboxTools(image="python:3.10-slim", workdir="/workspace")
    # Tell docker SDK to pull the image if it doesn't exist yet before creating the container
    try:
        sandbox_tools.client.images.get("python:3.10-slim")
    except Exception:
        print("Pulling python:3.10-slim image...")
        sandbox_tools.client.images.pull("python:3.10-slim")

    # Create the container for this run
    sandbox_tools.sandbox_create(run_id="poc_run_123")
    gateway = ToolGateway(db_session=db_session, run_id="poc_run_123", sandbox_tools=sandbox_tools)
except Exception as e:
    print(f"Warning: Could not initialize Docker Sandbox. Tools will fallback to local execution. Error: {e}")
    gateway = None

# Define the tools the Planner agent can use
@tool
def execute_in_sandbox(command: str) -> str:
    """Run a shell command like 'grep' or 'ls' inside the secure Docker Sandbox. 
    Use this to search the codebase and understand the file structure.
    Example commands: 'ls -la', 'grep -rn "class Planner" src/'
    """
    if not gateway:
        # Fallback to local execution if Docker is not available
        import subprocess
        try:
             result = subprocess.run(command, shell=True, capture_output=True, text=True, cwd="/home/kristian/project/AgenticDevOps")
             return f"Exit Code: {result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        except Exception as e:
             return f"Command execution failed: {str(e)}"
    
    # Needs to be a list for subprocess/docker exec
    import shlex
    cmd_list = shlex.split(command)
    
    result = gateway.execute_tool(
        actor_role="PLANNER", 
        tool_name="sandbox.run", 
        args={"cmd": cmd_list}
    )
    if "error" in result:
        return result["error"]
    return f"Exit Code: {result.get('exit_code')}\nSTDOUT:\n{result.get('stdout')}\nSTDERR:\n{result.get('stderr')}"

@tool
def read_sandbox_file(filepath: str) -> str:
    """Read the contents of a specific file inside the secure Docker Sandbox.
    Use this to read specific module implementations and architectures.
    """
    if not gateway:
        # Fallback to local file read
        try:
            with open(f"/home/kristian/project/AgenticDevOps/{filepath}", "r") as f:
                return f.read()
        except Exception as e:
            return f"Failed to read file: {str(e)}"
    
    result = gateway.execute_tool(
        actor_role="PLANNER",
        tool_name="fs.read_file",
        args={"path": filepath}
    )
    if "error" in result:
        return result["error"]
    return result.get("content", "Empty file or could not read.")

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
                    "for the requested objective. The codebase is located at `/home/kristian/project/AgenticDevOps`. "
                    "You can use the 'execute_in_sandbox' tool to run bash commands like 'ls', 'find', and 'grep' "
                    "to discover the directory structure, and 'read_sandbox_file' to read specific files. "
                    "Summarize your findings, detailing the files, modules, and changes needed. "
                    "If the task requires no code changes but coding from scratch no investigation of the codebase is needed!"
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

Microtasks MUST be actionable and grounded in the existing files discovered during the investigation.

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
