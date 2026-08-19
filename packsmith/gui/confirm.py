"""Confirmations for the operations that Ctrl+Z will not take back (design 9.3.3).

§9.3.3 draws the line as **undo reverses values and never reverses schema**. Deleting a tag
definition, a blueprint, an instance, or a bound slot changes what cells *exist* rather than
what one holds, so none of it is undoable — by design, not by omission.

That is only a fair deal if the user is told while deciding, which is what this module is:

> A confirmation for an operation outside undo must say that it cannot be undone, and name
> the snapshot that can recover it.

Both halves matter, and the second is the one that is easy to drop. "This cannot be undone"
on its own reads as *unrecoverable* and makes people hesitate over things that are actually
safe; naming where the copy lives turns it into what it really is — deliberate to reverse,
rather than gone. §9.3.1 takes that copy before every operation reaching this dialog.

One function rather than a sentence pasted into five call sites, because the wording is a
promise about behaviour: if snapshots move, or the coalescing window changes, the text has
to follow, and five copies would not.
"""
from PySide6.QtWidgets import QMessageBox

from packsmith.core import backup

# The guarantee is a snapshot *no older than the coalescing window*, not necessarily one
# taken for this click. A snapshot from four minutes ago predates this operation just as
# well, and claiming a fresh copy was made would be a lie every time coalescing skipped one.
_RECOVERY = ("Not undoable — Ctrl+Z reverses cell edits, not schema.\n"
             "A snapshot of this profile from just before now is kept in:\n  {where}")


def recovery_note(db_path) -> str:
    return _RECOVERY.format(where=backup.backups_dir(db_path))


def confirm_destructive(parent, title, body, db_path, *, ok="Delete") -> bool:
    """Ask before something undo cannot reverse. True if the user said yes.

    `body` says what is about to happen and how much of it; this adds only the part that is
    the same every time — that undo is not the way back, and what is.
    """
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Warning)
    box.setText(body)
    # Secondary text rather than more of `body`: the specifics are the decision, and the
    # recovery note is standing context that should not compete with them for attention.
    box.setInformativeText(recovery_note(db_path))
    yes = box.addButton(ok, QMessageBox.DestructiveRole)
    no = box.addButton("Cancel", QMessageBox.RejectRole)
    # By reference, not `buttons()[-1]`: that list is ordered by ROLE and platform
    # convention, not by the order they were added, so indexing into it made the
    # destructive button the default — Enter would have deleted.
    box.setDefaultButton(no)
    box.setEscapeButton(no)
    box.exec()
    return box.clickedButton() is yes
