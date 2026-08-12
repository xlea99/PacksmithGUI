"""Choosing where an override lands — design 6.5 / 8.1.

§6.5's save-as-override, in one dialog: pick the pack that will shadow a mod's file, or
make a new one. The pack list comes from the active loader (§8.1), so this shows the same
packs the game will actually load, in the order it loads them.

The destination path is **shown and not editable**, because it isn't a choice. An override
works by sitting at the same namespace path the mod uses, inside a pack that loads later —
`data/minecraft/loot_tables/blocks/oak_leaves.json` has to stay exactly that. Offering a
path field would invite the one edit that silently produces a file the game never reads.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QLineEdit, QPushButton,
    QDialogButtonBox, QRadioButton, QButtonGroup, QWidget,
)

from packsmith.gui.shell import style

NEW_PACK = object()      # sentinel for "create one"


class OverrideTargetDialog(QDialog):
    """Which pack should this override go into?"""

    def __init__(self, member: str, kind: str, packs, *, default=None, parent=None):
        super().__init__(parent)
        noun = "datapack" if kind == "datapacks" else "resource pack"
        self.setWindowTitle(f"Save as override")
        self.setMinimumWidth(460)
        self.result_pack = None          # chosen name, or a new one
        self.result_is_new = False
        self._packs = list(packs)

        root = QVBoxLayout(self)

        headline = QLabel(f"Override <b>{member}</b>")
        headline.setTextFormat(Qt.RichText)
        headline.setWordWrap(True)
        root.addWidget(headline)

        explain = QLabel(
            f"This copies the file into a {noun}, keeping its path exactly as it is inside "
            f"the mod — that path is what makes it an override rather than an unrelated "
            f"file.")
        explain.setWordWrap(True)
        explain.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
        root.addWidget(explain)

        self._group = QButtonGroup(self)
        self._existing = QRadioButton(f"Into an existing {noun}")
        self._create = QRadioButton(f"Into a new {noun}")
        self._group.addButton(self._existing)
        self._group.addButton(self._create)

        root.addWidget(self._existing)
        self._combo = QComboBox()
        self._combo.addItems(self._packs)
        # The loader lists packs in LOAD ORDER, and later packs win. Saying so beats
        # letting someone put an override into a pack that something else overrides back.
        self._combo.setToolTip("Listed in load order — later packs override earlier ones")
        row = self._indented(self._combo)
        root.addWidget(row)

        root.addWidget(self._create)
        self._name = QLineEdit()
        self._name.setPlaceholderText(f"new {noun} name")
        root.addWidget(self._indented(self._name))

        if self._packs:
            self._existing.setChecked(True)
            if default and default in self._packs:
                self._combo.setCurrentText(default)     # sticky per profile (§8.1)
        else:
            # Nothing to choose between: the only honest option is to make one.
            self._existing.setEnabled(False)
            self._combo.setEnabled(False)
            self._create.setChecked(True)
        self._group.buttonToggled.connect(self._sync)
        self._sync()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _indented(widget):
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(22, 0, 0, 0)
        layout.addWidget(widget)
        return holder

    def _sync(self, *_):
        into_existing = self._existing.isChecked()
        self._combo.setEnabled(into_existing and bool(self._packs))
        self._name.setEnabled(not into_existing)

    def accept(self):
        if self._existing.isChecked():
            self.result_pack = self._combo.currentText()
            self.result_is_new = False
        else:
            name = self._name.text().strip()
            if not name or "/" in name or "\\" in name:
                self._name.setPlaceholderText("a name without slashes, please")
                self._name.clear()
                return
            self.result_pack = name
            self.result_is_new = True
        if not self.result_pack:
            return
        super().accept()
