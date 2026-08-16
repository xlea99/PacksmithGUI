"""Reordering a loader's packs — a stopgap, and honestly so (design 8.1).

**Load order is not a setting.** It is pack content: it decides which override wins, which
makes it the user's data in exactly the way a tag or a blueprint is, and no more an
application preference than plugin order is a Mod Organizer preference. It is reachable
from Settings today because Settings is where the loader is chosen and that was the fastest
honest place to hang it — not because it belongs there.

Where it belongs is the Files panel's Datapacks category (§6.2), next to the packs
themselves. When that exists, this dialog should be reachable from there and the Settings
button becomes a shortcut rather than the only door.

The dialog itself is deliberately plain: a list and two arrows. Ordering is a rare,
low-volume act — a handful of packs, moved once — and anything fancier would be effort
spent where nobody is looking.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QPushButton,
    QDialogButtonBox, )

from packsmith.gui.shell import icons, style
from packsmith.gui.shell.dropdown import DropDown

KINDS = (("datapacks", "Datapacks"), ("resourcepacks", "Resource Packs"))


class LoadOrderDialog(QDialog):
    """Move a loader's packs up and down. Saves through the provider on OK."""

    def __init__(self, provider, instance_root, *, kinds=KINDS, parent=None):
        super().__init__(parent)
        self._provider = provider
        self._root = instance_root
        self._kinds = list(kinds)
        self._orders = {}            # kind -> [names], as edited
        self.saved = False

        self.setWindowTitle(f"Load order — {provider.name}")
        self.setMinimumSize(420, 340)

        root = QVBoxLayout(self)
        root.addWidget(self._explain(
            "Later packs override earlier ones. This is the order the game loads them in."))

        self._kind = DropDown()
        for key, label in self._kinds:
            self._kind.addItem(label, key)
        self._kind.currentIndexChanged.connect(self._show_kind)
        if len(self._kinds) > 1:
            root.addWidget(self._kind)

        row = QHBoxLayout()
        self._list = QListWidget()
        self._list.setStyleSheet(style.LIST_QSS)
        row.addWidget(self._list, 1)

        arrows = QVBoxLayout()
        arrows.addStretch()
        self._up = QPushButton()
        icons.mark(self._up, "up")
        self._up.setFixedWidth(34)
        self._up.clicked.connect(lambda: self._move(-1))
        arrows.addWidget(self._up)
        self._down = QPushButton()
        icons.mark(self._down, "down")
        self._down.setFixedWidth(34)
        self._down.clicked.connect(lambda: self._move(1))
        arrows.addWidget(self._down)
        arrows.addStretch()
        row.addLayout(arrows)
        root.addLayout(row)

        self._empty = self._explain("")
        root.addWidget(self._empty)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        for key, _ in self._kinds:
            self._orders[key] = list(provider.packs(instance_root, key))
        self._show_kind()

    @staticmethod
    def _explain(text):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
        return label

    @property
    def current_kind(self) -> str:
        return self._kind.currentData()

    def _show_kind(self):
        kind = self.current_kind
        self._list.clear()
        self._list.addItems(self._orders.get(kind, []))
        if self._list.count():
            self._list.setCurrentRow(0)
            self._empty.hide()
        else:
            noun = "datapacks" if kind == "datapacks" else "resource packs"
            self._empty.setText(f"No {noun} yet — nothing to order.")
            self._empty.show()
        self._sync_arrows()

    def _sync_arrows(self):
        row, count = self._list.currentRow(), self._list.count()
        self._up.setEnabled(row > 0)
        self._down.setEnabled(0 <= row < count - 1)

    def _move(self, delta: int):
        row = self._list.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self._list.count():
            return
        order = self._orders[self.current_kind]
        order[row], order[target] = order[target], order[row]
        self._list.insertItem(target, self._list.takeItem(row))
        self._list.setCurrentRow(target)
        self._sync_arrows()

    def order_for(self, kind: str) -> list:
        return list(self._orders.get(kind, []))

    def accept(self):
        """Write every kind, not just the one on screen — the combo is a view, and a user
        who reordered both and then switched tabs would otherwise lose one of them."""
        for kind, _ in self._kinds:
            try:
                self._provider.set_load_order(self._root, self._orders[kind], kind)
            except Exception:
                # One kind failing must not discard the other; the provider is the thing
                # that knows why, and it has already refused.
                continue
        self.saved = True
        super().accept()
