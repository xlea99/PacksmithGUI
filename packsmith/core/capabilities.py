"""Capabilities and providers — design 7.1.

A **capability** is a named interface an action asks for (`datapacks.write`). A
**provider** is the implementation that answers it in a given profile (Paxi, OpenLoader,
the built-in runtime). The split is the point: an action calling `pack.datapacks.write(...)`
works whichever loader is installed, and never learns which.

The granularity is per *capability*, not per provider, and that is load-bearing rather
than tidy. Paxi supports genuine load-order control and OpenLoader does not, so
`datapacks.ordering` is its own capability that one offers and the other doesn't — an
action that needs ordering declares it and is refused where it is absent, with the missing
capability named. Modelling loaders as interchangeable would make that impossible to say.

**Providers live in their own modules and base code never imports them**; a registry does.
That boundary is deliberately the same one a plugin system would need later (§3.4), so
adding real discovery is additive rather than a rewrite — but nothing here loads code
dynamically, because designing a *public* API before writing several providers means
freezing an interface shaped by whichever one came first.
"""
from dataclasses import dataclass, field

# --- capability names --------------------------------------------------------
# Strings, deliberately: an action declares what it needs by name, and the set is open —
# an integration may register capabilities nothing in this file has heard of (§7.5).

LOG = "log"
REGISTRY_READ = "registry.read"
TAGS_READ = "tags.read"
TAGS_WRITE = "tags.write"
FILESYSTEM_READ = "filesystem.read"
FILESYSTEM_WRITE = "filesystem.write"

DATAPACKS_READ = "datapacks.read"
DATAPACKS_WRITE = "datapacks.write"
DATAPACKS_ORDERING = "datapacks.ordering"
RESOURCEPACKS_READ = "resourcepacks.read"
RESOURCEPACKS_WRITE = "resourcepacks.write"
RESOURCEPACKS_ORDERING = "resourcepacks.ordering"

# What the runtime itself answers for, with no integration involved (§7.1's table).
BUILT_IN = (LOG, REGISTRY_READ, TAGS_READ, TAGS_WRITE, FILESYSTEM_READ, FILESYSTEM_WRITE)
BUILT_IN_PROVIDER = "built-in"


class CapabilityError(Exception):
    """An action needs a capability no active provider satisfies."""


@dataclass(frozen=True)
class Resolution:
    """Which provider answers a capability, and what else could have."""
    capability: str
    provider: str
    alternatives: tuple = ()

    @property
    def contested(self) -> bool:
        """More than one provider could answer this — so the choice is the user's (§7.1)."""
        return bool(self.alternatives)


class PackLoaderProvider:
    """What a global pack loader must be able to answer.

    Minecraft has no vanilla global datapack mechanism (§8.1), so every useful override
    workflow runs through one of these mods. The interface is small on purpose: it is the
    part that is genuinely common, and everything a specific loader does differently is
    expressed as a capability it does or doesn't offer.
    """

    name = "unnamed"
    mod_id = None                  # what to look for in the packdump's mod list
    capabilities = ()              # the most this loader can ever offer
    # Whether `mod_id` was read from a real jar's mods.toml or inferred. False means a
    # detection miss would be SILENT — the loader simply never appears — so it is recorded
    # rather than assumed correct. See tests/test_pack_loaders.py.
    mod_id_verified = False
    summary = ""                   # one line, for the settings dropdown

    def detect(self, packdump) -> bool:
        """Is this loader installed in the pack? (§8.1: scan the packdump's mod list.)"""
        if packdump is None or not self.mod_id:
            return False
        return self.mod_id in packdump.mods

    def available_capabilities(self, instance_root) -> tuple:
        """What this loader offers *in this instance*, which is not always `capabilities`.

        Three of the four supported loaders can be turned down in their own config files:
        Open Loader disables data or resource packs independently, Moonlight disables its
        global folder by setting the path to an empty string, and Global Packs is a list of
        paths that can simply be emptied. So "the mod is installed" does not mean "the
        capability is there", and asking the config is the only honest answer.

        ``instance_root`` of None means "answer statically" — for callers that have a
        packdump but no instance on disk.
        """
        return tuple(self.capabilities)

    def datapack_root(self, instance_root):
        raise NotImplementedError

    def resourcepack_root(self, instance_root):
        raise NotImplementedError

    def load_order(self, instance_root, kind="datapacks") -> list:
        """Pack names in load order, or [] when the loader has no such notion."""
        return []

    def set_load_order(self, instance_root, order, kind="datapacks") -> None:
        raise CapabilityError(f"{self.name} does not support load ordering")


@dataclass
class ResolutionTable:
    """Capability → provider for one profile (§7.1).

    Built at profile load. A capability with no provider is simply absent, which is what
    makes "can this action run here" answerable without special cases.
    """
    entries: dict = field(default_factory=dict)      # capability -> Resolution
    providers: dict = field(default_factory=dict)    # provider name -> object

    def provider_for(self, capability: str):
        entry = self.entries.get(capability)
        return self.providers.get(entry.provider) if entry else None

    def satisfies(self, capability: str) -> bool:
        return capability in self.entries

    def missing(self, required) -> list:
        """Which of ``required`` nothing answers — the satisfaction check (§7.1)."""
        return [name for name in required if name not in self.entries]

    def require(self, capability: str):
        """The provider for a capability, or a refusal that names what is missing."""
        provider = self.provider_for(capability)
        if provider is None:
            raise CapabilityError(
                f"nothing in this profile provides '{capability}' — install a mod that "
                f"does, or use an action that doesn't need it")
        return provider

    def as_rows(self) -> list:
        """The table as §7.1 draws it, for a panel to render."""
        return [(name, self.entries[name].provider,
                 ", ".join(self.entries[name].alternatives))
                for name in sorted(self.entries)]


# The user's sticky choice of loader (§8.1: "sticky per profile"). It stores a provider
# *name*, not an index — a list position would silently point at a different loader the
# moment one is installed or removed.
PACK_LOADER_SETTING = "pack_loader"

WRITE_CAPABILITY = {"datapacks": DATAPACKS_WRITE, "resourcepacks": RESOURCEPACKS_WRITE}
READ_CAPABILITY = {"datapacks": DATAPACKS_READ, "resourcepacks": RESOURCEPACKS_READ}


class PackTargets:
    """Which datapacks and resource packs this profile actually has (design 3.3 / 8.1).

    One object so that the three places that need the answer — the step editor's picker,
    the binding validator, and the `pack.datapacks` resolver — cannot disagree about what
    exists. All three route through the active loader, because a pack is only a pack if the
    loader that reads that directory is installed.
    """

    def __init__(self, table: "ResolutionTable", instance_root):
        self._table = table
        self._root = instance_root

    @property
    def root(self):
        return self._root

    @property
    def table(self):
        return self._table

    def provider_for(self, pack_kind: str):
        """The loader that answers for this kind, or None when nothing does."""
        if self._table is None or self._root is None:
            return None
        return self._table.provider_for(WRITE_CAPABILITY.get(pack_kind, ""))

    def available(self, pack_kind: str):
        """Pack names in LOAD ORDER, or None when no loader provides this kind at all.

        None and [] are different answers and the caller needs both: "there is no loader
        installed" points at §8.1, "you have no packs yet" points at making one.
        """
        provider = self.provider_for(pack_kind)
        if provider is None:
            return None
        return list(provider.packs(self._root, pack_kind))


def resolve(packdump, *, loaders=(), preferred=None, instance_root=None) -> ResolutionTable:
    """Build a profile's resolution table from the loaders that detect as present.

    ``preferred`` is the user's sticky per-profile choice (§8.1): when two loaders both
    provide `datapacks.write`, Packsmith picks a default and lets the user change it,
    rather than guessing differently on each launch.

    ``instance_root`` lets each loader read its own config and answer what it *actually*
    offers here — Open Loader with resource packs switched off provides `datapacks.*` and
    nothing else. Without it the answer falls back to each loader's static maximum, which
    is right for callers holding a packdump but no instance on disk.
    """
    table = ResolutionTable()
    for capability in BUILT_IN:
        table.entries[capability] = Resolution(capability, BUILT_IN_PROVIDER)
    table.providers[BUILT_IN_PROVIDER] = None

    active = [loader for loader in loaders if loader.detect(packdump)]
    # The preferred loader goes first, so it wins every capability it offers and the
    # others land in `alternatives` — which is exactly the disambiguation §7.1 describes.
    active.sort(key=lambda loader: (loader.name != preferred, loader.name))
    for loader in active:
        table.providers[loader.name] = loader
        for capability in loader.available_capabilities(instance_root):
            existing = table.entries.get(capability)
            if existing is None:
                table.entries[capability] = Resolution(capability, loader.name)
            else:
                table.entries[capability] = Resolution(
                    capability, existing.provider,
                    alternatives=existing.alternatives + (loader.name,))
    return table


# Minecraft rejects a pack whose `pack_format` doesn't match the version it is loaded in —
# with a warning that says "incompatible", which is a confusing thing to see on a pack you
# just made. The numbers are Mojang's and they change most releases; only versions this
# tool is plausibly used with are listed, and anything else falls back to the newest known
# rather than guessing a number that would be wrong in a specific way.
_PACK_FORMATS = {
    "1.20.5": (41, 32), "1.20.4": (26, 22), "1.20.3": (26, 22),
    "1.20.2": (18, 18), "1.20.1": (15, 15), "1.20": (15, 15),
    "1.19.4": (12, 13), "1.19.3": (10, 12), "1.19": (10, 9),
    "1.18.2": (9, 8), "1.18": (8, 8),
}
_NEWEST_KNOWN = (15, 15)


def pack_format_for(mc_version: str, kind: str = "datapacks", *, client_jar=None) -> int:
    """The `pack_format` a pack must declare to load on this Minecraft version.

    Prefers the number Minecraft itself states, in the client jar's `version.json`. The
    table below is a fallback for when the jar can't be found — useful, but memorised, and
    a memorised number is wrong in a specific and silent way the first time Mojang ships a
    version nobody updated it for.
    """
    if client_jar is not None:
        from packsmith.core.launchers import pack_version
        stated = pack_version(client_jar)
        if stated is not None:
            data_format, resource_format = stated
            return data_format if kind == "datapacks" else resource_format
    data_format, resource_format = _PACK_FORMATS.get(str(mc_version), _NEWEST_KNOWN)
    return data_format if kind == "datapacks" else resource_format


# Which kind of pack a jar member could be overridden into. Minecraft's own structure
# decides this, not the loader: `data/` is datapack territory and `assets/` is resource
# pack territory, and everything else in a mod jar — compiled classes, META-INF, the mod's
# own metadata — has no override target at all and never will (§6.3).
_OVERRIDE_ROOTS = {"data/": "datapacks", "assets/": "resourcepacks"}


def override_kind(member: str) -> str | None:
    """``"datapacks"``, ``"resourcepacks"``, or None if this file can't be overridden."""
    clean = str(member).replace(chr(92), "/").lstrip("/")
    if clean.lower().endswith(".class"):
        return None
    for root, kind in _OVERRIDE_ROOTS.items():
        if clean.startswith(root):
            return kind
    return None
