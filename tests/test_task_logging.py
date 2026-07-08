"""Background-task exception logging: a crash inside a _spawn()ed coroutine
must produce an ERROR log record with the traceback instead of vanishing."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import asyncio
import logging

import app.main as main

records = []


class Capture(logging.Handler):
    def emit(self, record):
        records.append(record)


handler = Capture()
logging.getLogger("app.main").addHandler(handler)


async def scenario():
    async def boom():
        raise RuntimeError("kaboom in background")

    async def fine():
        return 42

    main._spawn(boom())
    main._spawn(fine())
    await asyncio.sleep(0.05)  # let both settle and callbacks fire


try:
    asyncio.run(scenario())
    errors = [r for r in records if r.levelno == logging.ERROR]
    assert len(errors) == 1, f"expected exactly 1 error record, got {len(errors)}"
    assert "Background task" in errors[0].getMessage()
    assert errors[0].exc_info and "kaboom" in str(errors[0].exc_info[1])
    print("PASS: background-task crash is logged with traceback; successes stay quiet")
    assert not main._background_tasks, "task set must be drained"
    print("PASS: task registry cleaned up for both outcomes")
finally:
    logging.getLogger("app.main").removeHandler(handler)
