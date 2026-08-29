"""Getting from a registry entry to its PNG — design 6.5's override, reached from a cell.

§6.5 already copies a file out of a mod jar into a resource pack. That works, and it starts
in the wrong place: it needs you to know which jar ships the block you are looking at, and
then which of its three thousand members is the texture. The packdump knows both (§3.1,
schema 4 and 5) and has known them for a while without anything in the GUI asking.

So this adds no mechanism. It adds the **edge** from a thing you are looking at to the file
that draws it, and everything past that point is the override flow that already existed.

**Two verbs, and which you get is decided by the data rather than by a submenu.** A block
that renders offers *view* and *override*; a block that renders as nothing offers *create*,
because there is nothing to look at and the file does not exist yet. That is §6.5's own
taste — the jar viewer picks a save target from the member's path instead of asking — and
§5.1's, which omits an action rather than greying it out, on the grounds that a disabled
verb makes you hunt for what you did wrong when the answer is that the column has no such
concept.

**The packdump arrives through a getter, never held.** A holder that caches it goes silently
stale the moment a dump is imported, which is why anything keeping one has to implement
`set_packdump` and join `_packdump_holders`. Asking for it per menu is cheaper than joining
that protocol and cannot drift from it.
"""
from dataclasses import dataclass

from packsmith.core.packdump import MISSING_TEXTURE
from packsmith.core.textures import texture_member

BLOCKS = "minecraft:block"
ITEMS = "minecraft:item"


@dataclass(frozen=True)
class TextureTarget:
    """One entry's texture: which block answers for it, and where its PNG lives."""
    entry_id: str
    block_id: str
    texture_id: str
    member: str
    drawn: bool

    @property
    def verb(self) -> str:
        return "Override" if self.drawn else "Create"


def resolve(packdump, registry_type: str, entry_id: str):
    """The `TextureTarget` for an entry, or None when it has no texture to speak of.

    **Items reach their texture through their block**, which is why `places_block` exists
    (§3.1). The dump records block textures only — deliberately, since an item's inventory
    model is a different fact and `places_block` already answers which block an item is
    about. So a sword or an ingot resolves to nothing here, and correctly: its texture is
    not a block texture and pretending otherwise would offer to override a file that has no
    bearing on it.

    None is also the answer for a block whose model names no texture *at all* — Create's
    `fake_track` and `water_wheel_structure` are genuinely in that state. There is nothing
    to open and nowhere to create, and inventing a path to fill the hole would write a file
    the game never reads.
    """
    if packdump is None or not entry_id:
        return None
    if registry_type == BLOCKS:
        block_id = entry_id
    elif registry_type == ITEMS:
        block_id = packdump.attribute(ITEMS, entry_id, "places_block")
    else:
        return None
    if not block_id:
        return None

    texture_id = packdump.texture_source(BLOCKS, block_id)
    member = texture_member(texture_id)
    if not member:
        return None
    rendered = packdump.attribute(BLOCKS, block_id, "texture")
    return TextureTarget(entry_id=entry_id, block_id=block_id, texture_id=texture_id,
                         member=member, drawn=bool(rendered)
                         and rendered != MISSING_TEXTURE)


class TextureMenu:
    """The two context-menu entries, shared by every view that shows registry entries.

    One object rather than a mixin per widget: the registry table and the blueprint grid
    display the same entries for the same reasons, and a rule about which of them has a
    texture is not a fact about either widget. Both call `extend` and neither learns what a
    resource pack is.
    """

    def __init__(self, packdump_getter, on_view, on_write, on_copy=None):
        self._packdump = packdump_getter
        self._on_view = on_view
        self._on_write = on_write
        self._on_copy = on_copy

    def target(self, registry_type: str, entry_id: str):
        return resolve(self._packdump(), registry_type, entry_id)

    def extend(self, menu, registry_type: str, entry_id: str) -> bool:
        """Add the texture actions to `menu`. Returns whether anything was added.

        The caller uses the return to decide about a separator, which is the only thing it
        should have to know: a separator above nothing is a stray line, and a menu that
        renders one is a menu that has quietly stopped offering the actions.
        """
        found = self.target(registry_type, entry_id)
        if found is None:
            return False
        added = []
        if found.drawn:
            added.append(menu.addAction("View Texture", lambda: self._on_view(found)))
            # Beside View and on the same condition, because they answer the same question
            # — "let me at the actual PNG" — and differ only in where it goes. A block with
            # no art has nothing to copy, so this is absent there rather than handing over
            # the blank canvas `Create` is about to make.
            if self._on_copy is not None:
                added.append(menu.addAction("Copy Texture",
                                            lambda: self._on_copy(found)))
        added.append(menu.addAction(f"{found.verb} Texture…",
                                    lambda: self._on_write(found)))

        # An item's texture IS its block's, and for most items that is the same id said
        # twice. For the ~114 of 18,000 where it is not — `bakery:baguette` places
        # `bakery:baguette_block` — acting on the block while the menu says the item's name
        # is a small lie, and the honest fix is to say which block rather than to withhold
        # the action. The dump records block textures only (§3.1), so there is no item
        # sprite to offer instead; hiding this would leave the user with nothing.
        detail = found.texture_id
        if found.block_id != found.entry_id:
            detail = f"{found.block_id}\n{found.texture_id}"
        for action in added:
            action.setToolTip(detail)
        return True
