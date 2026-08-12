"""Creating, opening and deleting profiles (design 3.1).

A profile is one pack as a unit: the Minecraft instance, its packdump snapshots, and every
piece of Layer 2 data written against it. Switching profiles is therefore not a filter — it
is a different world, which is why the window rebuilds around it.

The one piece of real cleverness here is that **New Profile reads the instance's packdump
to fill in its own contract**. The loader and version aren't trivia the user should have to
recite; they are facts about the folder they just picked, and the Forge mod already wrote
them down. Asking would only create an opportunity to get it wrong.
"""
import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QLabel, QComboBox,
    QPushButton, QDialogButtonBox, QMessageBox, QFileDialog, QAbstractItemView,
    QTreeWidgetItem,
)

from packsmith.core.profile import MC_VERSION_POLICIES, list_profiles, Profile
from packsmith.gui.shell import style
from packsmith.gui.shell.tree import PanelTree

_POLICY_LABELS = {
    "strict": "Strict — any Minecraft version change is a different pack (recommended)",
    "same_minor": "Same minor — allow patch bumps (1.7.6 → 1.7.10). Legacy packs only",
}


def _hint(text, colour=None):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {colour or style.TEXT_FAINT}; font-size: 11px;")
    return label


def read_instance_contract(mc_path) -> dict:
    """Loader/version facts from an instance's packdump, or {} if it has none.

    Deliberately reads meta.json directly rather than going through ``Packdump.load``:
    this runs while the user is still typing a path, and a half-written or unreadable dump
    should leave the fields blank, not raise at them.
    """
    meta_path = Path(mc_path) / "packsmith" / "meta.json"
    if not meta_path.is_file():
        return {}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {
        "mc_version": meta.get("minecraft_version") or "",
        "loader": meta.get("loader") or "",
        "loader_version": meta.get("loader_version") or "",
        "mods": len(meta.get("mods") or []),
    }


class OpenProfileDialog(QDialog):
    """Pick a profile to open. Also the only place profiles get deleted from."""

    def __init__(self, current: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Open Profile")
        self.setMinimumSize(620, 340)
        self._current = current
        self.result_name = ""
        self.result_deleted = []

        root = QVBoxLayout(self)
        root.setSpacing(8)

        self._tree = PanelTree()
        self._tree.setColumnCount(4)
        self._tree.setHeaderLabels(["Profile", "Minecraft", "Loader", "Instance"])
        self._tree.setRootIsDecorated(False)
        self._tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._tree.itemDoubleClicked.connect(lambda *_: self.accept())
        root.addWidget(self._tree)

        buttons = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Cancel)
        self._delete = QPushButton("Delete…")
        self._delete.clicked.connect(self._delete_selected)
        buttons.addButton(self._delete, QDialogButtonBox.DestructiveRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._reload()

    def _reload(self):
        self._tree.clear()
        for name in sorted(list_profiles()):
            try:
                profile = Profile.load(name)
            except Exception as e:
                item = QTreeWidgetItem([name, "unreadable", "", str(e)])
                item.setForeground(0, Qt.gray)
                self._tree.addTopLevelItem(item)
                continue
            label = f"{name}   (open)" if name == self._current else name
            item = QTreeWidgetItem([
                label, profile.mc_version or "—",
                f"{profile.loader or '—'} {profile.loader_version or ''}".strip(),
                str(profile.mc_path or "—"),
            ])
            item.setData(0, Qt.UserRole, name)
            self._tree.addTopLevelItem(item)
            if name == self._current:
                item.setSelected(True)
        for column in range(3):
            self._tree.resizeColumnToContents(column)

    def _selected(self) -> str:
        items = self._tree.selectedItems()
        return items[0].data(0, Qt.UserRole) if items else ""

    def _delete_selected(self):
        name = self._selected()
        if not name:
            return
        if name == self._current:
            QMessageBox.information(
                self, "Profile is open",
                f"'{name}' is the profile you're in. Open a different one first.")
            return
        # Type-to-confirm: this destroys every tag, view, job, run and authored package in
        # the profile, and none of it is recoverable.
        typed, ok = _confirm_by_typing(
            self, "Delete Profile",
            f"<b>Delete the profile '{name}'?</b><br><br>"
            f"This permanently deletes its tags, views, jobs, run history, authored "
            f"packages and packdump snapshots.<br><br>"
            f"Your Minecraft instance is <b>not</b> touched.",
            expected=name, prompt="Type the profile name to confirm:")
        if not ok:
            return
        from packsmith.core.profile import delete_profile
        try:
            delete_profile(name)
        except Exception as e:
            QMessageBox.warning(self, "Couldn't delete", str(e))
            return
        self.result_deleted.append(name)
        self._reload()

    def accept(self):
        name = self._selected()
        if not name:
            QMessageBox.information(self, "Nothing selected", "Pick a profile to open.")
            return
        if name == self._current:
            self.reject()
            return
        self.result_name = name
        super().accept()


class NewProfileDialog(QDialog):
    """Name it, point it at an instance, and let the packdump fill in the rest."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Profile")
        self.setMinimumWidth(560)

        self.result_name = ""
        self.result_mc_path = ""
        self.result_mc_version = ""
        self.result_loader = ""
        self.result_loader_version = ""
        self.result_policy = "strict"

        root = QVBoxLayout(self)
        root.setSpacing(8)
        form = QFormLayout()
        form.setSpacing(6)

        self._name = QLineEdit()
        self._name.setPlaceholderText("deep_end")
        form.addRow("Profile name", self._name)

        path_row = QHBoxLayout()
        self._path = QLineEdit()
        self._path.setPlaceholderText(r"...\curseforge\minecraft\Instances\My Pack")
        self._path.textChanged.connect(self._detect)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self._path)
        path_row.addWidget(browse)
        form.addRow("Minecraft instance", path_row)

        self._detected = _hint("")
        form.addRow("", self._detected)

        self._mc_version = QLineEdit()
        self._mc_version.setPlaceholderText("1.20.1")
        form.addRow("Minecraft version", self._mc_version)

        self._loader = QLineEdit()
        self._loader.setPlaceholderText("forge")
        form.addRow("Loader", self._loader)

        self._loader_version = QLineEdit()
        self._loader_version.setPlaceholderText("47.4.10")
        form.addRow("Loader version", self._loader_version)

        self._policy = QComboBox()
        for key in MC_VERSION_POLICIES:
            self._policy.addItem(_POLICY_LABELS[key], key)
        form.addRow("Version policy", self._policy)

        root.addLayout(form)
        root.addWidget(_hint(
            "The Minecraft version and loader are the profile's contract: a packdump that "
            "doesn't match is refused rather than imported, because it describes a "
            "different game. Leave them blank and the first packdump you import will fill "
            "them in."))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _browse(self):
        chosen = QFileDialog.getExistingDirectory(self, "Select the Minecraft instance")
        if chosen:
            self._path.setText(chosen)

    def _detect(self):
        """Fill the contract from the instance's own packdump as soon as it's pointed at
        one. These are facts about the folder, not questions worth asking."""
        path = self._path.text().strip()
        if not path:
            self._detected.setText("")
            return
        found = read_instance_contract(path)
        if not found:
            self._detected.setText(
                "No packdump in this folder yet — run the Packsmith mod in the instance, "
                "or fill the contract in by hand.")
            self._detected.setStyleSheet(
                f"color: {style.OWNER_ACTION}; font-size: 11px;")
            return
        self._detected.setText(
            f"Packdump found: {found['mc_version']} {found['loader']} "
            f"{found['loader_version']}, {found['mods']} mods")
        self._detected.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
        for field, value in ((self._mc_version, found["mc_version"]),
                             (self._loader, found["loader"]),
                             (self._loader_version, found["loader_version"])):
            if not field.text().strip():
                field.setText(value)

    def accept(self):
        name = self._name.text().strip()
        if not name:
            QMessageBox.warning(self, "Name required", "The profile needs a name.")
            return
        if name in list_profiles():
            QMessageBox.warning(self, "Name taken",
                                f"A profile named '{name}' already exists.")
            return
        path = self._path.text().strip()
        if not path or not Path(path).is_dir():
            QMessageBox.warning(self, "Instance required",
                                "Point the profile at an existing Minecraft instance "
                                "folder.")
            return

        self.result_name = name
        self.result_mc_path = path
        self.result_mc_version = self._mc_version.text().strip()
        self.result_loader = self._loader.text().strip()
        self.result_loader_version = self._loader_version.text().strip()
        self.result_policy = self._policy.currentData()
        super().accept()


def _confirm_by_typing(parent, title, message, *, expected, prompt,
                       ok_text="Confirm") -> tuple:
    """A confirmation you cannot give by reflex (the §6.1 move, again).

    Returns (typed, ok). The OK button stays disabled until the exact string is typed, so
    muscle memory can't carry the user through a decision they'd want to have read.
    """
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setMinimumWidth(520)
    layout = QVBoxLayout(dialog)
    layout.setSpacing(10)

    body = QLabel(message)
    body.setWordWrap(True)
    body.setTextFormat(Qt.RichText)
    body.setStyleSheet(f"color: {style.TEXT}; font-size: 12px;")
    layout.addWidget(body)

    layout.addWidget(_hint(prompt, style.TEXT_MUTED))
    field = QLineEdit()
    field.setPlaceholderText(expected)
    layout.addWidget(field)

    buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
    confirm = buttons.addButton(ok_text, QDialogButtonBox.AcceptRole)
    confirm.setEnabled(False)
    confirm.setStyleSheet(
        f"QPushButton {{ color: {style.ERROR}; font-weight: bold; }}")
    field.textChanged.connect(lambda text: confirm.setEnabled(text.strip() == expected))
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    ok = bool(dialog.exec())
    return field.text().strip(), ok


def confirm_force_import(parent, expected_version, actual_version, issues) -> bool:
    """The override on a refused packdump. Loud, because it is almost never right.

    §6.1's rule: the destructive act stays available, but you have to name it. Here that
    means typing the version you are about to adopt — which is also the fact most likely
    to make you stop.
    """
    lines = []
    for name, issue in sorted(issues.items()):
        if issue.get("level") == "error":
            lines.append(f"&nbsp;&nbsp;• <b>{name}</b>: profile expects "
                         f"<code>{issue['expected']}</code>, dump is "
                         f"<code>{issue['actual']}</code>")
    detail = "<br>".join(lines)
    typed, ok = _confirm_by_typing(
        parent, "Force Import — Almost Certainly Wrong",
        f"<b style='color:{style.ERROR}'>This packdump is not a snapshot of this "
        f"pack.</b><br><br>{detail}<br><br>"
        f"Importing it will reinterpret <b>every tag, view and job you own</b> against a "
        f"registry they were not written for. Entries you tagged may not exist; ids that "
        f"survive may mean something different. There is almost no scenario in which "
        f"forcing this is the correct move — porting a pack to a new Minecraft version "
        f"means <b>making a new profile</b>, not overwriting this one's contract.<br><br>"
        f"Nothing here is undone by importing an older dump afterwards.",
        expected=actual_version or "unknown",
        prompt=f"If you truly mean it, type the incoming version ({actual_version}):",
        ok_text="Force import anyway")
    return ok
