"""The query result — always a table (design 3.2.4).

Every query, without exception, returns ``Result``: ordered columns (each carrying a
type) and ordered rows. A row is either *entry-backed* (carries the id it came from ->
editable in a View) or *computed* (an aggregate/distinct output with no single entry
behind it -> read-only). Editability is a consequence of the query's shape, not a flag.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Column:
    name: str
    type: str   # bool | string | enum | number | id


@dataclass
class Row:
    values: dict                    # column name -> value
    entry_id: str | None = None     # the backing entry (set) or None for computed rows

    @property
    def editable(self) -> bool:
        """True iff this row maps to a single registry entry you could write tags to."""
        return self.entry_id is not None


@dataclass
class Result:
    columns: list = field(default_factory=list)   # list[Column]
    rows: list = field(default_factory=list)       # list[Row]

    @property
    def column_names(self) -> list:
        return [c.name for c in self.columns]
