"""The app's log set-up, shared by the programs: the page and the engine each call it as they start.

One line format for the app's own lines and uvicorn's: ISO time with its UTC offset, program, pid, level, logger and
message."""

import datetime as dt
import logging

from app import program

#: The one line format. `program` and the offset time are put on each record by `LogFormatter`.
FORMAT = "%(asctime)s %(program)s %(process)d %(levelname)s %(name)s: %(message)s"


class LogFormatter(logging.Formatter):
    """`FORMAT` with the time as ISO 8601 with its UTC offset and the program that wrote the line."""

    def __init__(self, fmt: str | None = None, datefmt: str | None = None, *args: object, **kwargs: object) -> None:
        super().__init__(fmt or FORMAT, datefmt)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return dt.datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")

    def format(self, record: logging.LogRecord) -> str:
        record.program = getattr(program, "PROGRAM", None) or "-"
        return super().format(record)


class DropQuery(logging.Filter):
    """uvicorn's access line without the query string: the browser bridge's key travels in one."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (args[0], args[1], args[2].split("?", 1)[0], *args[3:])
        return True


def uvicorn_log_config() -> dict:
    """uvicorn's own log config with its error and access lines in the app's format and no query string."""
    handler = {"formatter": "app", "class": "logging.StreamHandler", "stream": "ext://sys.stderr"}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"app": {"()": "app.log_setup.LogFormatter"}},
        "filters": {"no_query": {"()": "app.log_setup.DropQuery"}},
        "handlers": {
            "default": dict(handler),
            "access": {**handler, "filters": ["no_query"]},
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
    }


def _is_foreign_console_handler(handler: logging.Handler) -> bool:
    """A console handler some library put on the root logger with a format of its own: a plain `StreamHandler` with no
    `LogFormatter`, or a rich one. (The MCP library does it with `logging.basicConfig(format="%(message)s")` when its
    server object is built, so the start-up line had no time, program or pid.) The test runner's capture handlers and
    file handlers are other classes and stay."""
    if isinstance(handler.formatter, LogFormatter):
        return False
    return type(handler) is logging.StreamHandler or type(handler).__module__.startswith("rich.")


def configure_logging() -> None:
    """The app's own log lines (a refused call at INFO, a failed pass or cancel with its traceback) go to stderr, which
    the app's log keeps; uvicorn sets up only its own loggers. A console handler a library already put on the root
    logger is replaced by ours, so every line has the one format; any other handler is left alone."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if _is_foreign_console_handler(h)]:
        root.removeHandler(handler)
    if not any(isinstance(h.formatter, LogFormatter) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(LogFormatter())
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    access = logging.getLogger("uvicorn.access")  # also when uvicorn was started without the config above
    if not any(isinstance(f, DropQuery) for f in access.filters):
        access.addFilter(DropQuery())
    for noisy in ("httpx", "httpcore"):  # one INFO line per request, a second apart, is not worth keeping
        logging.getLogger(noisy).setLevel(logging.WARNING)
