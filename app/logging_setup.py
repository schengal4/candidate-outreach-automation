"""App-wide logging: everything goes to the console AND a rotating file.

Modules get their logger with logging.getLogger("app.<module>"); this module
only wires up where those records go. configure_logging() is called once at
import time by app.main (before uvicorn starts serving) and is idempotent, so
re-imports — e.g. from tests or uvicorn --reload — don't stack duplicate
handlers and double every line.

The file lives at data/logs/app.log (gitignored with the rest of data/):
console output disappears when the terminal closes, and pipeline runs are
long, concurrent, and cost real money — a persistent record of what each run
did (and why a company was dropped) has to survive the terminal session.
Uvicorn's own loggers keep their own handlers and are not touched here.
"""

import logging
import logging.handlers

from . import config

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    log_level = config.settings.LOG_LEVEL
    # Fall back to INFO on a typo'd LOG_LEVEL instead of crashing startup.
    level = getattr(logging, log_level, None)
    bad_level = not isinstance(level, int)

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    log_dir = config.settings.LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO if bad_level else level)
    root.addHandler(console)
    root.addHandler(file_handler)

    # httpx logs one INFO line per API request — it duplicates the app.llm
    # usage lines and drowns the file. Real problems still come through.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if bad_level:
        logging.getLogger("app.logging_setup").warning(
            "Unknown LOG_LEVEL %r — using INFO", log_level
        )
