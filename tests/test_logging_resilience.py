"""Logging must never be the loudest thing in the console.

The reported failure: the app's own console filled with identical rotation tracebacks —
one per log record — because the log file sat a few dozen bytes under the rotation
threshold while another process held it. Every record retried the rotation, every retry
printed a full traceback, and the actual startup output was buried.
"""
import logging
import sys

import pytest

from packsmith.common.logging import TolerantRotatingFileHandler, get_logger


def _handler(tmp_path, **kwargs):
    handler = TolerantRotatingFileHandler(
        filename=tmp_path / "app.log", maxBytes=200, backupCount=2,
        encoding="utf-8", **kwargs)
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _record(message="x" * 50):
    return logging.LogRecord("t", logging.INFO, __file__, 1, message, (), None)


def test_a_locked_log_does_not_raise_through_to_the_app(tmp_path, monkeypatch):
    """Windows refuses to rename a file another process holds."""
    handler = _handler(tmp_path)
    monkeypatch.setattr(handler, "rotate",
                        lambda *a: (_ for _ in ()).throw(PermissionError(32, "in use")))

    for _ in range(10):
        handler.emit(_record())          # must not raise

    handler.close()
    assert (tmp_path / "app.log").read_text(encoding="utf-8"), \
        "records were dropped instead of appended to the unrotatable file"


def test_it_stops_retrying_instead_of_reporting_once_per_record(tmp_path, monkeypatch, capsys):
    """The spam is the bug. One locked file should cost one warning, not thousands."""
    handler = _handler(tmp_path)
    attempts = []

    def refuse(*_a):
        attempts.append(1)
        raise PermissionError(32, "in use")

    monkeypatch.setattr(handler, "rotate", refuse)
    for _ in range(200):
        handler.emit(_record())

    handler.close()
    assert len(attempts) == 1, f"retried rotation {len(attempts)} times while locked"
    assert capsys.readouterr().err.count("rotation deferred") == 1


def test_it_recovers_once_the_other_process_lets_go(tmp_path, monkeypatch):
    """Deferring must not be permanent — a transient AV scan shouldn't disable rotation
    for the life of the process."""
    handler = _handler(tmp_path)
    handler._RETRY_AFTER = 3
    monkeypatch.setattr(handler, "rotate",
                        lambda *a: (_ for _ in ()).throw(PermissionError(32, "in use")))
    handler.emit(_record())
    monkeypatch.undo()                                   # the lock goes away

    for _ in range(6):
        handler.emit(_record())
    handler.close()

    assert (tmp_path / "app.log.1").exists(), "never rotated again after the lock cleared"


def test_rotation_still_works_normally(tmp_path):
    handler = _handler(tmp_path)
    for _ in range(10):
        handler.emit(_record())
    handler.close()
    assert (tmp_path / "app.log.1").exists()


# --- tests and the app don't share a log file --------------------------------------------

def test_the_suite_logs_somewhere_other_than_the_app(tmp_path):
    """This run is under pytest, so it must not be writing to packsmith.log — a test run
    both holds that file (blocking the app's rotation) and fills it with noise."""
    assert "pytest" in sys.modules, "test env assumption"
    logging.getLogger("probe").handlers.clear()
    logger = get_logger(name="probe", log_dir=tmp_path, historical_debugs=0)
    files = {h.baseFilename for h in logger.handlers if hasattr(h, "baseFilename")}

    assert any("probe-test.log" in f for f in files), files
    assert not any(f.endswith("probe.log") for f in files), \
        "the suite is writing to the application's own log file"
