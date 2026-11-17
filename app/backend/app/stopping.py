"""Hearing a stop request when it arrives, not when the server gets round to the app.

The server (uvicorn) listens for the stop request itself (Ctrl+C, SIGTERM, and Ctrl+Break on Windows, which is what
`app/scripts/local/stack.py` sends), stops taking new calls, and then waits for every open response to finish before
it runs the app's own shutdown. A response that never finishes by itself (the page's live updates,
`jobsearch/pipeline/live.py`) is never told by that shutdown, because the shutdown waits for it: with a page left open
the app never finished a stop, and the launcher ended it after its whole wait, every time. So whatever must end for
the server to finish is told here, at the request."""

import asyncio
import contextlib
import signal
import threading
from collections.abc import Callable, Iterator
from typing import Any

#: The stop requests the server listens for (uvicorn's `HANDLED_SIGNALS`).
SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGBREAK") if hasattr(signal, name))


@contextlib.contextmanager
def heard(then: Callable[[], None]) -> Iterator[None]:
    """While this is open, a stop request runs `then` on the running event loop, and is then handed, unchanged, to
    whoever listened for it before (the server, which must have started listening already: it does before it starts
    the app). Where nothing listens for a request nothing is put in its way, so it still ends the process as it did;
    and off the main thread, where a process's requests cannot be listened for, this does nothing. The listeners that
    were there are put back at the end."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    loop = asyncio.get_running_loop()
    before: dict[int, Any] = {}

    def on_stop(sig: int, frame: Any) -> None:
        # A request arrives between any two steps of whatever the loop is running: `then` waits its turn on the loop.
        with contextlib.suppress(RuntimeError):  # the loop is closed: nothing is left to tell
            loop.call_soon_threadsafe(then)
        before[sig](sig, frame)

    for sig in SIGNALS:
        listener = signal.getsignal(sig)
        if callable(listener):
            before[sig] = listener
            signal.signal(sig, on_stop)
    try:
        yield
    finally:
        for sig, listener in before.items():
            if signal.getsignal(sig) is on_stop:
                signal.signal(sig, listener)
