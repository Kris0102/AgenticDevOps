import enum


class Category(str, enum.Enum):
    """Logical log channels. Each can be filtered to its own level via
    LOG_LEVEL_<NAME> (e.g. LOG_LEVEL_TOOL=DEBUG). The value is what appears in
    the `category` field of every structured line and what you filter a
    dashboard/grep on."""

    TOOL = "tool"            # ToolGateway: which tools ran / were denied + results
    SEMANTIC = "semantic"    # Context Engine: ingest counts, search queries + hits
    PLANNER = "planner"
    REVIEWER = "reviewer"
    CODER = "coder"
    TESTER = "tester"
    REFLECTOR = "reflector"
    HITL = "hitl"
    SANDBOX = "sandbox"      # Docker lifecycle / exec
    BOOTSTRAP = "bootstrap"
    ORCHESTRATOR = "orchestrator"  # graph routing / run lifecycle
    DB = "db"

    def __str__(self) -> str:
        return self.value
