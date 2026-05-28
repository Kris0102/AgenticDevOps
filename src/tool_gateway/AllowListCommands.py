
class AllowListCommands:
    def __init__(self):
        self.allowed_commands = {"git", "python", "pytest"}

    def is_command_allowed(self, command: str) -> bool:
        return command in self.allowed_commands
    
    