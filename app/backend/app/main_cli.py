"""The `resumebot` command: `resumebot COMMAND [options]`, or `python -m app.main_cli COMMAND` from the backend folder.

One import line for each command, and one entry in COMMANDS: the module that runs it. A command is a function that
takes its arguments (a list of words) and returns the exit status; the first line of its docstring is what
`resumebot --help` says about it. A work package that adds a command adds those two lines and takes its name out of
PLANNED, and edits nothing else here.

Exit status: 0 done, 2 the command line was wrong, 3 the command is planned and not built yet.
"""

import inspect
import sys
from collections.abc import Callable

from app.migrate.cli import main as copy_to_sqlite
from app.project_cli import project
from app.serve import main as serve
from app.setup_cli import connect, doctor, init, open_page, shortcut, signin, smoke, start, status

COMMANDS: dict[str, Callable[[list[str]], int]] = {
    "serve": serve,
    "copy-to-sqlite": copy_to_sqlite,
    "init": init,
    "connect": connect,
    "shortcut": shortcut,
    "open": open_page,
    "signin": signin,
    "start": start,
    "smoke": smoke,
    "doctor": doctor,
    "status": status,
    "project": project,
}

#: What is planned and not built yet. `dev.py release` and `dev.py preview` call these once they exist; until then
#: each says so and changes nothing.
PLANNED = {
    "backup": "planned",
    "restore": "planned",
    "update": "planned",
}

EXIT_NOT_BUILT = 3


def usage() -> str:
    lines = ["usage: resumebot COMMAND [options]", "", "commands:"]
    for name, command in COMMANDS.items():
        summary = (inspect.getdoc(command) or "").splitlines()
        lines.append(f"  {name:<10} {summary[0] if summary else ''}")
    for name, package in PLANNED.items():
        lines.append(f"  {name:<10} (not built yet: {package})")
    lines += ["", "  --version  say the version"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):  # a path or a child's text the PC's code page lacks must not end a command
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass
    if not args:
        print(usage(), file=sys.stderr)
        return 2
    name, rest = args[0], args[1:]
    if name in ("-h", "--help", "help"):
        print(usage())
        return 0
    if name in ("-V", "--version"):
        from app.settings import APP_NAME, VERSION  # not at the top: the usage above needs no settings

        print(f"{APP_NAME} {VERSION}")
        return 0
    if name in COMMANDS:
        return COMMANDS[name](rest)
    if name in PLANNED:
        print(
            f"resumebot {name} is not built yet ({PLANNED[name]}); "
            "nothing was changed.",
            file=sys.stderr,
        )
        return EXIT_NOT_BUILT
    print(f"resumebot: unknown command '{name}'\n\n{usage()}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
