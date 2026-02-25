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

    config: Path
    logs: Path

    data: Path

    # These are all built dynamically off of config, later.
    mc_root : Path = None
    mc_probejs : Path = None
    mc_kubejs_client : Path = None
    mc_kubejs_server : Path = None
    mc_kubejs_startup : Path = None
    mc_config : Path = None
    mc_paxi_data : Path = None
    mc_paxi_resources : Path = None

    glue_mod_data: Path = None
    glue_mod_assets: Path = None

    @staticmethod
    def build():
        # For frozen apps, PyInstaller unpacks resources to _MEIPASS - handle that case here
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            # noinspection PyProtectedMember
            root = Path(sys._MEIPASS)
        # Otherwise, rely on strict 3up root policy
        else:
            root = Path(__file__).resolve().parents[2]

        config = ensure_directory(root / "config", must_exist=True)
        logs = ensure_directory(root / "logs")

        data = ensure_directory(root / "data", must_exist=True)

        return ProjectPaths(
            root = root,
            config = config,
            logs = logs,
            data = data
        )
PATHS = ProjectPaths.build()

# Build config
with open(PATHS.config / "main.toml","rb") as f:
    CONFIG = tomllib.load(f)

# Load all config-dependent paths to the PATHS object.
# PATHS.mc_root = Path(CONFIG["paths"]["minecraft_instance"])
# PATHS.mc_probejs = PATHS.mc_root / ".vscode"
# PATHS.mc_kubejs_client = PATHS.mc_root / "kubejs/client_scripts"
# PATHS.mc_kubejs_server = PATHS.mc_root / "kubejs/server_scripts"
# PATHS.mc_kubejs_startup = PATHS.mc_root / "kubejs/startup_scripts"
# PATHS.mc_config = PATHS.mc_root / "config"
# PATHS.mc_paxi_data = PATHS.mc_config / "paxi/datapacks"
# PATHS.mc_paxi_resources = PATHS.mc_config / "paxi/resourcepacks"