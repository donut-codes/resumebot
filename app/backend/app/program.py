"""Which program this process is.

The app runs as two programs on one database file: the page (the owner's port: the built page, every read and every
owner click, the live stream) and the engine (the schedule, the work queue, the fetchers and the doors the lanes use).
`all` is the whole app in one process: the in-code test seam (`tests/conftest.py` builds from `jobsearch.main.app`) and
`resumebot serve`. It is not a mode the owner runs the instance in.

A program says which it is when it starts (`start`, in its lifespan), never when its app object is built: the tests
import the page, the engine and the whole app into one process, and building an app must not change what the process
is."""

ROLES = ("all", "page", "engine")

#: `all` until a program starts.
PROGRAM = "all"


class WrongProgram(RuntimeError):
    """Code that belongs to one program was reached in the other."""


def start(role: str) -> None:
    """Record which program this process is. A program's lifespan calls it first."""
    global PROGRAM
    if role not in ROLES:
        raise ValueError(f"unknown program {role!r}; one of {', '.join(ROLES)}")
    PROGRAM = role


def require(role: str) -> None:
    """Refuse to go on unless this process is the `role` program, or `all` (the test seam runs everything)."""
    if PROGRAM not in (role, "all"):
        raise WrongProgram(f"this belongs to the {role} program; this process is the {PROGRAM} program")
