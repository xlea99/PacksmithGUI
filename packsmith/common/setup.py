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
    mc_packsmith_dumps : Path = None
    mc_kubejs_client : Path = None
    mc_kubejs_server : Path = None
    mc_kubejs_startup : Path = None
    mc_config : Path = None
    mc_paxi_data : Path = None
    mc_paxi_resources : Path = None

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
GLOBAL_PATHS = ProjectPaths.build()

# Build config
with open(GLOBAL_PATHS.config / "main.toml","rb") as f:
    CONFIG = tomllib.load(f)

# Load all config-dependent paths to the GLOBAL_PATHS object.
GLOBAL_PATHS.mc_root = Path(CONFIG["paths"]["minecraft_instance"])
GLOBAL_PATHS.mc_packsmith_dumps = GLOBAL_PATHS.mc_root / "packsmith" / "dumps"
#GLOBAL_PATHS.mc_kubejs_client = GLOBAL_PATHS.mc_root / "kubejs/client_scripts"
#GLOBAL_PATHS.mc_kubejs_server = GLOBAL_PATHS.mc_root / "kubejs/server_scripts"
#GLOBAL_PATHS.mc_kubejs_startup = GLOBAL_PATHS.mc_root / "kubejs/startup_scripts"
GLOBAL_PATHS.mc_config = GLOBAL_PATHS.mc_root / "config"
GLOBAL_PATHS.mc_paxi_data = GLOBAL_PATHS.mc_config / "paxi/datapacks"
GLOBAL_PATHS.mc_paxi_resources = GLOBAL_PATHS.mc_config / "paxi/resourcepacks"