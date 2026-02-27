import sys
import tomllib
from pathlib import Path
from dataclasses import dataclass

# Lil helper function to create missing directories if missing, and optionally error out when a path
# doesn't exist.
def ensure_directory(path: Path,must_exist=False):
    if must_exist:
        if not path.exists():
            raise FileNotFoundError(f"Required directory is missing: {path}")
        if not path.is_dir():
            raise NotADirectoryError(f"Expected a directory, got a file: {path}")
    else:
        path.mkdir(parents=True,exist_ok=True)
    return path

# Dataclass for accessing paths across program.
@dataclass(frozen=False)
class ProjectPaths:

    root: Path

    # User data stuff
    userdata: Path
    config: Path
    logs: Path
    profiles: Path


    @staticmethod
    def build():
        # For frozen apps, PyInstaller unpacks resources to _MEIPASS - handle that case here
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            # noinspection PyProtectedMember
            root = Path(sys._MEIPASS)
        # Otherwise, rely on strict 3up root policy
        else:
            root = Path(__file__).resolve().parents[2]

        userdata = ensure_directory(root / "userdata")
        config = ensure_directory(userdata / "config", must_exist=True)
        logs = ensure_directory(userdata / "logs")
        profiles = ensure_directory(userdata / "profiles")

        return ProjectPaths(
            root = root,
            userdata = userdata,
            config = config,
            logs = logs,
            profiles = profiles
        )
GLOBAL_PATHS = ProjectPaths.build()
# Build config
with open(GLOBAL_PATHS.config / "main.toml","rb") as f:
    CONFIG = tomllib.load(f)