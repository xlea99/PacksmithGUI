"""Creating actions, files, and the packages that hold them (design 3.3.1).

§3.3.1 specifies the package part exactly: *"The New Action dialog opens with a first field
for the target package: a dropdown of the user's existing authored packages plus a '+ New
package…' option. Creating a new package is one text field for the package name, inline in
the dialog."*

The reason there's no shortcut — no scratch buffer, no magic bucket — is that authored and
downloaded packages are structurally identical, which is what lets something you wrote be
published later without restructuring it. Total extra friction: one text field, once per
package.

What these dialogs add beyond that is the **entry point**. An action is an ``(id, file,
function)`` triple, and the first version of this dialog quietly hard-coded the last two to
``<id>.star`` and ``run``. That made the two layers look like one: the only way to get a
file was to declare an action, and a file could never hold more than a single action
without hand-editing the manifest. Both are now first-class fields, and a file can be
created on its own.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QComboBox, QLineEdit, QLabel, QDialogButtonBox,
    QMessageBox,
)

from packsmith.core.packages import MANIFEST_NAME, SOURCE_SUFFIX, folders, source_files
from packsmith.gui.shell import style

_NEW_PACKAGE = "➕  New package…"
_NEW_FILE = "➕  New file…"
_ROOT_FOLDER = "(package root)"


def _hint(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {style.TEXT_FAINT}; font-size: 11px;")
    return label


class _PackageChooser(QDialog):
    """Shared base: pick an authored package, or create one inline."""

    def __init__(self, package_index, title, package=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self._index = package_index

        self.result_package = None       # existing package name, or None if creating
        self.result_new_package = None   # name to create, or None

        self._root = QVBoxLayout(self)
        self._root.setSpacing(8)
        self._form = QFormLayout()
        self._form.setSpacing(6)

        # Only authored packages can gain anything — you may extend what you wrote, not
        # what you installed.
        self._authored = sorted(
            name for name, pkg in package_index.packages.items()
            if pkg.provenance == "authored")
        self._package = QComboBox()
        self._package.addItems(self._authored)
        self._package.addItem(_NEW_PACKAGE)
        if package in self._authored:
            self._package.setCurrentText(package)
        elif not self._authored:
            self._package.setCurrentText(_NEW_PACKAGE)
        # Connected in _finish, not here: subclasses override _on_package_changed to
        # repopulate fields they haven't constructed yet at this point.
        self._form.addRow("Package", self._package)

        self._new_package = QLineEdit()
        self._new_package.setPlaceholderText("my_pack")
        self._new_package_label = QLabel("New package name")
        self._form.addRow(self._new_package_label, self._new_package)

    def _finish(self, hint_text):
        self._root.addLayout(self._form)
        self._root.addWidget(_hint(hint_text))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._root.addWidget(buttons)
        self._package.currentTextChanged.connect(self._on_package_changed)
        self._on_package_changed()

    def _creating_package(self) -> bool:
        return self._package.currentText() == _NEW_PACKAGE

    def _current_package(self):
        if self._creating_package():
            return None
        return self._index.package(self._package.currentText())

    def _on_package_changed(self):
        creating = self._creating_package()
        self._new_package.setVisible(creating)
        self._new_package_label.setVisible(creating)
        self.adjustSize()

    def _resolve_package(self) -> bool:
        """Validate the package half. Returns False when it should keep the dialog open."""
        if self._creating_package():
            name = self._new_package.text().strip()
            if not name:
                QMessageBox.warning(self, "Name required", "The new package needs a name.")
                return False
            if name in self._index.packages:
                QMessageBox.warning(self, "Name taken",
                                    f"A package named '{name}' already exists.")
                self._new_package.setFocus()
                self._new_package.selectAll()
                return False
            self.result_new_package = name
            self.result_package = None
        else:
            self.result_package = self._package.currentText()
        return True


class NewActionDialog(_PackageChooser):
    """Pick (or create) a package, name the action, and say where its entry point lives.
    On accept the ``result_*`` attributes describe what to build; the caller does the
    writing."""

    def __init__(self, package_index, package=None, parent=None):
        super().__init__(package_index, "New Action", package, parent)

        self.result_action_id = ""
        self.result_file = None          # existing file to declare into, or None
        self.result_new_file = None      # file name to create, or None
        self.result_function = "run"
        self.result_name = ""
        self.result_description = ""

        self._action_id = QLineEdit()
        self._action_id.setPlaceholderText("hide_deprecated")
        self._action_id.textEdited.connect(self._sync_suggestions)
        self._form.addRow("Action id", self._action_id)

        self._file = QComboBox()
        self._file.currentTextChanged.connect(self._on_file_changed)
        self._form.addRow("File", self._file)

        self._new_file = QLineEdit()
        self._new_file.setPlaceholderText(f"hide_deprecated{SOURCE_SUFFIX}")
        self._new_file.textEdited.connect(self._touch_new_file)
        self._new_file_label = QLabel("New file name")
        self._form.addRow(self._new_file_label, self._new_file)
        self._new_file_touched = False

        self._function = QLineEdit("run")
        self._function.textEdited.connect(self._touch_function)
        self._function.setToolTip("The Starlark function this action calls. One file can "
                                  "hold several — give each action its own.")
        self._form.addRow("Function", self._function)
        self._function_touched = False

        self._name = QLineEdit()
        self._name.setPlaceholderText("Hide Deprecated Items")
        self._form.addRow("Display name", self._name)

        self._description = QLineEdit()
        self._form.addRow("Description", self._description)

        self._finish(
            "Ids, file names and functions are lowercase letters, digits and underscores; "
            "a new file name may include folders (helpers/math.star). Declaring into an "
            "existing file appends a stub for the function if it isn't defined yet; the "
            "rest of that file is left alone.")

    # ------------------------------------------------------------------ field wiring

    def _touch_function(self):
        self._function_touched = True

    def _touch_new_file(self):
        self._new_file_touched = True

    def _on_package_changed(self):
        super()._on_package_changed()
        package = self._current_package()
        names = [] if package is None else [
            n for n in source_files(package) if n != MANIFEST_NAME]
        self._file.blockSignals(True)
        self._file.clear()
        self._file.addItems(names)
        self._file.addItem(_NEW_FILE)
        # A brand-new package has nowhere to put it yet, so creating a file is the only
        # honest option.
        if not names:
            self._file.setCurrentText(_NEW_FILE)
        self._file.blockSignals(False)
        self._on_file_changed()

    def _creating_file(self) -> bool:
        return self._file.currentText() == _NEW_FILE

    def _on_file_changed(self):
        creating = self._creating_file()
        self._new_file.setVisible(creating)
        self._new_file_label.setVisible(creating)
        self._sync_suggestions()
        self.adjustSize()

    def _sync_suggestions(self):
        """Follow the action id until the user overrides either field by hand.

        A new file wants to be named after its action; a *second* action in an existing
        file must not be another ``run``, so the function follows the id there instead.
        """
        action_id = self._action_id.text().strip()
        if not self._new_file_touched:
            self._new_file.setText(f"{action_id}{SOURCE_SUFFIX}" if action_id else "")
        if self._function_touched:
            return
        if self._creating_file() or not self._file_has_actions():
            self._function.setText("run")
        else:
            self._function.setText(action_id or "run")

    def _file_has_actions(self) -> bool:
        package = self._current_package()
        if package is None:
            return False
        chosen = self._file.currentText()
        return any(a.file == chosen for a in package.actions)

    # ------------------------------------------------------------------ validation

    def accept(self):
        action_id = self._action_id.text().strip()
        if not action_id:
            QMessageBox.warning(self, "Id required", "An action needs an id.")
            self._action_id.setFocus()
            return
        function = self._function.text().strip()
        if not function:
            QMessageBox.warning(self, "Function required",
                                "An action needs a function to call.")
            self._function.setFocus()
            return

        if not self._resolve_package():
            return

        package = self._current_package()
        if package is not None:
            if any(a.action_id == action_id for a in package.actions):
                QMessageBox.warning(
                    self, "Id taken",
                    f"'{package.name}' already declares an action '{action_id}'.")
                self._action_id.setFocus()
                self._action_id.selectAll()
                return

        if self._creating_file():
            file_name = self._new_file.text().strip()
            if not file_name:
                QMessageBox.warning(self, "File required", "The new file needs a name.")
                self._new_file.setFocus()
                return
            if not file_name.endswith(SOURCE_SUFFIX):
                file_name += SOURCE_SUFFIX
            if package is not None and file_name in source_files(package):
                QMessageBox.warning(self, "File exists",
                                    f"'{package.name}' already has a {file_name}.")
                self._new_file.setFocus()
                self._new_file.selectAll()
                return
            self.result_new_file = file_name
            self.result_file = file_name
        else:
            file_name = self._file.currentText()
            self.result_file = file_name
            clash = next((a for a in (package.actions if package else [])
                          if a.file == file_name and a.function == function), None)
            if clash is not None:
                QMessageBox.warning(
                    self, "Entry point taken",
                    f"'{clash.action_id}' already points at {file_name}:{function}().\n\n"
                    f"Give this action its own function name.")
                self._function.setFocus()
                self._function.selectAll()
                return

        self.result_action_id = action_id
        self.result_function = function
        self.result_name = self._name.text().strip()
        self.result_description = self._description.text().strip()
        QDialog.accept(self)


class _DestinationChooser(_PackageChooser):
    """A package chooser with a folder dropdown under it. Folders are organisation only —
    an action's ``file`` is just a relative path — so this is a plain prefix, not a
    namespace."""

    def __init__(self, package_index, title, package=None, folder="", parent=None):
        super().__init__(package_index, title, package, parent)
        self._preferred_folder = folder
        self._folder = QComboBox()
        self._folder.setToolTip("Where in the package to put it. Purely organisational.")
        self._form.addRow("Folder", self._folder)

    def _on_package_changed(self):
        super()._on_package_changed()
        if not hasattr(self, "_folder"):
            return
        package = self._current_package()
        options = [_ROOT_FOLDER] + ([] if package is None else
                                    [f"{f}/" for f in folders(package)])
        self._folder.blockSignals(True)
        self._folder.clear()
        self._folder.addItems(options)
        wanted = f"{self._preferred_folder}/" if self._preferred_folder else _ROOT_FOLDER
        if wanted in options:
            self._folder.setCurrentText(wanted)
        self._folder.blockSignals(False)

    def _destination(self) -> str:
        """The chosen folder as a path prefix ("" at the package root)."""
        text = self._folder.currentText()
        return "" if text == _ROOT_FOLDER else text.rstrip("/")

    def _qualified(self, name: str) -> str:
        folder = self._destination()
        return f"{folder}/{name}" if folder else name


class NewFileDialog(_DestinationChooser):
    """Add a source file to a package, declaring nothing.

    This is the operation whose absence made the panel confusing: a helper or library file
    had to be smuggled in as an action and then hand-undeclared.
    """

    def __init__(self, package_index, package=None, folder="", parent=None):
        super().__init__(package_index, "New File", package, folder, parent)

        self.result_file = ""

        self._file = QLineEdit()
        self._file.setPlaceholderText(f"helpers{SOURCE_SUFFIX}")
        self._form.addRow("File name", self._file)

        self._finish(
            f"A plain {SOURCE_SUFFIX} file with no manifest entry. Nothing in it runs "
            f"until an action points at one of its functions — declare one whenever "
            f"you're ready, or leave it as shared code.")

    def accept(self):
        file_name = self._file.text().strip()
        if not file_name:
            QMessageBox.warning(self, "Name required", "The file needs a name.")
            self._file.setFocus()
            return
        if not file_name.endswith(SOURCE_SUFFIX):
            file_name += SOURCE_SUFFIX

        if not self._resolve_package():
            return

        path = self._qualified(file_name)
        package = self._current_package()
        if package is not None and path in source_files(package):
            QMessageBox.warning(self, "File exists",
                                f"'{package.name}' already has a {path}.")
            self._file.setFocus()
            self._file.selectAll()
            return

        self.result_file = path
        QDialog.accept(self)


class NewFolderDialog(_DestinationChooser):
    """Create an empty folder inside a package, for organisation only."""

    def __init__(self, package_index, package=None, folder="", parent=None):
        super().__init__(package_index, "New Folder", package, folder, parent)

        self.result_folder = ""

        self._name = QLineEdit()
        self._name.setPlaceholderText("helpers")
        self._form.addRow("Folder name", self._name)

        self._finish("Folders are organisation and nothing else — an action's file is "
                     "just a relative path, so moving sources between folders never "
                     "changes what a package means.")

    def accept(self):
        name = self._name.text().strip().strip("/")
        if not name:
            QMessageBox.warning(self, "Name required", "The folder needs a name.")
            self._name.setFocus()
            return

        if not self._resolve_package():
            return

        path = self._qualified(name)
        package = self._current_package()
        if package is not None and path in folders(package):
            QMessageBox.warning(self, "Folder exists",
                                f"'{package.name}' already has a {path}/.")
            self._name.setFocus()
            self._name.selectAll()
            return

        self.result_folder = path
        QDialog.accept(self)


class RenameFileDialog(QDialog):
    """Rename a file or folder. The new name may include folders, which makes this a move
    as well; declarations pointing at it are retargeted by the caller."""

    def __init__(self, package_name, path, *, is_folder=False, parent=None):
        super().__init__(parent)
        self._is_folder = is_folder
        noun = "Folder" if is_folder else "File"
        self.setWindowTitle(f"Rename {noun}")
        self.setMinimumWidth(440)
        self.result_name = ""

        root = QVBoxLayout(self)
        root.setSpacing(8)
        form = QFormLayout()
        self._name = QLineEdit(path)
        self._name.selectAll()
        form.addRow("New path", self._name)
        root.addLayout(form)
        root.addWidget(_hint(
            f"Renaming {package_name}/{path}{'/' if is_folder else ''}. Include a folder "
            f"to move it — every action declaring "
            f"{'anything inside' if is_folder else 'it'} is retargeted, and the manifest "
            f"is otherwise untouched."))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def accept(self):
        name = self._name.text().strip().strip("/")
        if not name:
            QMessageBox.warning(self, "Name required", "It needs a name.")
            return
        if not self._is_folder and not name.endswith(SOURCE_SUFFIX):
            name += SOURCE_SUFFIX
        self.result_name = name
        QDialog.accept(self)
