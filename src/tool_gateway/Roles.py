
import enum


class Roles(enum.Enum):
    PLANNER = "planner"
    REVIEWER = "reviewer"
    CODER = "coder"
    TESTER = "tester"
    ORCHESTRATOR = "orchestrator"
    BOOTSTRAPPER = "bootstrapper"
    HUMAN = "human"
    DEPLOYER = "deployer"