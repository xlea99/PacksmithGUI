import os
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
    def data_root() -> Path:
        """Where USER DATA lives — profiles, logs, config. Never where the code lives.

        `_MEIPASS` is PyInstaller's extraction directory: a temp folder, recreated on every
        launch and deleted on exit (and read-only in onefile builds). Rooting `userdata/`
        there meant a frozen Packsmith would lose every profile, every tag and every
        blueprint each time it closed — the whole L2 database, silently, with the app
        looking like it started fresh. Nothing in dev catches it, because dev is never
        frozen.

        Three cases, in order:
        1. **Portable** — a `userdata/` sitting next to the executable wins. Modpack devs
           keep tools on the same drive as their instances and expect them to move; this
           is also the escape hatch if the per-user location is ever wrong.
        2. **Frozen** — the platform's per-user application data directory.
        3. **Source checkout** — the repo root, which is what every dev profile already
           uses. Unchanged deliberately: `userdata/` is in .gitignore and moving it would
           strand the profiles people already have.
        """
        if getattr(sys, "frozen", False):
            beside_exe = Path(sys.executable).resolve().parent / "userdata"
            if beside_exe.is_dir():
                return beside_exe.parent
            if sys.platform == "win32":
                base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
            elif sys.platform == "darwin":
                base = Path.home() / "Library" / "Application Support"
            else:
                base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
            return Path(base) / "Packsmith"
        return Path(__file__).resolve().parents[2]

    @staticmethod
    def build():
        root = ProjectPaths.data_root()


        userdata = ensure_directory(root / "userdata")
        # NOT must_exist: a fresh clone has no userdata/ at all, and crashing at *import
        # time* on a missing empty directory means the app cannot start until someone
        # mkdir's it — with a traceback that doesn't say so.
        config = ensure_directory(userdata / "config")
        logs = ensure_directory(userdata / "logs")
        profiles = ensure_directory(userdata / "profiles")

        return ProjectPaths(
            root = root,
            userdata = userdata,
            config = config,
            logs = logs,
            profiles = profiles
        )
def load_config(path: Path) -> dict:
    """App-wide config (§9.1: `userdata/config/main.toml`).

    Absent means "all defaults" — the correct reading of a file that isn't there, and the
    only reading that lets a fresh clone start. This used to be an unguarded `open()` at
    import time, so a checkout without the file died before `main()` with a
    FileNotFoundError naming a path nobody had been told to create.

    A *malformed* file is different and does raise: the user wrote it and meant something,
    and silently ignoring it would apply defaults they explicitly overrode.

    Nothing reads the result yet. Every setting that exists today is per-PROFILE (see
    `max_packdump_snapshot_count`), which is where anything about a specific pack belongs.
    """
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path} is not valid TOML: {e}") from e


GLOBAL_PATHS = ProjectPaths.build()
CONFIG = load_config(GLOBAL_PATHS.config / "main.toml")