"""The Text Editor (design 6.3) — one Monaco instance serving every editor tab (§4.2).

**Why one instance.** Measured: a `QWebEngineView` per tab costs its own Chromium process
and ~119 MB — five open files came to ~700 MB, ten to ~1.3 GB. One shared view with a
Monaco *model* per document is flat at ~225 MB however many are open. Reparenting that
single view between tab placeholders measured ~4 ms with no page reload and no loss of
per-model undo history, so sharing costs nothing in responsiveness.

**Documents, not files.** What may be edited is decided by where a document comes from
(see :mod:`packsmith.gui.editor.sources`): instance files answer to §6.1 ownership,
package sources answer to provenance. The host doesn't know either rule — it asks the
document's source and renders the answer.

**Locked, not warned.** A document its owner won't share opens read-only. The instant the
user types, Monaco's ``onDidAttemptReadOnlyEdit`` fires and we ask whether they mean to
take it. That keeps cause and effect in the same second; a warning at edit time whose
consequence lands days later (a job suddenly failing) is exactly what §6.1's philosophy
section argues against.
"""
from pathlib import Path

from PySide6.QtCore import QUrl, QObject, QTimer, Slot, Signal, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebChannel import QWebChannel

from packsmith.gui.shell import style

_HTML_PATH = Path(__file__).with_name("monaco_host.html")

# Monaco has no Starlark grammar, but Starlark *is* a Python dialect — the highlighting is
# correct for everything an action can legally contain.
#
# It has no TOML grammar either, and unlike Starlark there's no exact stand-in. `ini` is
# the closest thing Monaco ships: `[sections]`, `key = value`, `#` comments — TOML's basic
# shape, which is what mod configs are. Naming a language Monaco doesn't have is not an
# error, it just silently renders as plaintext, which is how this went unnoticed.
# Manifests are `.json5`, which Monaco has no support for at all — the host page
# registers a `json5` language of its own. See monaco_host.html.
_EXT_TO_LANGUAGE = {
    ".star": "python", ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".json": "json", ".json5": "json5", ".mcmeta": "json", ".toml": "ini",
    ".yaml": "yaml", ".yml": "yaml", ".xml": "xml", ".html": "html", ".css": "css",
    ".md": "markdown", ".txt": "plaintext", ".cfg": "ini", ".ini": "ini",
    ".properties": "ini", ".lang": "ini", ".snbt": "plaintext", ".mcfunction": "plaintext",
    ".zs": "javascript",
}


def language_for(path) -> str:
    return _EXT_TO_LANGUAGE.get(Path(path).suffix.lower(), "plaintext")


class _Bridge(QObject):
    """Python <-> JavaScript channel."""
    ready = Signal()
    save = Signal(str, str)      # key, content
    dirty = Signal(str, bool)    # key, is_dirty
    attempted = Signal(str)      # key — typed into a locked buffer

    @Slot()
    def editorReady(self):
        self.ready.emit()

    @Slot(str, str)
    def saveRequested(self, key, content):
        self.save.emit(key, content)

    @Slot(str, bool)
    def dirtyChanged(self, key, is_dirty):
        self.dirty.emit(key, is_dirty)

    @Slot(str)
    def readOnlyEditAttempted(self, key):
        self.attempted.emit(key)


class EditorHost(QObject):
    """Owns the one web view and brokers models for every editor tab."""

    dirty_changed = Signal(str, bool)   # key, is_dirty
    file_saved = Signal(str)            # key
    save_failed = Signal(str, str)      # key, reason
    unlocked = Signal(str)              # key
    edit_blocked = Signal(str, str)     # key, lock reason

    def __init__(self, sources: dict, container: QWidget, parent=None):
        super().__init__(parent)
        # {name: DocumentSource}. A document key is "<source>:<path>", so one flat model
        # map serves origins governed by completely different rules.
        self._sources = dict(sources)
        # `container` MUST live inside the main window. The view is a native widget, and
        # moving a native widget between *top-level windows* forces Qt to recreate its
        # window handle — visible as the whole app re-creating itself (it jumps, the
        # taskbar entry re-pops). Staying inside one top-level keeps reparents cheap.
        self._container = container
        self._ready = False
        self._pending = []          # calls queued until Monaco finishes loading
        self._locked = set()
        self._opened = set()        # keys Monaco holds a model for, so rebind can drop them

        # Focus is handed to the view a turn AFTER a tab activates — see `focus_editor`.
        # Parented to self so it dies with the host rather than firing into a deleted C++
        # object during teardown, which is the shape of every Qt crash this project has had.
        self._focus_timer = QTimer(self)
        self._focus_timer.setSingleShot(True)
        self._focus_timer.setInterval(0)
        self._focus_timer.timeout.connect(self._take_focus)

        self._bridge = _Bridge()
        self._bridge.ready.connect(self._on_ready)
        self._bridge.save.connect(self._on_save)
        self._bridge.dirty.connect(self._on_dirty)
        self._bridge.attempted.connect(self._on_edit_attempted)

        self._channel = QWebChannel()
        self._channel.registerObject("bridge", self._bridge)

        # Attaching reparents the view INTO a tab, which makes the tab its owner — so a
        # closing tab would delete the shared view along with itself. The container owns
        # it instead, keeping the singleton alive for the window's lifetime.
        self.view = QWebEngineView(container)
        self.view.page().setWebChannel(self._channel)
        # A web page's background defaults to WHITE, which is what flashes in the instant
        # after a reparent before the compositor redraws. Match the editor instead.
        self.view.page().setBackgroundColor(QColor(style.BG_DEEP))
        self.view.setStyleSheet(f"background: {style.BG_DEEP};")
        self.view.hide()
        # Navigate to the file rather than setHtml-ing its text: the page needs a real
        # file:// document URL for `./vendor/monaco/vs` to resolve, and for the page to
        # derive the absolute path its web workers need. Monaco is vendored, so this is
        # the whole of the offline story — no network, no first-open round trip.
        self.view.load(QUrl.fromLocalFile(str(_HTML_PATH)))

    # --- plumbing ----------------------------------------------------------

    def _js(self, code):
        if self._ready:
            self.view.page().runJavaScript(code)
        else:
            self._pending.append(code)

    def _on_ready(self):
        self._ready = True
        for code in self._pending:
            self.view.page().runJavaScript(code)
        self._pending.clear()
        self._send_starlark_api()

    def _send_starlark_api(self):
        """Hand the page the `pack` surface for `.star` completions (design 6.3).

        Sent once, because the surface is the same in every profile — it is introspected
        from `Pack`, not from the user's data, so there is nothing to refresh on a profile
        switch or a packdump import. Anything profile-specific (registry ids, the user's
        tag names) would need a request/response channel this bridge does not have; see
        `gui/editor/starlark_api.py` for what is deliberately out of scope.
        """
        import json
        from packsmith.gui.editor.starlark_api import catalog
        self._js(f"setStarlarkApi({json.dumps(catalog())})")

    @staticmethod
    def _quote(text) -> str:
        import json
        return json.dumps(text)

    # --- documents ----------------------------------------------------------

    @staticmethod
    def key_for(source: str, path: str) -> str:
        return f"{source}:{path}"

    @staticmethod
    def split_key(key: str):
        source, _, path = key.partition(":")
        return source, path

    def source_named(self, name: str):
        """One registered DocumentSource by name, for callers that need to ask a source
        something before a document exists — classifying bytes, most obviously."""
        return self._sources[name]

    def _source_of(self, key: str):
        source, path = self.split_key(key)
        return self._sources[source], path

    def lock_reason(self, key: str):
        """Why this document is locked, or None — answered by its own origin's rules."""
        source, path = self._source_of(key)
        return source.read_only_reason(path)

    def can_unlock(self, key: str) -> bool:
        source, path = self._source_of(key)
        return source.can_unlock(path)

    def open_document(self, source_name: str, path: str, *, read_only: bool = False) -> str:
        key = self.key_for(source_name, path)
        source = self._sources[source_name]
        content = source.read(path) or ""
        read_only = bool(read_only or source.read_only_reason(path))
        self._opened.add(key)
        if read_only:
            self._locked.add(key)
        self._js(f"openModel({self._quote(key)}, {self._quote(content)}, "
                 f"{self._quote(language_for(path))}, {str(read_only).lower()})")
        return key

    def show_document(self, key: str):
        self._js(f"showModel({self._quote(key)})")

    def close_document(self, key: str):
        self._locked.discard(key)
        self._opened.discard(key)
        self._js(f"closeModel({self._quote(key)})")

    def rebind(self, sources: dict):
        """Point the host at a different profile's documents, keeping the web view alive.

        Switching profiles rebuilds everything else in the window, but not this: the view
        is a `QWebEngineView`, so recreating it means paying Chromium startup again and
        re-entering the widget-lifetime problems that made the shared-view design tricky
        in the first place (see :meth:`EditorTab.attach_to`). Every model is dropped
        because every model belonged to the old profile.
        """
        for key in list(self._opened):
            self.close_document(key)
        self._locked.clear()
        self._opened.clear()
        self._sources = dict(sources)

    def unlock(self, key: str):
        """Lift a lock the user is permitted to lift, unlocking the buffer in place."""
        source, path = self._source_of(key)
        if not source.can_unlock(path):
            return
        source.unlock(path)
        self._locked.discard(key)
        self._js(f"setReadOnly({self._quote(key)}, false)")
        self.unlocked.emit(key)

    def is_locked(self, key: str) -> bool:
        return key in self._locked

    def resync_locks(self):
        """Re-ask every open document whether it is still writable.

        Lock state is decided when a document opens, which is fine until something else
        writes the file — a job run, most obviously. Without this the tab keeps *looking*
        editable after an action takes the file; the save is refused at the source (§6.1),
        but being told "no" at Ctrl+S is a worse experience than seeing the lock appear.
        Returns the keys whose state changed, so the caller can say so.
        """
        changed = []
        for key in sorted(self._opened):
            try:
                source, path = self._source_of(key)
            except (KeyError, ValueError):
                continue
            locked_now = bool(source.read_only_reason(path))
            if locked_now == (key in self._locked):
                continue
            if locked_now:
                self._locked.add(key)
            else:
                self._locked.discard(key)
            self._js(f"setReadOnly({self._quote(key)}, {str(locked_now).lower()})")
            changed.append(key)
        return changed

    # --- diffs ---------------------------------------------------------------

    def open_diff(self, key: str, original: str, modified: str, path: str):
        """Show two versions of one file side by side, read-only.

        Uses the SAME web view — §4.2's "one Chromium process however many tabs" holds for
        diffs too. Inside the page they are a second Monaco editor on a second div, because
        a diff editor is a different object from a normal one and cannot share its node;
        only one of the two is ever visible.
        """
        self._js(f"openDiff({self._quote(key)}, {self._quote(original or '')}, "
                 f"{self._quote(modified or '')}, {self._quote(language_for(path))})")
        return key

    def show_diff(self, key: str):
        self._js(f"showDiff({self._quote(key)})")

    def close_diff(self, key: str):
        self._js(f"closeDiff({self._quote(key)})")

    def request_save(self, key: str):
        """Ask Monaco for the buffer, which comes back through the save signal."""
        self.view.page().runJavaScript(
            f"(function(){{ var v = valueOf({self._quote(key)}); "
            f"if (v !== null) bridgeCall('saveRequested', {self._quote(key)}, v); }})()")

    # --- tabs ---------------------------------------------------------------

    def attach_to(self, placeholder: QWidget):
        """Move the single view into the active tab. Cheap (~4ms) and does not reload.

        Updates are suppressed across the move so the slot can't paint its own background
        in the gap — that gap is what shows up as a grey flash on every tab switch.
        """
        if self.view.parentWidget() is placeholder:
            self.view.show()
            return
        placeholder.setUpdatesEnabled(False)
        try:
            placeholder.layout().addWidget(self.view)
            self.view.show()
        finally:
            placeholder.setUpdatesEnabled(True)

    def focus_editor(self):
        """Put the keyboard in the editor when its tab comes forward.

        `showModel` already calls Monaco's own `editor.focus()`, but that only decides
        where the caret goes *inside* the page. The page still has to be the widget Qt is
        sending key events to, and switching tabs gives focus to the tab — not to the
        native child that was just reparented into it. The result looked like a focused
        editor that ignored the keyboard until you clicked the text once.

        Deferred by a turn because the view has only just been reparented and shown;
        focusing mid-move is dropped when the widget is re-shown at its new home.
        """
        self._focus_timer.start()

    def _take_focus(self):
        if not self.view.isVisible():
            return          # parked, or the tab moved on before the timer fired
        self.view.setFocus(Qt.OtherFocusReason)
        # Qt focus decides which widget gets the keys; this decides where they land in the
        # page. Both are needed, and the JS half is re-asserted here because `showModel`
        # ran before the widget could accept focus at all.
        self._js("if (typeof editor !== 'undefined' && editor) editor.focus();")

    def _park(self):
        self.view.setParent(self._container)
        self.view.hide()

    def release_from(self, widget: QWidget):
        """Called when a tab closes: reclaim the view first, or the tab's destruction
        takes the shared editor with it."""
        if self.view.parentWidget() is widget:
            self._park()

    # --- signals from the buffer --------------------------------------------

    def _on_edit_attempted(self, key):
        reason = self.lock_reason(key)
        if reason:
            self.edit_blocked.emit(key, reason)

    def _on_dirty(self, key, is_dirty):
        if is_dirty:
            source, path = self._source_of(key)
            source.on_first_edit(path)
        self.dirty_changed.emit(key, is_dirty)

    def _on_save(self, key, content):
        if key in self._locked:
            # §6.3: Ctrl+S is a no-op when locked — but a *silent* no-op reads as a bug,
            # so say why, the same way a blocked keystroke does.
            source, path = self._source_of(key)
            self.edit_blocked.emit(key, source.read_only_reason(path) or "locked")
            return
        source, path = self._source_of(key)
        try:
            source.write(path, content)
        except Exception as e:                       # escaped root, permissions, disk full…
            self.save_failed.emit(key, f"{type(e).__name__}: {e}")
            return
        self._js(f"markSaved({self._quote(key)})")
        self.file_saved.emit(key)


class EditorTab(QWidget):
    """One open document: a placeholder the shared view moves into, plus this document's
    lock banner and status strip."""

    unlock_requested = Signal(str)   # key

    def __init__(self, host: EditorHost, source_name: str, path: str, *, parent=None):
        super().__init__(parent)
        self.path = path
        self.source_name = source_name
        self.key = host.key_for(source_name, path)
        self._host = host

        # Paint the tab in the editor's own background so any frame the web view isn't
        # covering shows dark rather than default widget grey.
        self.setAutoFillBackground(True)
        self.setStyleSheet(f"background: {style.BG_DEEP};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Ambient state, so the lock is visible *before* you try to type; the
        # attempted-edit prompt is the backstop for when you try anyway.
        self._banner = QWidget()
        banner_row = QHBoxLayout(self._banner)
        banner_row.setContentsMargins(8, 2, 8, 2)
        self._banner_text = QLabel()
        self._banner_text.setStyleSheet(f"color: {style.OWNER_ACTION}; font-size: 11px;")
        self._unlock_btn = QPushButton("Take ownership")
        self._unlock_btn.setFixedHeight(18)
        self._unlock_btn.setCursor(Qt.PointingHandCursor)
        self._unlock_btn.setStyleSheet(
            f"QPushButton {{ background: {style.BG_CHROME}; color: {style.TEXT};"
            f" border: 1px solid {style.OWNER_ACTION}; font-size: 10px; padding: 0 8px; }}")
        self._unlock_btn.clicked.connect(lambda: self.unlock_requested.emit(self.key))
        banner_row.addWidget(self._banner_text)
        banner_row.addStretch()
        banner_row.addWidget(self._unlock_btn)
        self._banner.setStyleSheet(
            f"background: {style.BG_PANEL}; border-bottom: 1px solid {style.BORDER};")
        layout.addWidget(self._banner)

        self.slot = QWidget()
        self.slot.setAutoFillBackground(True)
        self.slot.setStyleSheet(f"background: {style.BG_DEEP};")
        QVBoxLayout(self.slot).setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.slot, 1)

        self._status = QLabel(path)
        self._status.setFixedHeight(20)
        self._status.setStyleSheet(
            f"color: {style.TEXT_FAINT}; font-size: 11px; padding: 2px 8px;"
            f" background: {style.BG_PANEL};")
        layout.addWidget(self._status)

        host.open_document(source_name, path)
        self.sync_lock()

    # --- state ---------------------------------------------------------------

    def activate(self):
        self._host.attach_to(self.slot)
        self._host.show_document(self.key)
        self._host.focus_editor()

    def detach(self):
        """Give the shared view back before this tab is destroyed."""
        self._host.release_from(self.slot)

    def sync_lock(self):
        reason = self._host.lock_reason(self.key)
        if not reason:
            self._banner.hide()
            return
        self._banner_text.setText(f"Locked — {reason}")
        self._unlock_btn.setVisible(self._host.can_unlock(self.key))
        self._banner.show()

    def set_dirty(self, is_dirty: bool):
        mark = " •  unsaved" if is_dirty else ""
        self._status.setText(f"{self.path}{mark}")
        self._status.setStyleSheet(
            f"color: {'#cc8844' if is_dirty else style.TEXT_FAINT}; font-size: 11px;"
            f" padding: 2px 8px; background: {style.BG_PANEL};")

    @property
    def is_dirty(self) -> bool:
        return "unsaved" in self._status.text()


class DiffTab(QWidget):
    """Two versions of one file, side by side (design 3.3's run report).

    Shaped like :class:`EditorTab` on purpose — a placeholder the shared view moves into,
    plus a strip saying what is being compared — so the workspace needs no idea that this
    is a different kind of thing.

    Read-only throughout. This shows what a step *did*; changing it back is rollback, which
    is a different act with a different button.
    """

    def __init__(self, host: EditorHost, key: str, path: str, *, left: str, right: str,
                 original: str, modified: str, note: str = "", parent=None):
        super().__init__(parent)
        self.key = key
        self.path = path
        self._host = host

        self.setAutoFillBackground(True)
        self.setStyleSheet(f"background: {style.BG_DEEP};")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        strip = QWidget()
        row = QHBoxLayout(strip)
        row.setContentsMargins(8, 3, 8, 3)
        caption = QLabel(f"{left}   →   {right}")
        caption.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 11px;")
        row.addWidget(caption)
        row.addStretch()
        if note:
            # The one thing a stored diff can be wrong about: the file has moved on since
            # the run. The hash recorded at commit time is what lets this be said rather
            # than guessed.
            warning = QLabel(note)
            warning.setStyleSheet(f"color: {style.WARNING}; font-size: 11px;")
            row.addWidget(warning)
        strip.setStyleSheet(
            f"background: {style.BG_PANEL}; border-bottom: 1px solid {style.BORDER};")
        layout.addWidget(strip)

        self.slot = QWidget()
        self.slot.setAutoFillBackground(True)
        self.slot.setStyleSheet(f"background: {style.BG_DEEP};")
        QVBoxLayout(self.slot).setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.slot, 1)

        host.open_diff(key, original, modified, path)

    def activate(self):
        self._host.attach_to(self.slot)
        self._host.show_diff(self.key)

    def detach(self):
        self._host.release_from(self.slot)
        self._host.close_diff(self.key)


class UnsupportedFileTab(QWidget):
    """The "cannot display" placeholder design 6.0 asks for.

    Deliberately not an error dialog: opening a jar isn't a mistake, it's a reasonable
    thing to try in a folder full of jars. So this says what the file *is*, which editor
    would own it, and offers the one thing that always works — open it in whatever the OS
    uses. A dead end that names the road out isn't a dead end.
    """

    def __init__(self, rel_path, kind, parent=None):
        super().__init__(parent)
        from packsmith.core import filetypes

        self.rel_path = rel_path
        self.kind = kind

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(10)
        lay.addStretch()

        title = QLabel(Path(rel_path).name)
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(f"color: {style.TEXT}; font-size: 15px; font-weight: bold;")
        lay.addWidget(title)

        explain = QLabel(filetypes.describe(kind))
        explain.setAlignment(Qt.AlignCenter)
        explain.setWordWrap(True)
        explain.setStyleSheet(f"color: {style.TEXT_MUTED}; font-size: 12px;")
        lay.addWidget(explain)

        path_lbl = QLabel(str(rel_path))
        path_lbl.setAlignment(Qt.AlignCenter)
        path_lbl.setStyleSheet(f"color: {style.TEXT_FAINT}; font-size: 11px;")
        lay.addWidget(path_lbl)
        lay.addStretch()

    def activate(self):
        """Same shape as EditorTab, so the workspace doesn't need to know the difference."""
