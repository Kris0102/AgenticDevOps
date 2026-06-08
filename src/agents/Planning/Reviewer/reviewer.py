from tool_gateway.Roles import Roles


class Reviewer:

    def __init__(self):
        self.role = Roles.REVIEWER
        self.name = Roles.REVIEWER.value
