"""Owner-facing wording kept as data: `templates/wording.<group>.yaml`, a mapping from a name to a sentence or a
block of copy, with `{{placeholder}}`s in it. Code fills them (`say`) through the same `render` the agents'
instruction files use, so a placeholder with no value fails loudly (`MissingPlaceholder`) instead of reaching the
owner as a typo.

A missing file, a malformed one or an unknown name raises `WordingError` naming the file. Files are read once and
kept until their mtime or size changes, so an edit takes effect without a rebuild."""

from pathlib import Path

import yaml

from app.config import settings
from app.errors import DomainError
from app.harness import instructions

PREFIX = "wording."
SUFFIX = ".yaml"

_cache: dict[Path, tuple[tuple[float, int], dict[str, str]]] = {}


class WordingError(DomainError):
    """A wording file is missing or malformed, or a name is not in it."""

    status = 500


def path_of(group: str) -> Path:
    return Path(settings.templates_path) / f"{PREFIX}{group}{SUFFIX}"


def groups() -> list[str]:
    """Every wording group there is, sorted."""
    folder = Path(settings.templates_path)
    return sorted(p.name[len(PREFIX):-len(SUFFIX)] for p in folder.glob(f"{PREFIX}*{SUFFIX}") if p.is_file())


def group(name: str) -> dict[str, str]:
    """The group's file as `{name: text}`, placeholders left as written."""
    path = path_of(name)
    try:
        stat = path.stat()
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WordingError(f"{path}: cannot be read ({exc.strerror or exc})") from exc
    stamp = (stat.st_mtime, stat.st_size)
    kept = _cache.get(path)
    if kept is not None and kept[0] == stamp:
        return dict(kept[1])
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WordingError(f"{path}: not valid YAML ({exc})") from exc
    if not isinstance(data, dict) or not data or not all(isinstance(v, str) for v in data.values()):
        raise WordingError(f"{path}: must be a mapping from a name to text")
    words = {str(k): v for k, v in data.items()}
    _cache[path] = (stamp, words)
    return dict(words)


def raw(name: str, key: str) -> str:
    """One entry of group `name`, placeholders left as written."""
    words = group(name)
    if key not in words:
        raise WordingError(f"{path_of(name)}: no entry named {key!r}; there are: {', '.join(words)}")
    return words[key]


def say(name: str, key: str, **values: object) -> str:
    """Entry `key` of group `name` with every `{{placeholder}}` filled from `values`."""
    return instructions.render(raw(name, key), values)
