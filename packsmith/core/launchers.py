"""Finding the real Minecraft client jar — design 3.1.

The packdump tells Packsmith what the *modded* registry looks like. It says nothing about
vanilla's own content: the loot table for `minecraft:oak_leaves`, the recipe for stone
bricks, the `minecraft:leaves` block tag. Those live in the client jar, which every player
of a modpack necessarily already has — 5,888 files under `data/` in 1.20.1 — and which
Packsmith can read without extracting anything (§6.5).

It is also where the authoritative `pack_format` lives (`version.json`), so a datapack
Packsmith creates declares the number Minecraft itself uses rather than one from a table
that rots every release.

**The hard part is not reading it, it is finding it.** Launchers do not agree, and there
is no registry to ask:

    CurseForge      <root>/Install/versions/<v>/<v>.jar
    Vanilla         <root>/versions/<v>/<v>.jar
    Prism / MultiMC <root>/libraries/com/mojang/minecraft/<v>/minecraft-<v>-client.jar
    Modrinth App    <root>/meta/versions/<v>/<v>.jar

So this is deliberately *three* strategies, ending in one that always works:

1. **An explicit path the user set.** Never guessed at, never overridden by detection.
2. **Detection**, by walking up from the instance directory and trying every known layout.
3. **Nothing** — reported honestly, so the caller degrades rather than inventing a path.

Adding a launcher is adding a row to `LAYOUTS`. That is the whole point of the shape: the
list of launchers will always be incomplete, so the cost of being wrong about one has to
be a line of data and not a new mechanism — and step 1 means a user on a launcher nobody
has heard of is never actually stuck.
"""
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

from packsmith.common.logging import log

# The profile setting that overrides detection entirely (strategy 1).
JAR_SETTING = "minecraft_jar"

# How far up from the instance directory to look for a launcher root. Instances sit one or
# two levels under it in every layout below; more than that and we would start matching
# unrelated Minecraft installs elsewhere on the disk.
_MAX_CLIMB = 4


@dataclass(frozen=True)
class JarLayout:
    """Where one launcher keeps its client jars, relative to its root."""
    launcher: str
    pattern: str            # `{version}` is substituted

    def path_in(self, root: Path, version: str) -> Path:
        return root / self.pattern.format(version=version)


# Ordered by how confident the match is, not alphabetically: CurseForge's `Install/` is a
# distinctive marker, while a bare `versions/` directory is common enough that it should
# not win against a more specific hit.
LAYOUTS = (
    JarLayout("CurseForge", "Install/versions/{version}/{version}.jar"),
    JarLayout("Modrinth App", "meta/versions/{version}/{version}.jar"),
    JarLayout("Prism / MultiMC",
              "libraries/com/mojang/minecraft/{version}/minecraft-{version}-client.jar"),
    JarLayout("Vanilla launcher", "versions/{version}/{version}.jar"),
)


@dataclass(frozen=True)
class LocatedJar:
    """A client jar, and how it was found — so the UI can say, and the user can correct."""
    path: Path
    version: str
    launcher: str           # a layout name, or "user setting"

    @property
    def user_set(self) -> bool:
        return self.launcher == "user setting"


def verify(path, version: str = None) -> str | None:
    """The Minecraft version a jar actually is, or None if it isn't a client jar.

    Checked rather than trusted, because a filename is a claim and `version.json` is a
    fact. Pointing at the wrong version silently would produce vanilla data for a game
    the user isn't running, which is worse than finding nothing — a missing answer is
    obvious, a wrong one isn't.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            payload = json.loads(archive.read("version.json"))
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return None
    found = str(payload.get("id") or "")
    if version is not None and found != version:
        return None
    return found or None


def pack_version(path) -> tuple | None:
    """``(data_format, resource_format)`` straight from the jar, or None.

    Mojang's own numbers. They do not follow a pattern — 10, 12, 15, 18, 26, 41 across
    1.19→1.20.5, with data and resource diverging in some releases — so this is the only
    way to be right about a version nobody has hardcoded.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            versions = json.loads(archive.read("version.json")).get("pack_version")
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
        return None
    if isinstance(versions, int):        # older jars stated a single number
        return versions, versions
    if isinstance(versions, dict):
        data = versions.get("data")
        resource = versions.get("resource")
        if data is not None and resource is not None:
            return int(data), int(resource)
    return None


def candidate_roots(instance_path) -> list:
    """Directories that might be a launcher root, nearest first.

    Walking up from the instance rather than scanning the disk: the instance is the one
    location Packsmith is certain about, every launcher keeps its jars near it, and a
    filesystem-wide search would be both slow and prone to finding somebody else's
    Minecraft.
    """
    roots = []
    current = Path(instance_path).resolve()
    for _ in range(_MAX_CLIMB):
        if current.parent == current:
            break
        current = current.parent
        roots.append(current)
    return roots


def locate(instance_path, version: str, *, override=None) -> LocatedJar | None:
    """Find the client jar for ``version``, or None.

    ``override`` is the user's explicit setting and is tried first and alone: if someone
    has told Packsmith where the jar is, quietly preferring a detected one would make the
    setting a suggestion.
    """
    if override:
        path = Path(override)
        if verify(path, version):
            return LocatedJar(path, version, "user setting")
        log.warning(f"The configured Minecraft jar isn't usable for {version}: {path}")
        return None

    for root in candidate_roots(instance_path):
        for layout in LAYOUTS:
            path = layout.path_in(root, version)
            if path.is_file() and verify(path, version):
                log.info(f"Found the {version} client jar via {layout.launcher}: {path}")
                return LocatedJar(path, version, layout.launcher)
    return None


def locate_for(profile) -> LocatedJar | None:
    """The client jar for a profile, honouring its override setting."""
    if not profile.mc_path or not profile.mc_version:
        return None
    return locate(profile.mc_path, profile.mc_version,
                  override=profile.settings.get(JAR_SETTING))
