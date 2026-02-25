from packsmith.common.logging import log


# Helper to both raise AND log an exception.
def raise_log(exception: type[Exception], message: str):
    log.error(message)
    raise exception(message)