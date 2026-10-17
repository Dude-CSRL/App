"""JSON Lines output helpers shared by the watcher, provisioning and snapshot tools."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import tempfile


def make_capture_logger(path: str, max_bytes: int, backup_count: int) -> logging.Logger:
    """A logger that writes bare messages to a RotatingFileHandler (one JSON object per line)."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    logger = logging.getLogger(f"ha_watcher.capture.{os.path.abspath(path)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def emit(logger: logging.Logger, record: dict) -> None:
    logger.info(json.dumps(record, separators=(",", ":")))
    for h in logger.handlers:
        h.flush()


def write_jsonl_atomic(path: str, records) -> int:
    """Write records as JSONL via temp file + fsync + os.replace. Returns the line count."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=d)
    n = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
                n += 1
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return n
