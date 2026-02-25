import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler
from packsmith.common.setup import PATHS
from datetime import datetime

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
        name = "deepend",
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

    log_dir = log_dir or PATHS.logs
    log_dir.mkdir(parents=True,exist_ok=True)
    log_file_path = log_dir / f"{name}.log"

    fmt = logging.Formatter(LOG_FORMAT,LOG_DATE_FORMAT)


    # Setup persistent handler
    persistent_handler_name = f"{name}:persistent"
    if persistent and not any(h.get_name() == persistent_handler_name for h in logger.handlers):
        persistent_handler = RotatingFileHandler(
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
        latest_log_path = log_dir / "latest.log"
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