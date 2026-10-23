"""Getting from a `minecraft:sound_event` cell to the audio — the texture menu's sibling.

Same shape as `TextureMenu`, for the same reason: the registry table and the blueprint grid
both show sound events, and "which files play for this" is a fact about the event, not
about either widget. Both call `extend`; neither learns what a `sounds.json` is.

**Two verbs.** *Play* does what the game does — picks one of the event's files at random by
weight and plays it — because that is what "what does this sound like" means for an event
with six footstep variants. *Open* hands a specific file to whatever the OS opens `.ogg`
with, and when the event has several files it becomes a submenu naming each, since
auditioning *variant 3* is exactly the thing Play cannot do for you. An event with no files
(registered in code, never given sounds) offers nothing, per the rule the texture menu
follows: an absent verb beats a greyed one that invites you to hunt for what you did wrong.

The catalog arrives through a getter and is never held, like the packdump in `TextureMenu`:
the owner rebuilds it when the pack changes, and holding one here is how it would go stale.
"""
from dataclasses import dataclass

from PySide6.QtWidgets import QMenu

from packsmith.core.sounds import SOUND_EVENTS


@dataclass(frozen=True)
class SoundTarget:
    """One event and everything that may play for it."""
    event_id: str
    files: tuple

    @property
    def count(self) -> int:
        return len(self.files)


class SoundMenu:
    def __init__(self, catalog_getter, on_play, on_open):
        self._catalog = catalog_getter
        self._on_play = on_play
        self._on_open = on_open

    def target(self, registry_type: str, entry_id: str):
        if registry_type != SOUND_EVENTS or not entry_id:
            return None
        catalog = self._catalog()
        if catalog is None:
            return None
        files = tuple(catalog.files(entry_id))
        return SoundTarget(entry_id, files) if files else None

    def extend(self, menu, registry_type: str, entry_id: str) -> bool:
        """Add the sound actions to `menu`. Returns whether anything was added."""
        found = self.target(registry_type, entry_id)
        if found is None:
            return False
        detail = "\n".join(f.name for f in found.files)
        play = menu.addAction("Play Sound", lambda: self._on_play(found))
        play.setToolTip(detail if found.count == 1
                        else f"one of {found.count}, at random:\n{detail}")
        if found.count == 1:
            single = found.files[0]
            open_ = menu.addAction("Open Sound", lambda: self._on_open(found, single))
            open_.setToolTip(single.member)
        else:
            # Parented explicitly: `addMenu(str)` hands back a wrapper PySide can drop
            # before the menu shows, and a submenu that vanishes fails without a trace.
            sub = QMenu(f"Open Sound ({found.count})", menu)
            menu.addMenu(sub)
            # Names alone can collide — `step1.ogg` under two directories — so the label
            # is the path below `sounds/`, which is unique within an event and still short.
            for f in found.files:
                label = f.member.split("/sounds/", 1)[-1] if "/sounds/" in f.member else f.name
                action = sub.addAction(label, lambda f=f: self._on_open(found, f))
                action.setToolTip(f.member)
        return True
