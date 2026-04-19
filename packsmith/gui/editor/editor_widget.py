import json
from pathlib import Path

from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
from PySide6.QtCore import QUrl, QObject, Slot, Signal
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebChannel import QWebChannel


# Language detection from file extension
_EXT_TO_LANGUAGE = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".json": "json",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".xml": "xml",
    ".html": "html",
    ".css": "css",
    ".md": "markdown",
    ".txt": "plaintext",
    ".cfg": "ini",
    ".ini": "ini",
    ".properties": "ini",
    ".mcmeta": "json",
    ".lang": "ini",
}

_MONACO_HTML = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    html, body { margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden; background: #1e1e1e; }
    #editor { width: 100%; height: 100%; }
    #loading { color: #666; font-family: monospace; padding: 20px; }
</style>
</head>
<body>
<div id="loading">Loading Monaco...</div>
<div id="editor"></div>

<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.52.2/min/vs/loader.min.js"></script>
<script>
    require.config({ paths: { vs: 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.52.2/min/vs' } });

    let editor = null;
    let dirty = false;

    function initBridge(callback) {
        if (window.qt && window.qt.webChannelTransport) {
            new QWebChannel(qt.webChannelTransport, function(channel) {
                window.bridge = channel.objects.bridge;
                callback();
            });
        } else {
            callback();
        }
    }

    initBridge(function() {
        require(['vs/editor/editor.main'], function () {
            document.getElementById('loading').style.display = 'none';

            editor = monaco.editor.create(document.getElementById('editor'), {
                value: '',
                language: 'python',
                theme: 'vs-dark',
                automaticLayout: true,
                minimap: { enabled: true },
                fontSize: 14,
                lineNumbers: 'on',
                scrollBeyondLastLine: false,
                roundedSelection: false,
                renderWhitespace: 'none',
                cursorBlinking: 'smooth',
                smoothScrolling: true,
            });

            editor.onDidChangeModelContent(function() {
                if (!dirty) {
                    dirty = true;
                    if (window.bridge) {
                        window.bridge.markDirty();
                    }
                }
            });

            // Intercept Ctrl+S inside Monaco so the browser doesn't eat it
            editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, function() {
                if (window.bridge) {
                    dirty = false;
                    window.bridge.saveRequested(editor.getValue());
                }
            });

            if (window.bridge) {
                window.bridge.editorReady();
            }
        });
    });

    function setContent(text, language) {
        if (!editor) return;
        var model = editor.getModel();
        monaco.editor.setModelLanguage(model, language || 'python');
        editor.setValue(text);
        dirty = false;
    }

    function getContent() {
        return editor ? editor.getValue() : '';
    }
</script>
</body>
</html>
"""


class _Bridge(QObject):
    """Python <-> JavaScript bridge for Monaco communication."""

    ready = Signal()
    save = Signal(str)
    dirty = Signal()

    @Slot()
    def editorReady(self):
        self.ready.emit()

    @Slot(str)
    def saveRequested(self, content):
        self.save.emit(content)

    @Slot()
    def markDirty(self):
        self.dirty.emit()


class MonacoEditor(QWidget):
    """Widget wrapping a Monaco editor instance via QWebEngineView."""

    file_saved = Signal(str)  # emits file path on save

    def __init__(self, parent=None):
        super().__init__(parent)
        self._content = ""
        self._file_path = None
        self._pending_load = None
        self._ready = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Bridge
        self._bridge = _Bridge()
        self._bridge.ready.connect(self._on_ready)
        self._bridge.save.connect(self._on_save)
        self._bridge.dirty.connect(self._on_dirty)

        # Web channel
        self._channel = QWebChannel()
        self._channel.registerObject("bridge", self._bridge)

        # Web view
        self._web_view = QWebEngineView()
        self._web_view.page().setWebChannel(self._channel)
        layout.addWidget(self._web_view)

        # Status label
        self._status = QLabel("Saved")
        self._status.setFixedHeight(20)
        self._status.setStyleSheet("QLabel { color: #555555; font-size: 11px; padding: 2px 8px; background: #1e1e1e; }")
        layout.addWidget(self._status)

        # Load HTML with a real base URL so CDN fetches work
        self._web_view.setHtml(_MONACO_HTML, QUrl("https://cdnjs.cloudflare.com/"))

    def _on_ready(self):
        self._ready = True
        if self._pending_load:
            text, language = self._pending_load
            self._pending_load = None
            self._set_content(text, language)

    def _on_dirty(self):
        self._status.setText("Unsaved")
        self._status.setStyleSheet("QLabel { color: #cc4444; font-size: 11px; padding: 2px 8px; background: #1e1e1e; }")

    def _on_save(self, content):
        if not self._file_path:
            return
        self._content = content
        Path(self._file_path).write_text(content, encoding="utf-8")
        self._status.setText("Saved")
        self._status.setStyleSheet("QLabel { color: #555555; font-size: 11px; padding: 2px 8px; background: #1e1e1e; }")
        self.file_saved.emit(self._file_path)

    def _set_content(self, text: str, language: str):
        escaped = json.dumps(text)
        lang = json.dumps(language)
        self._web_view.page().runJavaScript(f"setContent({escaped}, {lang})")

    def load_file(self, file_path: str):
        """Load a file into the editor with auto-detected language."""
        path = Path(file_path)
        text = path.read_text(encoding="utf-8")
        language = _EXT_TO_LANGUAGE.get(path.suffix.lower(), "plaintext")
        self._content = text
        self._file_path = file_path

        if self._ready:
            self._set_content(text, language)
        else:
            self._pending_load = (text, language)

    def get_content(self) -> str:
        return self._content
