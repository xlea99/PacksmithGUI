"""Following a log as the game writes it (design 6.3).

A log is the one kind of document where "changed on disk" is the normal state rather than
an event, so it is read differently from everything else:

* **Only the new bytes.** `debug.log` in a 300-mod pack passes a hundred megabytes; reading
  the whole file every second to show the last line would be absurd. The follower keeps a
  byte offset and reads from there.
* **The tail, not the whole file, on open.** Past `TAIL_BYTES` the start of the file is
  skipped and says so on its first line. Monaco would hold a 200 MB buffer, slowly, and
  nobody scrolls to the top of a log to find out what is happening now.
* **Restarts are noticed.** Minecraft renames `latest.log` away and starts a new one on
  every launch. The new file is shorter than where the follower was — or, if the game has
  already written past that point, starts with different bytes — and either way the
  follower starts over rather than appending the new log onto the end of the old one.
* **Bytes that are not UTF-8** become U+FFFD instead of refusing the file. A log holds
  whatever every mod printed; one stray byte must not make it unreadable. The decoder is
  incremental, so a character split across two reads is joined rather than mangled.
"""
import codecs
from pathlib import Path

TAIL_BYTES = 8 * 1024 * 1024
HEAD_BYTES = 256
FOLLOWED_SUFFIXES = (".log",)


def is_followed(path) -> bool:
    """Whether a document is a log to follow rather than a file to edit."""
    return Path(str(path)).suffix.lower() in FOLLOWED_SUFFIXES


def _decoder():
    return codecs.getincrementaldecoder("utf-8")(errors="replace")


class LogFollower:
    """Reads a growing file a piece at a time."""

    def __init__(self, path, *, tail_bytes=TAIL_BYTES):
        self.path = Path(path)
        self._tail_bytes = tail_bytes
        self._offset = 0
        self._head = b""
        self._decode = _decoder()

    def open(self) -> str:
        """The text to show now: the whole file, or its tail with a note saying so."""
        self._decode = _decoder()
        try:
            with open(self.path, "rb") as f:
                size = f.seek(0, 2)
                start = max(0, size - self._tail_bytes)
                f.seek(start)
                data = f.read(size - start)
                f.seek(0)
                self._head = f.read(min(size, HEAD_BYTES))
        except OSError:
            self._offset, self._head = 0, b""
            return ""
        self._offset = start + len(data)
        note = ""
        if start:
            # Start on a line, not in the middle of one.
            cut = data.find(b"\n")
            data = data[cut + 1:] if cut >= 0 else data
            note = (f"[packsmith] … the first {start / (1024 * 1024):.1f} MB of this file are "
                    f"not shown — following its end …\n")
        return note + self._decode.decode(data)

    def poll(self):
        """``("append", text)``, ``("reset", text)`` or ``("none", "")``."""
        try:
            with open(self.path, "rb") as f:
                size = f.seek(0, 2)
                f.seek(0)
                head = f.read(min(size, HEAD_BYTES))
                restarted = size < self._offset or head[:len(self._head)] != self._head
                if restarted:
                    return "reset", self.open()
                if size == self._offset:
                    return "none", ""
                f.seek(self._offset)
                data = f.read(size - self._offset)
        except OSError:
            return "none", ""
        self._offset += len(data)
        if len(self._head) < HEAD_BYTES:
            self._head = head
        return "append", self._decode.decode(data)
