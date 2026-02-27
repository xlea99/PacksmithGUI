import json
from pathlib import Path
from packsmith.common.logging import log


# Helper to both raise AND log an exception.
def raise_log(exception: type[Exception], message: str):
    log.error(message)
    raise exception(message)

# lil json writing helper
def write_json(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)