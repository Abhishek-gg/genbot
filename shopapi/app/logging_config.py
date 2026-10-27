"""Structured JSON logging. One JSON object per line, to stdout and LOG_DIR/shopapi.log."""
import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)

SERVICE = "shopapi"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": SERVICE,
            "version": os.getenv("APP_VERSION", "dev"),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if trace_id := trace_id_var.get():
            entry["trace_id"] = trace_id
        # Extra structured fields: log.info("...", extra={"fields": {...}})
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exc_type"] = record.exc_info[0].__name__
            entry["stack"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def setup_logging() -> None:
    formatter = JsonFormatter()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_dir = os.getenv("LOG_DIR", "logs")
    os.makedirs(log_dir, exist_ok=True)
    handlers.append(RotatingFileHandler(os.path.join(log_dir, "shopapi.log"), maxBytes=20_000_000, backupCount=3))

    root = logging.getLogger()
    root.handlers.clear()
    for h in handlers:
        h.setFormatter(formatter)
        root.addHandler(h)
    root.setLevel(logging.INFO)
