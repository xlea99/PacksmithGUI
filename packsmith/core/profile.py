import json
import shutil
from pathlib import Path
from packsmith.common.setup import GLOBAL_PATHS, ensure_directory, forget_ui_state
from packsmith.common.logging import log
from packsmith.util.misc import raise_log, write_json

_SCHEMA_VERSION = 1
_VALID_SCHEMAS = {1}

# How strictly an incoming packdump's Minecraft version must match the profile's.
#
# `mc_version` is not a property of a profile so much as part of its IDENTITY. A dump from
# a different Minecraft version is not a newer snapshot of the same pack — it is a snapshot
# of a different game, in which every registry id the user has tagged may mean something
# else or not exist. From 1.14 or so onward, even a patch bump (1.20.1 -> 1.20.2) is a
# parallel mod ecosystem, so `strict` is the only correct default.
#
# `same_minor` exists for genuinely old packs, where patch releases did share an ecosystem
# (1.7.6 and 1.7.10 ran the same mods). It is a per-profile choice rather than a rule we
# infer, deliberately: any "which versions are compatible" table encodes modding lore that
# goes stale, and the person who owns the pack already knows the answer.
MC_VERSION_POLICIES = ("strict", "same_minor")
_DEFAULT_MC_VERSION_POLICY = "strict"


def _version_parts(version: str):
    """('1', '20', '1') -> (1, 20, 1). None when it isn't a plain numeric version."""
    if not version:
        return None
    parts = str(version).split(".")
    if not all(part.isdigit() for part in parts):
        return None                     # snapshots, release candidates, anything odd
    return tuple(int(part) for part in parts)


def mc_versions_compatible(expected: str, actual: str, policy: str) -> bool:
    if expected == actual:
        return True
    if policy != "same_minor":
        return False
    expected_parts, actual_parts = _version_parts(expected), _version_parts(actual)
    if expected_parts is None or actual_parts is None:
        return False                    # can't reason about it, so don't
    return expected_parts[:2] == actual_parts[:2]

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
        self.packages_dir = ensure_directory(root / "packages")

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
    def create(cls, name: str, loader: str = None, mc_path: str | Path = None, mc_version: str = None,
               loader_version: str = None, mc_version_policy: str = _DEFAULT_MC_VERSION_POLICY):
        root = GLOBAL_PATHS.profiles / name
        if (root / "profile.json").exists():
            raise_log(ValueError, f"Profile '{name}' already exists")
        if mc_version_policy not in MC_VERSION_POLICIES:
            raise_log(ValueError, f"Unknown mc_version_policy: '{mc_version_policy}'")
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
            settings={"max_packdump_snapshot_count": 10,
                      "mc_version_policy": mc_version_policy},
        )
        profile.save()
        log.info(f"Profile '{name}' created")
        return profile

    @property
    def mc_version_policy(self) -> str:
        policy = self.settings.get("mc_version_policy", _DEFAULT_MC_VERSION_POLICY)
        return policy if policy in MC_VERSION_POLICIES else _DEFAULT_MC_VERSION_POLICY

    # Checks a single packdump against this profile's contract, returning a dict of issues.
    # Empty == good :)
    #
    # An "error" here means the dump is not a snapshot of THIS PACK, and adopting it would
    # silently reinterpret every tag the user owns against a registry that isn't theirs.
    # That is why both loader and mc_version are errors rather than warnings: the cost of
    # wrongly blocking is one explicit override, and the cost of wrongly accepting is
    # corruption you don't notice for weeks.
    def validate_packdump(self, packdump):
        issues = {}

        # Loader mismatch is a hard block — a Fabric dump is not a Forge pack.
        if self.loader and packdump.loader != self.loader:
            issues["loader"] = {
                "level": "error",
                "expected": self.loader,
                "actual": packdump.loader,
            }

        # MC version mismatch is a hard block too, subject to the profile's policy. This is
        # the common case in practice — people change Minecraft versions far more often
        # than they change loaders.
        if self.mc_version and not mc_versions_compatible(
                self.mc_version, packdump.mc_version, self.mc_version_policy):
            issues["mc_version"] = {
                "level": "error",
                "expected": self.mc_version,
                "actual": packdump.mc_version,
                "policy": self.mc_version_policy,
            }

        # Loader version change is informational — a new Forge build for the same
        # Minecraft version is a normal, compatible thing to do.
        if self.loader_version and packdump.loader_version != self.loader_version:
            issues["loader_version"] = {
                "level": "info",
                "expected": self.loader_version,
                "actual": packdump.loader_version,
            }

        return issues

    # Adopts a dump's identity for fields the profile never declared. A profile created
    # without a version has no contract at all, so the first dump it accepts defines one;
    # every later dump is then checked against it.
    def adopt_contract_from(self, packdump) -> list[str]:
        adopted = []
        for field, value in (("loader", packdump.loader),
                             ("mc_version", packdump.mc_version),
                             ("loader_version", packdump.loader_version)):
            if not getattr(self, field) and value:
                setattr(self, field, value)
                adopted.append(field)
        if adopted:
            self.save()
        return adopted

# Deletes a profile and everything in it: tags, views, jobs, history, authored packages,
# packdump snapshots. The Minecraft instance is NOT touched — a profile points at a game
# folder, it does not contain one, and deleting your work should never delete your game.
def delete_profile(name: str):
    root = GLOBAL_PATHS.profiles / name
    if not (root / "profile.json").exists():
        raise_log(ValueError, f"No profile named '{name}'")
    shutil.rmtree(root)
    # The remembered layout lives in app state, keyed by NAME — and a name is reusable, so
    # leaving it behind means the next profile called this inherits a dead one's furniture:
    # a View group holding view ids that no longer exist, column widths for deleted tags.
    forget_ui_state(name)
    log.info(f"Profile '{name}' deleted")


# Returns a list of available profile names.
def list_profiles() -> list[str]:
    if not GLOBAL_PATHS.profiles.is_dir():
        return []
    return [
        folder.name for folder in GLOBAL_PATHS.profiles.iterdir()
        if folder.is_dir() and (folder / "profile.json").exists()
    ]
