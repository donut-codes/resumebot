"""The structured record: one shape for a posting and for
the owner's master, so a comparison is a lookup from field to field.

A record is a set of filled fields. A field the text does not state is absent, never an empty value. Each filled field
carries who read it (`source`: `code`, `structured`, `listing`, `haiku`) and the line it rests on (`quote`); a field
with no source or no quote is refused, and a quote that is not in the text is no fact (`check_quotes`, the rule
`posting_facts.quote_in` already applies). A `listing` field (the title) rests on the listing, not on the text. A record
made from a short snippet is `partial`, and a partial record never fails a gate on an empty field.

Pure data: no database, no model call. The stored table is `models.StructuredRecord`.
"""

from dataclasses import dataclass, field

CODE, STRUCTURED, LISTING, HAIKU, SONNET = "code", "structured", "listing", "haiku", "sonnet"


def _norm(text: str | None) -> str:
    """A text as quotes are compared: lowercase, quotes and dashes made plain, every run of space one space."""
    folded = (text or "").translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                                                   "–": "-", "—": "-", " ": " "}))
    return " ".join(folded.lower().split())


def quote_in(text: str | None, quote: str | None) -> bool:
    """Whether `quote` is in the posting's `text`, word for word (case, spacing, curly quotes and dashes aside). A quote
    that is not is no evidence: a fact a model read stands only on a line it can point to."""
    wanted = _norm(quote).strip(" .\"'")
    return len(wanted) >= 4 and wanted in _norm(text)


SOURCES = (CODE, STRUCTURED, LISTING, HAIKU, SONNET)
#: What a record can hold, one name each (both sides share them). Every one is the owner's data once filled; none is a
#: value in code.
FIELDS = ("title", "level", "years", "areas", "stack", "education", "degree", "certifications", "clearance",
          "requirements", "duties", "preferred", "location", "work_mode", "pay", "pay_period", "travel",
          "employment_type", "claims")


class RecordError(ValueError):
    """A record that breaks the rules: no source, no quote, an unknown field or source, or nothing in the value."""


@dataclass(frozen=True)
class Field:
    """One filled field: its value, who read it, the line it rests on, and the other readings code noted but did not
    choose (several years figures in one line): they go to a model."""

    value: object
    source: str
    quote: str
    also: tuple = ()

    def as_dict(self) -> dict:
        out = {"value": self.value, "source": self.source, "quote": self.quote}
        if self.also:
            out["also"] = list(self.also)
        return out


@dataclass
class Record:
    fields: dict[str, Field] = field(default_factory=dict)
    partial: bool = False

    def as_dict(self) -> dict:
        return {"partial": self.partial, "fields": {k: v.as_dict() for k, v in self.fields.items()}}

    @classmethod
    def from_dict(cls, data: dict | None) -> "Record":
        data = data or {}
        out: dict[str, Field] = {}
        for name, raw in (data.get("fields") or {}).items():
            if name not in FIELDS:
                raise RecordError(f"{name!r} is not a field of the record")
            raw = raw or {}
            source, quote, value = raw.get("source"), str(raw.get("quote") or "").strip(), raw.get("value")
            if source not in SOURCES:
                raise RecordError(f"{name}: no source mark" if not source else f"{name}: {source!r} is not a source")
            if not quote:
                raise RecordError(f"{name}: no quote")
            if value is None or value == "" or value == [] or value == {}:
                raise RecordError(f"{name}: an empty field is left out, not filled")
            out[name] = Field(value, source, quote, tuple(raw.get("also") or ()))
        return cls(out, bool(data.get("partial")))


def check_quotes(record: Record, text: str | None) -> list[str]:
    """The names of the filled fields whose quote is not in the text (a `listing` field rests on the listing, so it is
    not held to the text). A list field whose items carry a quote of their own (the posting's numbered items) is held to
    each of them. Empty when every quote stands."""
    return [name for name, f in record.fields.items()
            if f.source != LISTING and not all(quote_in(text, q) for q in (f.quote, *_item_quotes(f)))]


def _item_quotes(f: Field) -> list[str]:
    """The quotes the items of a list field carry (none for a field whose items cite master lines instead)."""
    if not isinstance(f.value, list):
        return []
    return [str(i["quote"]) for i in f.value if isinstance(i, dict) and "quote" in i]
