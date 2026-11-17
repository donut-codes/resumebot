"""The field transforms a `request` recipe may name: a closed list, each a pure
function of one value, so a recipe stays data our code runs and never code a model wrote. A transform cannot reach the
network, a file or the clock; a pattern is the same safe kind `skills.compile_pattern` allows and runs under the same
time budget.

`first` takes the first of a list. `join` joins a list into one line. `html_text` turns markup into plain text. `date`
reads a date written in a stated format (or any of the usual ones) as `YYYY-MM-DD`. `pay` makes a pay value (a text, a
number, or an object with a low and a high) into the text the pay reader takes. `regex` takes one group of the first
match. `template` puts the value into a short text (`/jobs/{value}`), for an address built from an id."""

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: A date format is made of these: the percent codes of `strptime` that read a date, and plain separators.
_FORMAT = re.compile(r"(?:%[YymdbBHMSzfj]|[ \-/:.,T])+")
TEMPLATE_MAX = 200
LOW_KEYS = ("min", "minimum", "from", "low", "lower")
HIGH_KEYS = ("max", "maximum", "to", "high", "upper")
TEXT_MAX = 100_000


class Transform(BaseModel):
    """One step: `op`, and the one setting that op takes."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["first", "join", "html_text", "date", "pay", "regex", "template"]
    #: join: what goes between the parts.
    sep: str | None = Field(default=None, max_length=10)
    #: date: how the date is written (`%Y-%m-%d`, `%b %d, %Y`); blank, the usual ways.
    format: str | None = Field(default=None, max_length=40)
    #: regex: the pattern and which group of the match is kept (0 is the whole match).
    pattern: str | None = None
    group: int = Field(default=1, ge=0, le=5)
    #: template: the text, holding {value} once.
    text: str | None = Field(default=None, max_length=TEMPLATE_MAX)

    @field_validator("format")
    @classmethod
    def _format_plain(cls, v: str | None) -> str | None:
        if v is not None and not _FORMAT.fullmatch(v):
            raise ValueError("a date format is made of %Y %m %d %b %B %H %M %S and plain separators")
        return v

    @model_validator(mode="after")
    def _one_setting(self):
        given = {k for k in ("sep", "format", "pattern", "text") if getattr(self, k) is not None}
        allowed = {"join": {"sep"}, "date": {"format"}, "regex": {"pattern"}, "template": {"text"}}.get(self.op, set())
        if given - allowed:
            raise ValueError(f"{self.op} takes {', '.join(sorted(allowed)) or 'no setting'}, not "
                             f"{', '.join(sorted(given - allowed))}")
        if self.op == "regex":
            if self.pattern is None:
                raise ValueError("regex needs a pattern")
            from app.sites import skills  # the one place patterns are checked

            skills.compile_pattern(self.pattern, "transform pattern")
        if self.op == "template":
            rest = (self.text or "").replace("{value}", "", 1)
            if self.text is None or self.text.count("{value}") != 1 or "{" in rest or "}" in rest:
                raise ValueError("template needs a text holding {value} once and no other braces")
        return self


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (bool, dict)):
        return None
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, list):
        return None
    return " ".join(str(value).split()) or None


def _first_of(value: Any, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if isinstance(value, dict) and value.get(key) is not None:
            return value[key]
    return None


def apply_one(step: Transform, value: Any, budget: Any = None) -> Any:
    """One transform of `value`; None when it gives nothing (the field is then left empty, never guessed). `budget` is
    the run's pattern budget (`skills._Budget`); a regex without one is not run."""
    from app.jobsearch.adapters import _loose_date, html_to_text

    if value is None:
        return None
    if step.op == "first":
        return next((v for v in value if v is not None), None) if isinstance(value, list) else value
    if step.op == "join":
        if not isinstance(value, list):
            return value
        named = ("name", "label", "text", "title", "value")
        parts = [p for p in ((_text(_first_of(v, named)) if isinstance(v, dict) else _text(v)) for v in value) if p]
        return (step.sep if step.sep is not None else ", ").join(parts) or None
    if step.op == "html_text":
        text = _text(value) if not isinstance(value, str) else value
        return html_to_text(text[:TEXT_MAX]) or None if text else None
    if step.op == "date":
        text = _text(value)
        if text is None:
            return None
        if step.format:
            try:
                return datetime.strptime(text, step.format).date().isoformat()
            except ValueError:
                return None
        return _loose_date(text)
    if step.op == "pay":
        low, high = _first_of(value, LOW_KEYS), _first_of(value, HIGH_KEYS)
        if isinstance(value, dict) and (low is not None or high is not None):
            nums = [n for n in (_text(low), _text(high)) if n]
            return " - ".join(f"${int(float(n)):,}" if re.fullmatch(r"[\d.]+", n) else n for n in nums) or None
        return _text(value)
    if step.op == "regex":
        text = _text(value)
        if text is None or budget is None or step.pattern is None:
            return None
        from app.sites import skills

        found = budget.match(skills.compile_pattern(step.pattern, "transform pattern"), text, step.group)
        return found
    if step.op == "template":
        text = _text(value)
        return step.text.replace("{value}", text) if text and step.text else None
    return None


def apply(steps: list[Transform] | tuple[Transform, ...], value: Any, budget: Any = None) -> Any:
    """The transforms in order. A step that gives nothing ends the chain with nothing."""
    for step in steps:
        value = apply_one(step, value, budget)
        if value is None:
            return None
    return value
