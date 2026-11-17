"""The published data model: what a process's conditions may read (requirement 15.3).

Each process declares its variables by dotted path with a type. `plan.items[].risk` is the `risk` of every
element of the list `plan.items`; that is how a condition's `every item in plan.items satisfies item.risk =
"low"` is checked field by field when the file loads. A condition that reads a path the model doesn't
declare fails to load. A field of type `any` allows anything beneath it (an opaque payload).
"""

import re
from dataclasses import dataclass

from process_engine.errors import ProcessLoadError

TYPES = ("string", "number", "boolean", "list", "object", "any")
_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[\])?(\.[A-Za-z_][A-Za-z0-9_]*(\[\])?)*$")

#: The most fields one process may declare (the biggest shipped one declares about a hundred). `resolve` looks through
#: every field, so a file with tens of thousands could keep the loader busy for minutes.
MAX_FIELDS = 1000


@dataclass(frozen=True)
class Field:
    path: str
    type: str = "any"
    enum: tuple[str, ...] = ()
    description: str = ""


#: Always in a process's model: `result` is the last step's result, `error` is what an error boundary caught.
IMPLICIT = (
    Field("result", "any", description="What the last step produced."),
    Field("error", "object", description="The error an error boundary event caught."),
    Field("error.code", "string", description="The error's name."),
    Field("error.message", "string", description="The error's plain-words message."),
)
_IMPLICIT_PATHS = {f.path for f in IMPLICIT}


def parent_path(path: str) -> str:
    """`a.b[].c` -> `a.b[]`; `a.b[]` -> `a.b`; `a` -> ''."""
    if path.endswith("[]"):
        return path[:-2]
    cut = path.rfind(".")
    return path[:cut] if cut >= 0 else ""


class DataModel:
    def __init__(self, fields: list[Field] | None = None):
        self.fields: dict[str, Field] = {}
        problems: list[str] = []
        declared_here: set[str] = set()
        if len(fields or []) > MAX_FIELDS:
            raise ProcessLoadError(
                f"data model: {len(fields)} fields are declared; a process can declare at most {MAX_FIELDS}")
        for field in [*IMPLICIT, *(fields or [])]:
            if not _PATH.match(field.path):
                problems.append(f"data model: '{field.path}' is not a field path (names, dots, and [] for list items)")
                continue
            if field.type not in TYPES:
                problems.append(f"data model: '{field.path}' has type '{field.type}'; use one of {', '.join(TYPES)}")
                continue
            if field.path in declared_here:
                problems.append(f"data model: '{field.path}' is declared twice")
                continue
            if field.path not in _IMPLICIT_PATHS or field not in IMPLICIT:
                declared_here.add(field.path)
            self.fields[field.path] = field
        for path in list(self.fields):
            parent = parent_path(path)
            while parent:
                declared = self.fields.get(parent)
                if declared and declared.type not in ("object", "list", "any"):
                    problems.append(f"data model: '{path}' sits under '{parent}', which is a {declared.type}")
                    break
                parent = parent_path(parent)
        if problems:
            raise ProcessLoadError(problems)

    @classmethod
    def from_dict(cls, spec: dict) -> "DataModel":
        """`{"plan.items": "list", "plan.items[].risk": {"type": "string", "enum": ["low", "high"]}}`."""
        fields = []
        for path, value in spec.items():
            if isinstance(value, str):
                fields.append(Field(path, value))
            else:
                fields.append(Field(path, value.get("type", "any"), tuple(value.get("enum", ())),
                                    value.get("description", "")))
        return cls(fields)

    def declared(self) -> list[Field]:
        """The fields an author declared (not the implicit ones)."""
        return [f for f in self.fields.values() if f not in IMPLICIT]

    def merged(self, other: "DataModel") -> "DataModel":
        return DataModel([*self.declared(), *other.declared()])

    def resolve(self, path: str) -> Field | None:
        """The field at `path`, or None when the model doesn't declare it. A path above declared fields is an
        object (or list); a path beneath an `any` field is `any`."""
        if path in self.fields:
            return self.fields[path]
        if any(p.startswith(path + "[]") for p in self.fields):
            return Field(path, "list")
        if any(p.startswith(path + ".") for p in self.fields):
            return Field(path, "object")
        parent = parent_path(path)
        while parent:
            declared = self.fields.get(parent)
            if declared and declared.type == "any":
                return Field(path, "any")
            parent = parent_path(parent)
        return None

    def listing(self) -> list[dict]:
        """For the How they work page: every declared field, in plain terms."""
        return [{"path": f.path, "type": f.type, "enum": list(f.enum), "description": f.description}
                for f in self.fields.values()]
