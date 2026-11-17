"""One app server per database. The app serializes its writes with one lock inside its own process (the writer turn,
`app.db`) and `BEGIN IMMEDIATE`; a second copy of the app on the same database file has a turn of its own, so the two
would fight through the busy timeout, and both would run the schedule, the start-up repairs and the dispatcher, reading
every job page and calling every model twice. So the server takes an exclusive lock on a file kept beside the database
(`<database file>.lock`) before it opens the database or runs any start-up step, and keeps it until it ends.

It is an operating system lock (`LockFileEx` through `msvcrt` on Windows, `flock` elsewhere), so the system frees it
when the process dies, a crash included, and nothing stale is left to clear by hand. The lock is on a byte far past the
end of the file, so the file's own text stays readable by the copy that is refused: it holds the holder's pid and
address, which the refusal names.

Only the app server takes it. The backup, `stack.py` (status, smoke), the setup and the command line open the database
while the app runs and must keep working, so they never come here. Copies on different database files never meet."""

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy.engine import make_url

#: Where in the lock file the OS lock is taken: one byte, far past the text, so a reader of the text
#: is never in its way.
_LOCK_OFFSET = 0x7FFFFFF0


class AlreadyRunning(RuntimeError):
    """Another copy of the app holds this database. The text is the plain message the refused start prints."""


def lock_path(database_url: str) -> Path | None:
    """The lock file of the SQLite database a URL names, by the file's resolved path; None when the URL names no file
    (an in-memory database has no second copy to meet)."""
    name = make_url(database_url).database
    if not name or name == ":memory:" or name.startswith("file:"):
        return None
    database = Path(name).resolve()
    return database.with_name(database.name + ".lock")


def _try_lock(handle: int) -> bool:
    os.lseek(handle, _LOCK_OFFSET, os.SEEK_SET)
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # whole-file, per open file: unlike lockf
    except OSError:
        return False
    return True


def _unlock(handle: int) -> None:
    if sys.platform == "win32":
        import msvcrt
        os.lseek(handle, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN)


def _own_address() -> str:
    """The address this server was started to listen on, read from its command line (`--host`, `--port`, as uvicorn and
    `resumebot serve` take them); empty when the port is not named there."""
    host, port = "127.0.0.1", ""
    args = sys.argv[1:]
    for index, arg in enumerate(args):
        for flag in ("--host", "--port"):
            if arg == flag and index + 1 < len(args):
                value = args[index + 1]
            elif arg.startswith(flag + "="):
                value = arg.split("=", 1)[1]
            else:
                continue
            if flag == "--host":
                host = value
            else:
                port = value
    return f"http://{host}:{port}" if port.isdigit() else ""


def _holder(path: Path) -> tuple[str, str]:
    """The pid and address the holder of a lock file wrote into it ('' for what it did not write)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "", ""
    pid = lines[0].strip() if lines else ""
    return (pid if pid.isdigit() else ""), (lines[1].strip() if len(lines) > 1 else "")


def refusal(path: Path, database: str) -> str:
    pid, address = _holder(path)
    who = ", ".join(part for part in ((f"pid {pid}" if pid else ""), address) if part)
    return (f"The app is already running on this database ({database})" + (f": {who}" if who else "")
            + ". Stop that copy first, then start this one.")


@contextlib.contextmanager
def hold(database_url: str) -> Iterator[None]:
    """Hold the lock of this database for the life of the `with`. Raises `AlreadyRunning` at once, before anything is
    opened or written, when another copy holds it. A database that is no file is not locked."""
    path = lock_path(database_url)
    if path is None:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if not _try_lock(handle):
            raise AlreadyRunning(refusal(path, str(path.with_suffix(""))))
        text = f"{os.getpid()}\n{_own_address()}\n".encode()
        os.lseek(handle, 0, os.SEEK_SET)
        os.ftruncate(handle, 0)
        os.write(handle, text)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                _unlock(handle)
    finally:
        os.close(handle)
