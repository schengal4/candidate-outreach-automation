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

Every record carries the pipeline run it belongs to ([run_id] in the format,
"-" outside a run): companies within a run log concurrently, and two runs can
overlap (multiple profiles, open mode), so without the stamp interleaved
lines are un-attributable. run_id_var is set at the top of the two pipeline
entry points (see app/pipeline.py); asyncio tasks, gather() and to_thread()
all inherit the contextvar, so every line below them — steps, llm, hunter,
gmail — is stamped without any call-site changes.
"""

import logging
import logging.handlers
from contextvars import ContextVar

from . import config

# The id of the pipeline run this task belongs to. Set by
# pipeline.run_discovery / run_pipeline, read by _RunIdFilter below.
run_id_var: ContextVar[str] = ContextVar("run_id", default="-")

_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(run_id)s]: %(message)s"

_configured = False


class _RunIdFilter(logging.Filter):
    """Stamps every record with the current run id. Attached to the HANDLERS,
    not to app loggers: records from uvicorn/anthropic/startup code would
    lack the attribute otherwise, and %(run_id)s on a record without it is a
    formatting error. A handler-level filter guarantees it on everything."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = run_id_var.get()
        return True


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
    run_id_filter = _RunIdFilter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(run_id_filter)

    log_dir = config.settings.LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(run_id_filter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file_handler)

    # LOG_LEVEL governs OUR code only (everything here logs under "app.").
    # It used to set the root level, where DEBUG unlocked third-party floods
    # instead of app detail: httpcore chats ~10 lines per HTTP request, and
    # the anthropic SDK logs the ENTIRE request payload — prompts, the
    # candidate's resume — on every call.
    logging.getLogger("app").setLevel(logging.INFO if bad_level else level)

    # Defense in depth for that payload dump: even if something else drops
    # the root level to DEBUG, the anthropic logger stays at INFO. The SDK's
    # own ANTHROPIC_LOG=debug escape hatch still works — it sets this
    # logger's level itself when genuinely needed.
    logging.getLogger("anthropic").setLevel(logging.INFO)

    # httpx logs one INFO line per API request — it duplicates the app.llm
    # usage lines and drowns the file. Real problems still come through.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Unhandled route exceptions ("Exception in ASGI application") are logged
    # by uvicorn.error, and uvicorn configures its loggers with
    # propagate=False — so those tracebacks were console-only and died with
    # the terminal, the exact loss the file exists to prevent. Attaching the
    # file handler to the parent "uvicorn" logger captures them (and startup/
    # shutdown lines); uvicorn's own console handler stays, so nothing
    # doubles on screen. uvicorn.access is deliberately NOT attached: the run
    # page polls /runs/{id}/panel every 3s — hundreds of lines per run.
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.addHandler(file_handler)
    # Under a real server uvicorn's dictConfig already set propagate=False
    # (before this lifespan code runs); pin it so contexts where that config
    # never ran (tests, scripts) don't double-write records to the file
    # through both the direct handler and root.
    uvicorn_logger.propagate = False

    if bad_level:
        logging.getLogger("app.logging_setup").warning(
            "Unknown LOG_LEVEL %r — using INFO", log_level
        )
