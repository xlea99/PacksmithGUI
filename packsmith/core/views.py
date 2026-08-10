"""Saved Views (design 3.2.3) — Layer 2's third primitive.

A **View** is a named ``(query, renderer, renderer config)`` triple. Because a query is
serializable data (design 3.2.4), persisting one is just storing its JSON — no bespoke
config format, and the same bytes that round-trip through the constructor round-trip
through the database.

Views are **user-only**: §7.4 grants actions ``views.read`` and deliberately no
``views.write``, so none of the tag-ownership machinery (owner_kind, conflict policy)
applies here. Whoever the user is, they own their views.

Storage note: identity is the surrogate ``id``, not the name — so a rename is a rename,
not a delete-and-recreate, and anything holding a view id keeps working.
"""
import json
import sqlite3
from dataclasses import dataclass, field

from packsmith.core.query import to_dict, from_dict

DEFAULT_RENDERER = "registry_table"


@dataclass
class View:
    """A saved view. ``query`` is a live AST object, not JSON — callers work with the
    query directly and the store handles (de)serialization."""
    id: int
    name: str
    query: object
    renderer: str = DEFAULT_RENDERER
    renderer_config: dict = field(default_factory=dict)
    position: int = 0


class ViewStore:
    """CRUD over saved views."""

    def __init__(self, db):
        self._db = db

    # --- reads -------------------------------------------------------------

    def all(self) -> list[View]:
        """Every saved view that can be loaded, in display order.

        One unreadable row must not take the panel with it. A view is a saved query, and a
        query is data that outlives the code that wrote it — a row from a newer PackSmith,
        a hand-edited profile, or a node the decoder doesn't know yet is a real possibility,
        and losing access to nine healthy views because the tenth is broken is a far worse
        failure than the broken one. Casualties are reported via :meth:`unreadable`.
        """
        rows = self._db.fetch_all("SELECT * FROM views ORDER BY position, id")
        loaded, broken = [], []
        for row in rows:
            try:
                loaded.append(self._row_to_view(row))
            except Exception as e:
                broken.append((row["id"], row["name"], f"{type(e).__name__}: {e}"))
        self._unreadable = broken
        return loaded

    def unreadable(self) -> list:
        """``[(id, name, reason)]`` for rows the last :meth:`all` could not decode."""
        return list(getattr(self, "_unreadable", []))

    def get(self, view_id: int) -> View | None:
        row = self._db.fetch_one("SELECT * FROM views WHERE id = ?", (view_id,))
        return self._row_to_view(row) if row else None

    def get_by_name(self, name: str) -> View | None:
        row = self._db.fetch_one("SELECT * FROM views WHERE name = ?", (name,))
        return self._row_to_view(row) if row else None

    @property
    def count(self) -> int:
        row = self._db.fetch_one("SELECT COUNT(*) AS n FROM views")
        return row["n"] if row else 0

    # --- writes ------------------------------------------------------------

    def create(self, name: str, query, *, renderer: str = DEFAULT_RENDERER,
               renderer_config: dict = None, position: int = None) -> View:
        """Save a new view. Raises ValueError if the name is already taken."""
        name = (name or "").strip()
        if not name:
            raise ValueError("A view needs a name")
        if position is None:
            position = self._next_position()
        try:
            cur = self._db.execute(
                """INSERT INTO views (name, query_json, renderer, renderer_config, position)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, json.dumps(to_dict(query)), renderer,
                 json.dumps(renderer_config) if renderer_config else None, position),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"A view named '{name}' already exists")
        return View(id=cur.lastrowid, name=name, query=query, renderer=renderer,
                    renderer_config=renderer_config or {}, position=position)

    def update_query(self, view_id: int, query):
        """Persist an edit made through the constructor."""
        self._db.execute("UPDATE views SET query_json = ? WHERE id = ?",
                         (json.dumps(to_dict(query)), view_id))

    def rename(self, view_id: int, name: str):
        name = (name or "").strip()
        if not name:
            raise ValueError("A view needs a name")
        try:
            self._db.execute("UPDATE views SET name = ? WHERE id = ?", (name, view_id))
        except sqlite3.IntegrityError:
            raise ValueError(f"A view named '{name}' already exists")

    def set_renderer_config(self, view_id: int, renderer_config: dict):
        self._db.execute("UPDATE views SET renderer_config = ? WHERE id = ?",
                         (json.dumps(renderer_config) if renderer_config else None, view_id))

    def reorder(self, view_ids: list[int]):
        """Set display order from a list of ids, first to last."""
        for position, view_id in enumerate(view_ids):
            self._db.execute("UPDATE views SET position = ? WHERE id = ?", (position, view_id))

    def delete(self, view_id: int):
        """Remove a view permanently. Distinct from closing its tab, which only closes a
        window onto it."""
        self._db.execute("DELETE FROM views WHERE id = ?", (view_id,))

    # --- helpers -----------------------------------------------------------

    def _next_position(self) -> int:
        row = self._db.fetch_one("SELECT MAX(position) AS p FROM views")
        current = row["p"] if row and row["p"] is not None else -1
        return current + 1

    @staticmethod
    def _row_to_view(row) -> View:
        raw_config = row["renderer_config"]
        return View(
            id=row["id"],
            name=row["name"],
            query=from_dict(json.loads(row["query_json"])),
            renderer=row["renderer"],
            renderer_config=json.loads(raw_config) if raw_config else {},
            position=row["position"],
        )
