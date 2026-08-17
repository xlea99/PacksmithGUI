import json
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


# App STATE, as distinct from app config. Config is what the *user* writes — hand-edited
# TOML, comments and all — and Packsmith must never rewrite it just to leave itself a
# breadcrumb. State is the opposite: things the app remembers on the user's behalf, like
# which profile was open last. Separate file, machine-owned, JSON so the standard library
# can write it (tomllib reads TOML and cannot produce it).
STATE_FILE = "state.json"


def load_state() -> dict:
    """What the app remembered last run. Missing or corrupt reads as "nothing".

    Unlike `load_config`, a malformed file here is NOT an error: nobody wrote it by hand,
    so there is no intent to respect, and refusing to launch over a damaged breadcrumb
    would be a spectacular over-reaction to losing which profile was open.
    """
    try:
        with open(GLOBAL_PATHS.config / STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def load_ui_state(profile: str) -> dict:
    """Remembered layout for one profile — panel heights, and whatever else gets dragged.

    Kept in state rather than in the profile's own `profile.json`, even though it is
    per-profile. `profile.json` holds the pack's *contract* — instance path, MC version,
    loader — which the user reads and sometimes edits. A splitter position is neither a
    setting nor a contract; it is furniture the app moved because the user dragged it, and
    it has no business in a file someone might open to check what version their pack is.
    """
    if not profile:
        return {}
    return load_state().get("ui", {}).get(profile, {}) or {}


def save_ui_state(profile: str, **changes) -> None:
    if not profile:
        return
    ui = load_state().get("ui", {})
    ui[profile] = {**ui.get(profile, {}), **changes}
    save_state(ui=ui)


def forget_ui_state(profile: str) -> None:
    """Drop everything remembered about one profile's furniture.

    Called when a profile is deleted, because this state is keyed by profile **name** and
    a name is reusable. Without it, deleting a profile and making a new one with the same
    name inherits the dead one's layout — and that layout refers to rows by *id*: a View
    group holding views 1 and 2, column widths for tags, a remembered job. None of those
    ids mean anything in the new database, so the panel restores a group of views that do
    not exist.

    The profile's data lives in `profile.db` and its furniture lives here; nothing linked
    the two lifecycles, so deleting one left the other behind.
    """
    if not profile:
        return
    ui = load_state().get("ui", {})
    if ui.pop(profile, None) is not None:
        save_state(ui=ui)


def save_state(**changes) -> None:
    """Merge ``changes`` into the stored state. Best-effort — never fatal.

    Merged rather than replaced so one caller cannot drop another's key, and swallowed
    because failing to record a breadcrumb must not take down whatever the user was
    actually doing.
    """
    state = load_state()
    state.update(changes)
    try:
        path = GLOBAL_PATHS.config / STATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except (OSError, ValueError, TypeError):
        # Wider than OSError on purpose. An unusable path raises ValueError rather than
        # OSError (an embedded null, for one), and a caller passing something JSON cannot
        # serialise raises TypeError — and "best-effort" has to mean it, or the promise is
        # only kept for the failures somebody happened to anticipate.
        #
        # Imported here, not at module scope: `logging` reads GLOBAL_PATHS back out of this
        # module, so a top-level import is circular — and this module has to be importable
        # before logging exists at all.
        from packsmith.common.logging import log
        log.warning("Could not write %s", STATE_FILE, exc_info=True)