import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
from packsmith.common.setup import GLOBAL_PATHS
from datetime import datetime


class TolerantRotatingFileHandler(RotatingFileHandler):
    """A rotating handler that survives not being able to rotate.

    On Windows a file cannot be renamed while another process holds it, so rotation fails
    whenever a second thing is touching the log — another Packsmith window, a test run, an
    antivirus scanner mid-read. The stock handler's response is to print the whole
    traceback to stderr, and because it re-attempts on *every* record once the file is at
    the size limit, the app's console fills with identical tracebacks faster than it can
    show anything real. Losing the ability to log is annoying; losing the ability to read
    the console is worse, and it hides whatever you were actually debugging.

    So a failed rotation degrades instead: keep appending to the current file (it grows
    past the cap, which is a far smaller problem than silence) and stop retrying for a
    while, so one locked file costs one warning rather than thousands.
    """

    _RETRY_AFTER = 500          # records to wait before trying to rotate again

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rollover_cooldown = 0

    def shouldRollover(self, record):
        if self._rollover_cooldown > 0:
            self._rollover_cooldown -= 1
            return False
        return super().shouldRollover(record)

    def doRollover(self):
        try:
            super().doRollover()
        except OSError as e:
            self._rollover_cooldown = self._RETRY_AFTER
            # Reopen whatever we can still write to — the base class closes the stream
            # before it rotates, so leaving it None would drop records until it reopened.
            if self.stream is None and not self.delay:
                self.stream = self._open()
            print(f"[packsmith] log rotation deferred ({e.__class__.__name__}: "
                  f"the log file is held by another process)", file=sys.stderr)

# Set up the special "test" log level for specific testing.
TEST_LOG_LEVEL = 25
if logging.getLevelName(TEST_LOG_LEVEL) == str(f"Level {TEST_LOG_LEVEL}"):
    logging.addLevelName(TEST_LOG_LEVEL,"TEST")
    def test(self,message,*args,**kwargs):
        if self.isEnabledFor(TEST_LOG_LEVEL):
            self._log(TEST_LOG_LEVEL,message,args,**kwargs)
    logging.Logger.test = test

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s:%(filename)s:%(lineno)d] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

def get_logger(
        name = "packsmith",
        level = logging.INFO,
        log_dir: Path | None = None,
        max_bytes = 5 * 1024 * 1024,
        backup_count = 5,
        persistent = True,
        console = False,
        historical_debugs: int = 10
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    log_dir = log_dir or GLOBAL_PATHS.logs
    log_dir.mkdir(parents=True,exist_ok=True)
    # The test suite gets its own file. Sharing one was causing both halves of a nasty
    # interaction: a test run holds the log, so the app can't rotate it — and every test
    # run pours thousands of lines in, which is how the file reached the rotation
    # threshold in the first place. An app log full of test noise is also just a worse log.
    stem = f"{name}-test" if "pytest" in sys.modules else name
    log_file_path = log_dir / f"{stem}.log"

    fmt = logging.Formatter(LOG_FORMAT,LOG_DATE_FORMAT)


    # Setup persistent handler
    persistent_handler_name = f"{name}:persistent"
    if persistent and not any(h.get_name() == persistent_handler_name for h in logger.handlers):
        persistent_handler = TolerantRotatingFileHandler(
            filename=log_file_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=False,
        )
        persistent_handler.setLevel(level)
        persistent_handler.setFormatter(fmt)
        persistent_handler.set_name(persistent_handler_name)
        logger.addHandler(persistent_handler)

    # Setup latest-only handler
    latest_handler_name = f"{name}:latest"
    if not any(h.get_name() == latest_handler_name for h in logger.handlers):
        latest_log_path = log_dir / ("latest-test.log" if "pytest" in sys.modules else "latest.log")
        latest_handler = logging.FileHandler(
            filename=latest_log_path,
            mode="w",
            encoding="utf-8",
            delay=False
        )
        latest_handler.setLevel(level)
        latest_handler.setFormatter(fmt)
        latest_handler.set_name(latest_handler_name)
        logger.addHandler(latest_handler)

    # Setup historical debug handler
    historical_debug_handler_name = f"{name}:historical_debug"
    if historical_debugs > 0 and not any(h.get_name() == historical_debug_handler_name for h in logger.handlers):
        historical_debug_path = log_dir / "debug"
        historical_debug_path.mkdir(parents=True,exist_ok=True)
        this_historical_debug_log_path =  historical_debug_path / f"{name}_{datetime.now():%Y-%m-%d_%H-%M-%S}.log"

        historical_debug_handler = logging.FileHandler(
            filename=this_historical_debug_log_path,
            encoding="utf-8",
            delay=False
        )
        historical_debug_handler.setLevel(logging.DEBUG)
        historical_debug_handler.setFormatter(fmt)
        historical_debug_handler.set_name(historical_debug_handler_name)
        logger.addHandler(historical_debug_handler)

        # Prune oldest runs
        runs = sorted(historical_debug_path.glob(f"{name}_*.log"),key=lambda p: p.stat().st_mtime,reverse=True)
        for run in runs[historical_debugs:]:
            try: run.unlink()
            except OSError: pass

    # Setup console handler
    console_handler_name = f"{name}:console"
    if console and not any(h.get_name() == console_handler_name for h in logger.handlers):
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(fmt)
        console_handler.set_name(console_handler_name)
        logger.addHandler(console_handler)

    return logger

log = get_logger(level=logging.INFO,console=False,historical_debugs=10)
log.info("=== INITIALIZED NEW SESSION ===")