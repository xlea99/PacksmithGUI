"""Packsmith settings — the skeleton (design 3.1, Open Questions §Settings).

Two settings, both of which already existed in the profile and neither of which had a hand
on it: whether a new packdump is adopted automatically, and where Minecraft's client jar
lives. Everything here reads and writes `profile.settings`, which is why the dialog says
whose settings these are rather than implying they are global — a second profile pointing at
a different instance genuinely wants different answers to both.

**What is deliberately NOT here: datapack load order.** It is tempting, because a loader
provides it and it looks configurable. But load order is *pack content* — it decides which
override wins — no more an application setting than plugin order is a Mod Organizer setting.
It belongs to the pack, as first-class user data, and putting it here would be filing the
user's work under the app's preferences.
"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QLineEdit, QPushButton,
    QDialogButtonBox, QFileDialog, QWidget, QFrame, QComboBox,
)

from packsmith.core import launchers
from packsmith.core.capabilities import (
    DATAPACKS_ORDERING, DATAPACKS_WRITE, PACK_LOADER_SETTING,
    RESOURCEPACKS_ORDERING, RESOURCEPACKS_WRITE, resolve)
from packsmith.core.packdump import AUTO_ADOPT_SETTING
from packsmith.integrations import PACK_LOADERS
from packsmith.gui.load_order_dialog import LoadOrderDialog
from packsmith.gui.shell import style


class SettingsDialog(QDialog):
    """Edit the current profile's settings. `result_settings` is None unless accepted."""

    def __init__(self, profile, packdump=None, parent=None):
        super().__init__(parent)
        self._profile = profile
        # Loader detection is a packdump question (§8.1: scan the mod list), and a Profile
        # doesn't carry one — so it is handed in rather than reached for.
        self._packdump = packdump
        self.result_settings = None
        self.setWindowTitle("Packsmith Settings")
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        whose = QLabel(f"Settings for the profile <b>{profile.name}</b>")
        whose.setTextFormat(Qt.RichText)
        whose.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
        root.addWidget(whose)

        root.addWidget(self._packdump_section())
        root.addWidget(self._divider())
        root.addWidget(self._loader_section())
        root.addWidget(self._divider())
        root.addWidget(self._jar_section())
        root.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _divider():
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color: {style.BORDER};")
        return line

    @staticmethod
    def _explain(text):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
        return label

    # --- auto-adopt --------------------------------------------------------

    def _packdump_section(self) -> QWidget:
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self._auto_adopt = QCheckBox("Adopt a new packdump automatically")
        self._auto_adopt.setChecked(
            bool(self._profile.settings.get(AUTO_ADOPT_SETTING, True)))
        lay.addWidget(self._auto_adopt)
        # §3.1's reasoning, in the place where the choice is actually made: stale is the
        # worse failure mode, which is why this is on by default rather than merely
        # convenient.
        lay.addWidget(self._explain(
            "Checked, Packsmith takes on a changed packdump as soon as it notices one — "
            "which happens when you come back from the game. Unchecked, the dump waits in "
            "the Packdump tab until you bless it.\n"
            "On is the default because a stale registry is the worse failure: it produces "
            "confidently wrong output that looks fine, while an unwanted import announces "
            "itself the moment you look at anything."))
        return holder

    # --- the global pack loader (§8.1) -------------------------------------

    def _loader_section(self) -> QWidget:
        """Which installed loader receives overrides.

        No path chooser, deliberately: each loader dictates its own directory, so there is
        nothing here for the user to browse to. The only real choice is *which mod*, and
        that choice only exists among the ones actually installed.
        """
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        title = QLabel("<b>Global pack loader</b>")
        title.setTextFormat(Qt.RichText)
        lay.addWidget(title)
        lay.addWidget(self._explain(
            "Minecraft has no built-in way to load a datapack into every world, so this is "
            "the mod that does it. Overrides and generated packs are written where the "
            "selected loader reads them."))

        self._installed = [loader for loader in PACK_LOADERS
                           if loader.detect(self._packdump)]

        self._loader = QComboBox()
        for loader in self._installed:
            self._loader.addItem(loader.name, loader.name)
        self._loader.currentIndexChanged.connect(self._refresh_loader_status)
        lay.addWidget(self._loader)

        self._loader_status = QLabel()
        self._loader_status.setWordWrap(True)
        self._loader_status.setTextFormat(Qt.RichText)
        lay.addWidget(self._loader_status)

        self._order_button = QPushButton("Edit load order…")
        self._order_button.clicked.connect(self._edit_load_order)
        row = QHBoxLayout()
        row.addWidget(self._order_button)
        row.addStretch()
        lay.addLayout(row)

        if not self._installed:
            self._loader.addItem("(none installed)", None)
            self._loader.setEnabled(False)
        else:
            stored = self._profile.settings.get(PACK_LOADER_SETTING)
            index = self._loader.findData(stored)
            if index < 0:
                # Nothing chosen yet, or the chosen mod is gone. Show whoever `resolve`
                # ACTUALLY picks rather than whoever happens to be listed first — those
                # were two different orderings, and the dialog confidently displayed Paxi
                # on a pack where every override was going to Moonlight. A settings screen
                # that disagrees with the running app is worse than no settings screen.
                index = max(0, self._loader.findData(self._resolved_loader_name()))
            # The stored value is left alone when its mod is missing, so reinstalling it
            # restores the user's choice rather than finding it silently overwritten.
            self._loader.setCurrentIndex(index)
        self._refresh_loader_status()
        return holder

    def _resolved_loader_name(self) -> str:
        """Whichever loader the app is really writing overrides through right now."""
        table = resolve(self._packdump, loaders=PACK_LOADERS,
                        preferred=self._profile.settings.get(PACK_LOADER_SETTING),
                        instance_root=self._profile.mc_path)
        provider = table.provider_for(DATAPACKS_WRITE)
        return provider.name if provider is not None else ""

    def _selected_loader(self):
        name = self._loader.currentData()
        return next((l for l in self._installed if l.name == name), None)

    def _refresh_loader_status(self):
        loader = self._selected_loader()
        if loader is None:
            self._say_loader(
                "<b>⚠ No global pack loader installed.</b> Overrides and generated "
                "datapacks have nowhere to go. Install Paxi, Open Loader, Moonlight or "
                "Global Packs, then run the game once.", style.ERROR)
            self._order_button.setEnabled(False)
            return

        offered = set(loader.available_capabilities(self._profile.mc_path))
        lines = [
            self._capability_line("Datapacks", DATAPACKS_WRITE in offered),
            self._capability_line("Resource packs", RESOURCEPACKS_WRITE in offered),
            self._capability_line("Load ordering", DATAPACKS_ORDERING in offered
                                  or RESOURCEPACKS_ORDERING in offered),
        ]
        self._say_loader(f"{loader.summary}<br>" + " &nbsp; ".join(lines), style.TEXT_MUTED)
        self._order_button.setEnabled(
            bool({DATAPACKS_ORDERING, RESOURCEPACKS_ORDERING} & offered))

    @staticmethod
    def _capability_line(label, present) -> str:
        mark = "✓" if present else "✗"
        colour = style.SUCCESS if present else style.TEXT_FAINT
        return f"<span style='color:{colour}'>{mark} {label}</span>"

    def _say_loader(self, message, colour):
        self._loader_status.setText(message)
        self._loader_status.setStyleSheet(f"color: {colour}; font-size: 11px;")

    def _edit_load_order(self):
        loader = self._selected_loader()
        if loader is None:
            return
        offered = set(loader.available_capabilities(self._profile.mc_path))
        kinds = [(k, label) for k, label, cap in
                 (("datapacks", "Datapacks", DATAPACKS_ORDERING),
                  ("resourcepacks", "Resource Packs", RESOURCEPACKS_ORDERING))
                 if cap in offered]
        dialog = LoadOrderDialog(loader, self._profile.mc_path, kinds=kinds, parent=self)
        dialog.exec()

    # --- the client jar ----------------------------------------------------

    def _jar_section(self) -> QWidget:
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        title = QLabel("<b>Minecraft client jar</b>")
        title.setTextFormat(Qt.RichText)
        lay.addWidget(title)
        lay.addWidget(self._explain(
            "Vanilla's own loot tables, recipes and tags live in here, along with the "
            "authoritative pack_format. Packsmith finds it by itself for the launchers it "
            "knows; set it by hand for the ones it doesn't."))

        stored = self._profile.settings.get(launchers.JAR_SETTING) or ""
        row = QHBoxLayout()
        self._jar_path = QLineEdit(str(stored))
        self._jar_path.setPlaceholderText("detected automatically — set a path to override")
        self._jar_path.textChanged.connect(self._refresh_jar_status)
        row.addWidget(self._jar_path, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        lay.addLayout(row)

        self._jar_status = QLabel()
        self._jar_status.setWordWrap(True)
        self._jar_status.setTextFormat(Qt.RichText)
        lay.addWidget(self._jar_status)

        self._refresh_jar_status()
        return holder

    def _browse(self):
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Minecraft client jar", self._jar_path.text() or "",
            "Minecraft client jar (*.jar)")
        if chosen:
            self._jar_path.setText(chosen)

    def _refresh_jar_status(self):
        """Say what the jar situation actually is, right now.

        The red case is the one this dialog exists for: detection found nothing and nothing
        has been set, so every feature that needs vanilla data is quietly degraded. Silence
        there would leave the user with no way to know why.
        """
        typed = self._jar_path.text().strip()
        version = self._profile.mc_version or ""

        if typed:
            actual = launchers.verify(Path(typed), version or None)
            if actual:
                self._say(f"✓ Verified — this jar is Minecraft {actual}.", style.SUCCESS)
            elif not Path(typed).is_file():
                self._say("There is no file at that path.", style.ERROR)
            else:
                # A filename is a claim; `version.json` is the fact. A jar for a different
                # version would be worse than none — a missing answer is obvious, a wrong
                # one isn't.
                real = launchers.verify(Path(typed))
                self._say(
                    f"That file isn't a client jar for {version}."
                    + (f" It reports itself as {real}." if real else ""), style.ERROR)
            return

        found = launchers.locate_for(self._profile)
        if found is not None:
            self._say(f"✓ Found automatically via {found.launcher}:<br>{found.path}",
                      style.SUCCESS)
        else:
            self._say(
                f"<b>⚠ Not found automatically.</b> Packsmith couldn't locate the "
                f"{version or 'Minecraft'} client jar near this instance, so vanilla data "
                f"and the exact pack_format are unavailable until you set the path above.",
                style.ERROR)

    def _say(self, message, colour):
        self._jar_status.setText(message)
        self._jar_status.setStyleSheet(f"color: {colour}; font-size: 11px;")

    @property
    def jar_warning_visible(self) -> bool:
        """Whether the jar row is currently showing the red state."""
        return style.ERROR in self._jar_status.styleSheet()

    # --- committing --------------------------------------------------------

    def accept(self):
        """Refuse a jar path that doesn't verify, rather than storing it.

        `launchers.locate` treats an unusable override as "nothing found" and moves on, so
        a bad path saved here would silently behave exactly like no path at all — the user
        would believe they had fixed the thing the warning complained about.
        """
        settings = dict(self._profile.settings)
        settings[AUTO_ADOPT_SETTING] = self._auto_adopt.isChecked()
        if self._selected_loader() is not None:
            settings[PACK_LOADER_SETTING] = self._loader.currentData()

        typed = self._jar_path.text().strip()
        if typed:
            if not launchers.verify(Path(typed), self._profile.mc_version or None):
                self._refresh_jar_status()      # the label already says why
                return
            settings[launchers.JAR_SETTING] = typed
        else:
            settings.pop(launchers.JAR_SETTING, None)   # back to detection

        self.result_settings = settings
        super().accept()
