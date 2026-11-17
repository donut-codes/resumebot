"""Instruction and template files: what each step of the loop tells an agent, loaded at runtime.

- `instructions/<feature>.<step>.instructions.md`: what to do in one step; `{{placeholder}}`s are
  filled for the task (`render`).
- `templates/<feature>.<step>.template.md`: the shape of the response.

Each file opens with an HTML header comment (where it is used, what loads it, its placeholders); the
comment is dropped when a file is served. Files are read from disk on every call, so an edit takes
effect without a rebuild. A name is the file's name without its `.instructions.md` / `.template.md`
suffix (`projects.plan.write`); the path the process file stores (`instructions/projects.plan.write.
instructions.md`) is accepted too. Only files in those two folders can be named.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import settings
from app.errors import DomainError
from app.harness.loader import split_front_matter

INSTRUCTIONS_SUFFIX = ".instructions.md"
TEMPLATE_SUFFIX = ".template.md"
#: A soft limit: a file longer than this is flagged for review (a warning, never a failure).
REVIEW_WORDS = 1400

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_HEADER = re.compile(r"\A\s*<!--.*?-->\s*", re.S)


class UnknownName(DomainError):
    """No instruction, template or kind has that name. The message lists the names there are."""

    status = 404


class MissingPlaceholder(DomainError):
    """An instruction names a `{{placeholder}}` that no value was given for."""

    status = 409


def _dir(kind: str) -> tuple[Path, str]:
    if kind == "instructions":
        return Path(settings.instructions_path), INSTRUCTIONS_SUFFIX
    return Path(settings.templates_path), TEMPLATE_SUFFIX


def names(kind: str) -> list[str]:
    """Every instruction (`kind="instructions"`) or template (`"templates"`) name there is, sorted."""
    folder, suffix = _dir(kind)
    if not folder.is_dir():
        return []
    return sorted(p.name.removesuffix(suffix) for p in folder.glob(f"*{suffix}") if p.is_file())


def short_name(kind: str, ref: str) -> str:
    """`ref` (a name, a file name, or `instructions/<file>` as the process file stores it) as a name."""
    _folder, suffix = _dir(kind)
    ref = str(ref or "").strip().replace("\\", "/")
    ref = ref.rsplit("/", 1)[-1]
    return ref.removesuffix(suffix)


def strip_header(text: str) -> str:
    """The file's text without its opening header comment."""
    return _HEADER.sub("", text, count=1).strip()


def read(kind: str, ref: str) -> str:
    """The file `ref` names, header comment dropped, placeholders left as written. Raises `UnknownName`."""
    name = short_name(kind, ref)
    folder, suffix = _dir(kind)
    if name not in names(kind):  # also keeps a path or `..` from reaching any other file
        label = "instruction" if kind == "instructions" else "template"
        have = ", ".join(names(kind)) or "none"
        raise UnknownName(f"no {label} named {name!r}; there are: {have}")
    return strip_header((folder / f"{name}{suffix}").read_text(encoding="utf-8"))


def placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text))


def render(text: str, values: dict[str, object]) -> str:
    """`text` with every `{{placeholder}}` filled from `values`. Raises `MissingPlaceholder`."""
    missing = sorted(placeholders(text) - set(values))
    if missing:
        raise MissingPlaceholder(f"no value for {', '.join('{{' + m + '}}' for m in missing)}")
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), text)


def read_kind(name: str, content=None) -> dict:
    """A kind's file for `kind.read`: its spec and the prose after the front matter. Raises `UnknownName`.

    `content` is the harness to read from; left out, it is the shipped one."""
    from app.harness.kinds import kind_spec
    from app.harness.loader import harness

    content = content if content is not None else harness()
    kinds = content.kinds
    kind = kinds.get(str(name or "").strip())
    if kind is None:
        raise UnknownName(f"no kind named {name!r}; there are: {', '.join(sorted(kinds)) or 'none'}")
    spec = kind_spec(kind)
    path = content.kind_files.get(kind.kind)
    if path is not None:
        _meta, notes = split_front_matter(path.read_text(encoding="utf-8"), str(path))
        if notes:
            spec["notes"] = notes
    spec["enabled"] = kind.enabled
    if kind.needs:
        spec["needs"] = kind.needs
    return spec


def word_count(text: str) -> int:
    return len(text.split())
