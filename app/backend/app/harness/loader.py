"""Load and validate the harness's content files: the vocab, the kinds of work, and the loop.

- `vocab/<name>.vocab.md`: YAML front matter `vocab`, `terms: [{id, term, means, avoid?, group}]`.
- `kinds/<domain>/<kind>.kind.md`: YAML front matter `kind`, `domain`, `title`, `data`, `do`,
  `done_means`, `check`, `enabled`, `needs`.
- `playbooks/project.pdca.process.yaml`: `process`, `steps: [{id, phase, role, ..., next}]`.

The content ships with each release and is read from disk, never baked into the code: `harness()`
re-reads a file only when its mtime or size changed (a few `stat` calls per call), so an edit takes
effect without a rebuild. A file that doesn't validate raises `HarnessError` naming every problem.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.config import settings
from app.errors import DomainError


class HarnessError(DomainError):
    """One or more content files don't validate; the message lists each problem and its file."""


# --- schemas ----------------------------------------------------------------------------------


class Term(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    term: str = Field(min_length=1)
    means: str = Field(min_length=1)
    avoid: list[str] = Field(default_factory=list)
    group: Literal["status", "outcome", "cause", "role", "phase", "object", "state"]


class Vocab(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vocab: str = Field(min_length=1)
    terms: list[Term] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> Vocab:
        seen: set[str] = set()
        for term in self.terms:
            if term.id in seen:
                raise ValueError(f"term id {term.id!r} appears twice")
            seen.add(term.id)
        return self

    def by_id(self) -> dict[str, Term]:
        return {t.id: t for t in self.terms}


class KindField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["int", "string", "bool", "list", "object"]
    required: bool = True
    means: str = ""


class KindDo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Who does it. How (a command, a tool call) is the agent's own business, never the app's. "porter": the
    #: app itself, in code, with no agent: a deterministic look at what the app already holds.
    runner: Literal["tool", "model", "household", "porter"]


class KindCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Who makes the check result: "agent" (a checker reads `test`) or "porter" (the app decides from
    #: the do-task's own evidence, no agent).
    by: Literal["agent", "porter"] = "agent"
    test: str = Field(min_length=1)
    causes: list[str] = Field(default_factory=list)


class Kind(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    title: str = Field(min_length=1)
    risk: Literal["read_only", "write", "destructive"]
    data: dict[str, KindField] = Field(default_factory=dict)
    do: KindDo
    done_means: str = Field(min_length=1)
    check: KindCheck
    enabled: bool = False
    needs: str | None = None
    #: What a done task of this kind found, as data: `{field: {type, required, means}}`, the same
    #: shape as `data`. `task.complete` carries a `result` checked against it (`result_problems`).
    result: dict[str, KindField] = Field(default_factory=dict)
    #: A read-only finding the next plan needs: a done task of this kind stays in the next plan's
    #: unresolved items, carrying its evidence and result. Left out, it is true for a `read_only`
    #: kind and false for any other (requirement 1.5); a kind file says `false` when its finding
    #: never needs acting on.
    feeds_next_plan: bool | None = None

    @model_validator(mode="after")
    def _feeds_by_default(self) -> Kind:
        if self.feeds_next_plan is None:
            self.feeds_next_plan = self.risk == "read_only"
        return self


class Step(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    phase: str = ""
    title: str = ""
    role: str = ""
    instructions: str | None = None
    template: str | None = None
    your_job: str = ""
    next: list[str] = Field(default_factory=list)

    @field_validator("next", mode="before")
    @classmethod
    def _next_may_be_one_id(cls, value: Any) -> Any:
        if value is None:
            return []
        return [value] if isinstance(value, str) else value


class Process(BaseModel):
    """The loop as a file. `process` is its name; the existing playbook format's `id` is accepted too."""

    model_config = ConfigDict(extra="ignore")

    process: str = Field(min_length=1)
    steps: list[Step] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _name_may_be_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and "process" not in data and "id" in data:
            return {**data, "process": data["id"]}
        return data

    @model_validator(mode="after")
    def _steps_hang_together(self) -> Process:
        ids = [s.id for s in self.steps]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"step ids repeat: {dupes}")
        for step in self.steps:
            missing = [n for n in step.next if n not in ids]
            if missing:
                raise ValueError(f"step {step.id!r} goes next to {missing}, which aren't steps")
        return self

    def step(self, step_id: str) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)


# --- what a load returns ----------------------------------------------------------------------


@dataclass(frozen=True)
class Harness:
    vocab: dict[str, Vocab] = field(default_factory=dict)
    kinds: dict[str, Kind] = field(default_factory=dict)
    process: Process | None = None
    #: Where each kind's file is, for `kind.read`'s notes.
    kind_files: dict[str, Path] = field(default_factory=dict)

    def vocab_ids(self, name: str) -> set[str]:
        v = self.vocab.get(name)
        return set(v.by_id()) if v is not None else set()

    def reference_problems(self) -> list[str]:
        """Cross-file mistakes: a kind naming a domain with no vocab, or a cause that isn't a term in
        that domain's vocab or porter.core. Not checked at load (a content edit must not take the
        doors down); the content test fails on any."""
        problems = []
        core = self.vocab_ids("porter.core")
        for kind in self.kinds.values():
            domain_ids = self.vocab_ids(kind.domain)
            if kind.domain not in self.vocab:
                problems.append(f"kind {kind.kind}: no vocab file for its domain {kind.domain!r}")
            for cause in kind.check.causes:
                if cause not in domain_ids and cause not in core:
                    problems.append(f"kind {kind.kind}: cause {cause!r} isn't a term in {kind.domain} or porter.core")
        return problems


# --- reading files ----------------------------------------------------------------------------


def split_front_matter(text: str, where: str) -> tuple[dict, str]:
    """`(front matter as a dict, the prose after it)` for a file that opens with a `---` block."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        raise HarnessError(f"{where}: the file must open with a --- front matter block")
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            try:
                meta = yaml.safe_load("\n".join(lines[1:end])) or {}
            except yaml.YAMLError as exc:
                raise HarnessError(f"{where}: the front matter isn't valid YAML: {exc}") from exc
            if not isinstance(meta, dict):
                raise HarnessError(f"{where}: the front matter must be a mapping")
            return meta, "\n".join(lines[end + 1:]).strip()
    raise HarnessError(f"{where}: the front matter block has no closing ---")


def _why(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'file'}: {e['msg']}" for e in exc.errors()
    )


def _stat_key(path: Path) -> tuple[str, int, int]:
    st = path.stat()
    return (str(path), st.st_mtime_ns, st.st_size)


def _files(vocab_dir: Path, kinds_dir: Path, process_file: Path) -> list[Path]:
    found: list[Path] = []
    if vocab_dir.is_dir():
        found += sorted(vocab_dir.glob("*.vocab.md"))
    if kinds_dir.is_dir():
        found += sorted(kinds_dir.glob("*/*.kind.md"))
    if process_file.is_file():
        found.append(process_file)
    return found


def parse_vocab_text(text: str, name: str, where: str) -> Vocab:
    """A vocab file's text, checked the way a file on disk is. `name` is the file name's stem."""
    meta, _ = split_front_matter(text, where)
    try:
        vocab = Vocab.model_validate(meta)
    except ValidationError as exc:
        raise HarnessError(f"{where}: {_why(exc)}") from exc
    if vocab.vocab != name:
        raise HarnessError(f"{where}: `vocab: {vocab.vocab}` must equal the file name ({name})")
    return vocab


def parse_kind_text(text: str, name: str, domain: str, where: str) -> Kind:
    """A kind file's text, checked the way a file on disk is. `name` is the file name's stem and
    `domain` its folder."""
    meta, _ = split_front_matter(text, where)
    try:
        kind = Kind.model_validate(meta)
    except ValidationError as exc:
        raise HarnessError(f"{where}: {_why(exc)}") from exc
    if kind.kind != name:
        raise HarnessError(f"{where}: `kind: {kind.kind}` must equal the file name ({name})")
    if kind.domain != domain:
        raise HarnessError(f"{where}: `domain: {kind.domain}` must equal its folder ({domain})")
    return kind


def _load_vocab(path: Path) -> Vocab:
    return parse_vocab_text(path.read_text(encoding="utf-8"), path.name.removesuffix(".vocab.md"), str(path))


def _load_kind(path: Path) -> Kind:
    return parse_kind_text(
        path.read_text(encoding="utf-8"), path.name.removesuffix(".kind.md"), path.parent.name, str(path),
    )


def _load_process(path: Path) -> Process:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise HarnessError(f"{path}: not valid YAML: {exc}") from exc
    try:
        return Process.model_validate(raw)
    except ValidationError as exc:
        raise HarnessError(f"{path}: {_why(exc)}") from exc


def load_harness(vocab_dir: Path, kinds_dir: Path, process_file: Path) -> Harness:
    """Read and validate everything, no cache. Raises `HarnessError` listing every bad file."""
    problems: list[str] = []
    vocab: dict[str, Vocab] = {}
    kinds: dict[str, Kind] = {}
    process: Process | None = None
    kind_files: dict[str, Path] = {}
    for path in _files(vocab_dir, kinds_dir, process_file):
        try:
            if path.name.endswith(".vocab.md"):
                v = _load_vocab(path)
                if v.vocab in vocab:
                    raise HarnessError(f"{path}: vocab {v.vocab!r} is defined twice")
                vocab[v.vocab] = v
            elif path.name.endswith(".kind.md"):
                k = _load_kind(path)
                if k.kind in kinds:
                    raise HarnessError(f"{path}: kind {k.kind!r} is defined twice")
                kinds[k.kind] = k
                kind_files[k.kind] = path
            else:
                process = _load_process(path)
        except HarnessError as exc:
            problems.append(str(exc))
        except OSError as exc:  # a file replaced mid-read
            problems.append(f"{path}: {exc}")
    if problems:
        raise HarnessError("\n".join(problems))
    return Harness(vocab=vocab, kinds=kinds, process=process, kind_files=kind_files)


# --- the cached entry point -------------------------------------------------------------------

_lock = threading.Lock()
_cache: dict[tuple[str, str, str], tuple[tuple, Harness]] = {}


def harness() -> Harness:
    """The current content, from the configured paths (`settings.vocab_path`, `kinds_path` and `process_path`).
    Cached; reloaded when any file's mtime or size changes, or one is added or removed.
    Raises `HarnessError` if a file is invalid."""
    vocab_dir, kinds_dir, process_file = Path(settings.vocab_path), Path(settings.kinds_path), Path(
        settings.process_path
    )
    key = (str(vocab_dir), str(kinds_dir), str(process_file))
    try:
        signature = tuple(_stat_key(p) for p in _files(vocab_dir, kinds_dir, process_file))
    except OSError:  # a file vanished between the listing and the stat: just reload
        signature = ()
    with _lock:
        cached = _cache.get(key)
        if cached is not None and signature and cached[0] == signature:
            return cached[1]
        loaded = load_harness(vocab_dir, kinds_dir, process_file)
        _cache[key] = (signature, loaded)
        return loaded


def clear_cache() -> None:
    with _lock:
        _cache.clear()
