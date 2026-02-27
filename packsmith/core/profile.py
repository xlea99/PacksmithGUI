import json
from pathlib import Path
from packsmith.common.setup import GLOBAL_PATHS, ensure_directory
from packsmith.common.logging import log
from packsmith.util.misc import raise_log, write_json

_SCHEMA_VERSION = 1
_VALID_SCHEMAS = {1}

# A profile represents one distinct pack as a unit - the minecraft install, snapshotted
# packdumps of that install, and all user data (tags, blueprints, etc) for that pack.
# loader is frozen at creation time. mc_version and loader_version are tracked for
# contract enforcement against incoming packdumps.
class Profile:

    def __init__(self, name, root, mc_path, mc_version, loader, loader_version, settings):
        self.name = name
        self.root = root
        self.mc_path = mc_path
        self.mc_version = mc_version
        self.loader = loader
        self.loader_version = loader_version
        self.settings = settings

        # Ensure profile subdirectories exist
        self.packdumps_dir = ensure_directory(root / "packdumps")

    # Save and load methods for loading and saving the profile (which is primarily just an organizational json pointing
    # at different things)
    @classmethod
    def load(cls, name: str):
        root = GLOBAL_PATHS.profiles / name
        profile_json_path = root / "profile.json"

        if not profile_json_path.exists():
            raise_log(FileNotFoundError, f"No profile.json found in profile '{name}'")

        with open(profile_json_path, "r") as f:
            data = json.load(f)

        if data.get("type") != "packsmith_profile":
            raise_log(ValueError, f"profile.json type is '{data.get('type')}', expected 'packsmith_profile'")
        schema = data.get("schema_version")
        if schema not in _VALID_SCHEMAS:
            raise_log(ValueError, f"Unsupported profile schema version: {schema}")
        # TODO: migrate older schemas to _SCHEMA_VERSION here

        game = data["game"]
        mc_path = Path(game["path"]) if game.get("path") else None

        return cls(
            name=name,
            root=root,
            mc_path=mc_path,
            mc_version=game.get("mc_version"),
            loader=game.get("loader"),
            loader_version=game.get("loader_version"),
            settings=data.get("settings", {}),
        )
    def save(self):
        data = {
            "type": "packsmith_profile",
            "schema_version": _SCHEMA_VERSION,
            "game": {
                "path": str(self.mc_path) if self.mc_path else None,
                "mc_version": self.mc_version,
                "loader": self.loader,
                "loader_version": self.loader_version,
            },
            "settings": self.settings,
        }
        write_json(self.root / "profile.json", data)
        log.info(f"Profile '{self.name}' saved")

    # Creates a brand new profile with the given name.
    @classmethod
    def create(cls, name: str, loader: str = None, mc_path: str | Path = None, mc_version: str = None, loader_version: str = None):
        root = GLOBAL_PATHS.profiles / name
        if (root / "profile.json").exists():
            raise_log(ValueError, f"Profile '{name}' already exists")
        root.mkdir(parents=True, exist_ok=True)

        if isinstance(mc_path,str):
            mc_path = Path(mc_path)
        if isinstance(mc_path,Path):
            if not mc_path.exists():
                raise_log(FileNotFoundError,f"Supplied Minecraft path not found: '{mc_path}'")

        profile = cls(
            name=name,
            root=root,
            mc_path=mc_path,
            mc_version=mc_version,
            loader=loader,
            loader_version=loader_version,
            settings={"max_packdump_snapshot_count": 10},
        )
        profile.save()
        log.info(f"Profile '{name}' created")
        return profile

    # Checks a single packdump against this profile's contract, returning a dict of issues. Empty == good :)
    def validate_packdump(self, packdump):
        issues = {}

        # Loader mismatch is a hard block
        if self.loader and packdump.loader != self.loader:
            issues["loader"] = {
                "level": "error",
                "expected": self.loader,
                "actual": packdump.loader,
            }

        # MC version mismatch is a warning (minor bumps may be okay, but often aren't)
        if self.mc_version and packdump.mc_version != self.mc_version:
            issues["mc_version"] = {
                "level": "warning",
                "expected": self.mc_version,
                "actual": packdump.mc_version,
            }

        # Loader version change is informational
        if self.loader_version and packdump.loader_version != self.loader_version:
            issues["loader_version"] = {
                "level": "info",
                "expected": self.loader_version,
                "actual": packdump.loader_version,
            }

        return issues

# Returns a list of available profile names.
def list_profiles() -> list[str]:
    if not GLOBAL_PATHS.profiles.is_dir():
        return []
    return [
        folder.name for folder in GLOBAL_PATHS.profiles.iterdir()
        if folder.is_dir() and (folder / "profile.json").exists()
    ]


test = Profile.create("testicles",mc_path=r"C:\Users\timbe\curseforge\minecraft\Instances\Packsmith Test Pack")