"""Write the three helper agents into a project folder (`resumebot project [folder]`).

The files come from `app/project-agents/`, beside the code in the clone the install is made from. They go to
`<folder>/.claude/agents/` and nowhere else. This command is the one place that refuses a folder: the clone of the app,
its data folder, or anything inside either. A file that is already there is never touched: the same words are "already
there", different words are kept as the user left them (to take a newer copy they delete their file and run it again).
No flag, no backup file, no other file written."""

import sys
from pathlib import Path

from app import setup_cli

#: The clone the install runs from (one above `app/`) and the folder the agent files are read from. Tests patch both.
CODE_ROOT = Path(__file__).resolve().parents[3]
AGENTS_SOURCE = Path(__file__).resolve().parents[2] / "project-agents"


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _inside(path: Path, root: Path) -> bool:
    resolved = root.resolve()
    return path == resolved or resolved in path.parents


def _refusal(folder: Path) -> str | None:
    """Why this folder is not one to write into, or None."""
    if not folder.exists():
        return f"{folder} does not exist: start Claude Code in an empty folder of your own and run this there."
    if not folder.is_dir():
        return f"{folder} is not a folder."
    here = folder.resolve()
    for root, what in ((CODE_ROOT, "the app's code folder"), (setup_cli.data_folder(), "the app's data folder")):
        if _inside(here, root):
            return (f"{folder} is {what} or inside it: the helper agents go in your own project folder. "
                    "Start Claude Code in an empty folder of your own.")
    return None


def _agents_folder(folder: Path) -> tuple[Path | None, str | None]:
    """The folder to write into, resolved. A `.claude` or `agents` that is a link leaving the project is refused."""
    here = folder.resolve()
    target = folder / ".claude" / "agents"
    resolved = target.resolve()  # follows any link, existing or not yet made
    if not _inside(resolved, here):
        return None, f"{target} leads outside {folder}: nothing was written."
    for root in (CODE_ROOT, setup_cli.data_folder()):
        if _inside(resolved, root):
            return None, f"{target} leads into the app's own folders: nothing was written."
    return target, None


def project(argv: list[str]) -> int:
    """write the three helper agents into a project folder"""
    if len(argv) > 1 or (argv and argv[0].startswith("-")):
        print("usage: resumebot project [folder]", file=sys.stderr)
        return 2
    folder = Path(argv[0]).expanduser() if argv else Path.cwd()
    sources = sorted(AGENTS_SOURCE.glob("*.md"))
    if not sources:
        print(f"resumebot project: no helper agents found in {AGENTS_SOURCE}.", file=sys.stderr)
        return 1
    why = _refusal(folder)
    if why:
        print(f"resumebot project: {why}", file=sys.stderr)
        return 1
    target, why = _agents_folder(folder)
    if why or target is None:
        print(f"resumebot project: {why}", file=sys.stderr)
        return 1
    target.mkdir(parents=True, exist_ok=True)
    for source in sources:
        wanted = _lf(source.read_bytes())
        mine = target / source.name
        if mine.exists():
            word = "already there" if _lf(mine.read_bytes()) == wanted else (
                f"kept your {source.name} (it differs from this release's)")
            print(f"{source.name}: {word}")
            continue
        mine.write_bytes(wanted)
        print(f"{source.name}: written to {target}")
    return 0
