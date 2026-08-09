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

from PySide6.QtCore import QUrl, QObject, Slot, Signal, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebChannel import QWebChannel

from packsmith.gui.shell import style

_HTML_PATH = Path(__file__).with_name("monaco_host.html")

# Monaco has no Starlark grammar, but Starlark *is* a Python dialect — the highlighting is
# correct for everything an action can legally contain.
_EXT_TO_LANGUAGE = {
    ".star": "python", ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".json": "json", ".json5": "json", ".mcmeta": "json", ".toml": "toml",
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
        # A real base URL so the CDN fetch resolves. Bundling Monaco locally (design 4.2)
        # is still outstanding — until then the first open needs a network round trip.
        self.view.setHtml(_HTML_PATH.read_text(encoding="utf-8"),
                          QUrl("https://cdnjs.cloudflare.com/"))

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
        if read_only:
            self._locked.add(key)
        self._js(f"openModel({self._quote(key)}, {self._quote(content)}, "
                 f"{self._quote(language_for(path))}, {str(read_only).lower()})")
        return key

    def show_document(self, key: str):
        self._js(f"showModel({self._quote(key)})")

    def close_document(self, key: str):
        self._locked.discard(key)
        self._js(f"closeModel({self._quote(key)})")

    def unlock(self, key: str):
        """Lift a lock the user is permitted to lift, unlocking the buffer in place."""
        source, path = self._source_of(key)
        if not source.can_unlock(path):
            return
        source.unlock(path)
        self._locked.discard(key)
        self._js(f"setReadOnly({self._quote(key)}, false)")
        self.unlocked.emit(key)

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
            return                                   # §6.3: Ctrl+S is a no-op when locked
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
