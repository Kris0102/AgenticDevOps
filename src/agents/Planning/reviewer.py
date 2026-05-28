
from src.tool_gateway.Roles import Roles
class Reviewer:
    role: Roles
    def __init__(self):
        self.role = Roles.REVIEWER
