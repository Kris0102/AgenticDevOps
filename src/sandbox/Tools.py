
import enum
class Tools(str, enum.Enum):
    
    SANDBOX_RUN = "sandbox.run"
    FS_READ_FILE = "fs.read_file"
    FS_WRITE_FILE = "fs.write_file"
    REPO_DIFF = "repo.diff"
    SEMANTIC_SEARCH = "semantic.search"