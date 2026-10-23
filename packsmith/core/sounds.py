"""From a sound event to the `.ogg` files that play for it.

A `minecraft:sound_event` entry is a name, and nothing about the name says where the audio
is. The game resolves it through `assets/<ns>/sounds.json`, which maps the event's path to a
list of *sounds* — each either a file (`assets/<ns>/sounds/<path>.ogg`) or a reference to
another event — with an optional weight, volume and pitch. Playing an event means picking
one of those at random by weight, which is why a footstep never sounds the same twice.

**`sounds.json` merges; it does not shadow.** A resource pack's copy adds its sounds to the
events it names and leaves every other event alone, unless an entry says `"replace": true`,
in which case it wipes what the packs beneath it had for that one event. So this reads every
copy the index can see and folds them in the game's order — lowest priority first — rather
than taking the first hit the way a texture lookup does.

Vanilla's own `sounds.json` and `.ogg` files are not in the client jar but in the launcher's
hashed asset store; `textures.AssetObjects` presents that store as an ordinary source, so
nothing here knows the difference.
"""
import json
from dataclasses import dataclass


SOUND_EVENTS = "minecraft:sound_event"

# How deep an event may refer to other events before we stop following. Real data nests
# once or twice; a cycle would nest forever.
_MAX_EVENT_DEPTH = 8


@dataclass(frozen=True)
class SoundFile:
    """One file that may play for an event, with the game's per-sound tweaks."""
    member: str             # assets/<ns>/sounds/<path>.ogg
    weight: int = 1
    volume: float = 1.0
    pitch: float = 1.0

    @property
    def name(self) -> str:
        return self.member.rsplit("/", 1)[-1]


def split_id(identifier: str):
    """``"twigs:block/x"`` -> ``("twigs", "block/x")``; unqualified means vanilla."""
    if not identifier or not isinstance(identifier, str):
        return None, None
    namespace, colon, path = identifier.partition(":")
    if not colon:
        namespace, path = "minecraft", namespace
    if not namespace or not path or ".." in path:
        return None, None
    return namespace, path


def sound_member(sound_name: str):
    """``"twigs:block/stone/step1"`` -> ``"assets/twigs/sounds/block/stone/step1.ogg"``."""
    namespace, path = split_id(sound_name)
    if namespace is None:
        return None
    return f"assets/{namespace}/sounds/{path}.ogg"


def sounds_json_member(namespace: str) -> str:
    return f"assets/{namespace}/sounds.json"


def merge_sounds_json(copies) -> dict:
    """Fold several `sounds.json` documents, lowest priority first, into one event map.

    Returns ``{event_path: [sound, ...]}`` where each sound is the raw entry (a string or a
    dict). Documents that are not JSON or not an object are skipped rather than failing the
    lot: one mod's typo should cost that mod's sounds, not everybody's.
    """
    merged = {}
    for raw in copies:
        try:
            doc = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(doc, dict):
            continue
        for event, entry in doc.items():
            if not isinstance(entry, dict):
                continue
            sounds = entry.get("sounds")
            if not isinstance(sounds, list):
                continue
            if entry.get("replace") is True or event not in merged:
                merged[event] = list(sounds)
            else:
                merged[event].extend(sounds)
    return merged


class SoundCatalog:
    """Which files play for a sound event, looked up through an asset index.

    Holds the merged `sounds.json` per namespace, built on first ask — vanilla's alone is
    a megabyte of JSON, and re-parsing it for every right-click would be felt. The catalog
    is owned by whoever owns the index and dropped when the index is, so it can never
    outlive the sources it was read from.
    """

    def __init__(self, index):
        self.index = index
        self._events = {}           # namespace -> {event_path: [raw sound entries]}

    def _events_for(self, namespace: str) -> dict:
        if namespace not in self._events:
            copies = self.index.find_all(sounds_json_member(namespace))
            # find_all is most-specific first; the game applies lowest priority first so
            # that a later pack's `replace` wins. Reverse to walk it the game's way.
            self._events[namespace] = merge_sounds_json(data for data, _ in reversed(copies))
        return self._events[namespace]

    def files(self, event_id: str) -> list:
        """Every `SoundFile` that may play for `event_id`, references followed.

        An event nobody has given sounds — registered in code, never listed in any
        `sounds.json` — comes back empty, which the menu reads as "nothing to offer".
        """
        return self._resolve(event_id, depth=0, seen=set())

    def _resolve(self, event_id, depth, seen) -> list:
        namespace, path = split_id(event_id)
        if namespace is None or depth > _MAX_EVENT_DEPTH or (namespace, path) in seen:
            return []
        seen = seen | {(namespace, path)}
        out = []
        for raw in self._events_for(namespace).get(path, []):
            if isinstance(raw, str):
                raw = {"name": raw}
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                continue
            name = raw["name"]
            weight = raw.get("weight", 1)
            volume = raw.get("volume", 1.0)
            pitch = raw.get("pitch", 1.0)
            if raw.get("type") == "event":
                # An event that delegates to another: its own weight/volume/pitch apply on
                # top of whatever the referenced event's sounds carry.
                for inner in self._resolve(name, depth + 1, seen):
                    out.append(SoundFile(inner.member, _as_int(weight, 1) * inner.weight,
                                         _as_float(volume, 1.0) * inner.volume,
                                         _as_float(pitch, 1.0) * inner.pitch))
                continue
            member = sound_member(name)
            if member:
                out.append(SoundFile(member, _as_int(weight, 1), _as_float(volume, 1.0),
                                     _as_float(pitch, 1.0)))
        return out

    def read(self, sound: SoundFile):
        """``(bytes, source)`` for a resolved file, or ``(None, None)`` if nobody ships it."""
        return self.index.find(sound.member)


def choose(files: list, rng) -> "SoundFile | None":
    """Pick one file the way the game does: at random, weighted."""
    if not files:
        return None
    total = sum(max(0, f.weight) for f in files)
    if total <= 0:
        return files[rng.randrange(len(files))]
    roll = rng.randrange(total)
    for f in files:
        roll -= max(0, f.weight)
        if roll < 0:
            return f
    return files[-1]


def _as_int(value, default):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
