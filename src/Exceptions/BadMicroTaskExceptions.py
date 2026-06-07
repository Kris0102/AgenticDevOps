
class BadMicroTaskException(Exception):
    """Raised when a microtask fails quality validation checks."""
    
    def __init__(self, message:str): 
        super().__init__(message)