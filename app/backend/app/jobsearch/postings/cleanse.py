"""The cleaner: a posting's plain fields as the reader made them, made
into one clean shape. Pure: no database, no network, no model.

What it does, and nothing else:
- embedded data is taken out of the text before anything else (`drop_embedded`): a `data:` address (a picture's bytes
  written into the text) and the image or link wrapper that only carried it. It is no posting text. It is named in
  `removed` as `embedded_data`, decided on what it is and never on its length;
- the text is tidied (odd spaces and blank runs) and the named boilerplate rules (`RULES`) take out lines that are no
  part of the job: the equal-opportunity statement, a run of benefits, how to apply. Every rule that took something out
  is named in `removed` with the lines and characters, and the raw copy still has all of it. A line with a pay figure
  in it is never taken out, and a text that would be left empty is kept whole. Nothing is cut for length;
- the fields the gates decide on are plain values (title, employer, places, way of working, pay, posted date);
- the structured record (`postings/record.py`, the code fillers of `structured_fill.py`) is filled from the clean text
  and kept as `marks`: each field with who read it and the line it rests on. A value the job system itself gave rests on
  the listing (`LISTING`); one read from the text is `CODE` with its quote.

`CLEANER_VERSION` is a code constant. A change to what this module does takes a new number and a line in the comment
above it, then `recleanse`: every copy is rebuilt from its raw rows with no fetch, and every role is settled and ranked
again. A change that leaves all but a handful of texts as they were may keep the number instead, when the comment above
it says so and why, and the few copies it does change are made again on their own (embedded data:
`raw_copies.heal_embedded`). Nothing here names a person, an employer or a place: every rule is about the shape of a
line.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from app.jobsearch.postings import stems, whole

#: What each version did: 1 = the first cleaner. Rules: equal_opportunity, benefits, how_to_apply (lines only, never one
#: with a dollar figure). Fields: title, employer, places, way of working, pay, posted date; marks from structured_fill.
#: Embedded data (`drop_embedded`) came in under the same number on purpose: a new number makes `recleanse`
#: build a copy of every posting again, and with it a settle and a rank for each, where all but a handful of texts come
#: out the same. A copy made before that day may still hold a `data:` run until its posting is cleaned again: the
#: `heal-embedded-copies` command (`raw_copies.heal_embedded`) makes those copies again, once.
CLEANER_VERSION = 1

#: What each odd space becomes: a plain space (no-break, thin and narrow no-break spaces) or nothing (the zero-width
#: ones and the byte order mark).
_ODD_SPACE = {**{chr(c): " " for c in (0xA0, 0x2009, 0x202F)}, **{chr(c): "" for c in (0x200B, 0x200C, 0x200D, 0xFEFF)}}


def normalize(text: str | None) -> str:
    """The text with odd spaces made plain, trailing spaces gone and every run of blank lines one blank line."""
    plain = text or ""
    if not plain.isascii():  # only a text with a character outside ASCII can hold an odd space
        for odd, said in _ODD_SPACE.items():
            plain = plain.replace(odd, said)
    lines = [line.rstrip() for line in plain.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


# --- embedded data -------------------------------------------------------------------------------------------------

#: The start of a `data:` address: the scheme (in any case: `DATA:` is the same scheme), a media type, its parameters
#: (`;base64` is one) and the comma.
_DATA_HEAD = r"(?i:data):[\w.+-]+/[\w.+-]+(?:;[\w.+=-]+)*,"
#: A whole `data:` address where it is not held in quotes: the start and the run after it, up to the first space, quote,
#: bracket or angle. Not one that follows a letter or sits inside a web address ("metadata:", "?x=1&data:").
_DATA = r"(?<![\w/?&%#.-])" + _DATA_HEAD + r"""[^\s"'<>()\[\]]+"""
_ATTRS = r"""(?:\s+[\w:-]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'<>]+))?)*"""
#: In the order they are tried, each a wrapper that only carried the address, then the address bare: an opening tag
#: with it as an attribute's value (closed or not: a text may end inside one); a markdown image; a markdown link, whose
#: words stay; the address alone in square brackets; the address.
_EMBEDDED = re.compile(
    rf"""<\w+{_ATTRS}\s+[\w:-]+\s*=\s*(?:"\s*{_DATA_HEAD}[^"<>]*"|'\s*{_DATA_HEAD}[^'<>]*'|["']?{_DATA}["']?)"""
    rf"""{_ATTRS}\s*/?>?"""
    rf"|!\[[^\]]*\]\(\s*{_DATA}\s*\)"
    rf"|\[(?P<words>[^\]]*)\]\(\s*{_DATA}\s*\)"
    rf"|\[{_DATA}\]"
    rf"|{_DATA}")


def drop_embedded(text: str) -> tuple[str, list[dict]]:
    """The text without its embedded data, and what was taken: `[{rule: "embedded_data", lines, chars}]` (the lines
    that held some, the characters taken), or `[]` and the very same text when it holds none. A `data:` address goes
    with the image or link wrapper that only carried it (`_EMBEDDED`); a link's own words stay, and so does every other
    character, an ordinary web address of any length among them. A line left empty is tidied by `normalize`."""
    if "data:" not in text.lower():
        return text, []
    lines = text.split("\n")
    held = chars = 0
    for i, line in enumerate(lines):
        if "data:" not in line.lower():
            continue
        kept = _EMBEDDED.sub(lambda m: m.group("words") or "", line)
        if len(kept) != len(line):
            held += 1
            chars += len(line) - len(kept)
            lines[i] = kept
    if not held:
        return text, []
    return "\n".join(lines), [{"rule": "embedded_data", "lines": held, "chars": chars}]


# --- the boilerplate rules ----------------------------------------------------------------------------------------

_MONEY = re.compile(r"\$\s?\d")
_EEO = re.compile(
    r"equal (?:employment )?opportunity|does not discriminate|do not discriminate|without regard to (?:race|color|"
    r"religion|sex|gender|national origin|age|disability|veteran)|affirmative action|protected (?:veteran|class)", re.I)
_BENEFIT_WORDS = (r"health insurance|medical|dental|vision|401\(?k\)?|retirement plan|paid time off|\bpto\b|vacation|"
                  r"parental leave|tuition|wellness|life insurance|disability insurance|paid holidays|sick leave")
_BENEFIT = re.compile(_BENEFIT_WORDS, re.I)
_APPLY = re.compile(
    r"^\W*(?:to apply|how to apply|apply now|apply today|click (?:the )?apply|submit your (?:resume|application|cv)|"
    r"please (?:submit|send|apply)|interested\?)", re.I)


#: The words every match of `_EEO` holds, one for each of its alternatives (`postings/stems.py`): a new alternative of
#: `_EEO` needs its word here.
_EEO_STEMS = ("equal", "discriminate", "without regard", "affirmative", "protected")
#: The words of `_BENEFIT`, one for each of its alternatives but "401(k)", which is "401" and counts as four (its
#: spellings "401k", "401(k)", "401(k" and "401k)" are four different matches); a new alternative needs its word here.
_BENEFIT_STEMS = ("medical", "dental", "vision", "retirement plan", "paid time off", "pto", "vacation",
                  "parental leave", "tuition", "wellness", "life insurance", "disability insurance", "paid holidays",
                  "sick leave", "health insurance")


def _equal_opportunity(line: str) -> bool:
    if len(line) < 40:
        return False
    low = stems.folded(line)
    if low is not None and not any(word in low for word in _EEO_STEMS):
        return False
    return bool(_EEO.search(line))


def _benefits(line: str) -> bool:
    if len(line) < 40:
        return False
    low = stems.folded(line)
    # a line needs three different matches, and the words it holds bound how many it can have
    if low is not None and (4 if "401" in low else 0) + sum(1 for word in _BENEFIT_STEMS if word in low) < 3:
        return False
    return len({m.group(0).lower() for m in _BENEFIT.finditer(line)}) >= 3


def _how_to_apply(line: str) -> bool:
    return len(line) <= 300 and bool(_APPLY.search(line))


#: In the order they are tried: the name `removed` gives, and the test of a line.
RULES: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("equal_opportunity", _equal_opportunity), ("benefits", _benefits), ("how_to_apply", _how_to_apply))


def strip_boilerplate(text: str, normalized: bool = False) -> tuple[str, list[dict]]:
    """The text without the lines a rule takes out, and what each rule took: `[{rule, lines, chars}]`. A line with a
    dollar figure stays; a text that every line of would go stays whole. `normalized` says the text is `normalize`d
    already, so one that loses no line is not tidied a second time."""
    kept: list[str] = []
    taken: dict[str, list[int]] = {}
    for line in text.split("\n"):
        rule = None
        if line.strip() and not _MONEY.search(line):
            rule = next((name for name, test in RULES if test(line)), None)
        if rule is None:
            kept.append(line)
        else:
            counts = taken.setdefault(rule, [0, 0])
            counts[0] += 1
            counts[1] += len(line)
    clean = text if normalized and not taken else normalize("\n".join(kept))
    if not clean and text:
        return text, []
    return clean, [{"rule": name, "lines": taken[name][0], "chars": taken[name][1]} for name, _ in RULES
                   if name in taken]


# --- the fields -----------------------------------------------------------------------------------------------------


@dataclass
class Cleansed:
    """A cleansed copy before it is stored: the columns of `CleansedPosting`."""

    title: str | None = None
    employer: str | None = None
    locations: list[str] = field(default_factory=list)
    work_mode: str | None = None
    pay_min: int | None = None
    pay_max: int | None = None
    posted_date: date | None = None
    text_clean: str = ""
    removed: list[dict] = field(default_factory=list)
    marks: dict = field(default_factory=dict)
    partial: bool = True


def _words(value: object) -> str | None:
    return " ".join(str(value).split()) or None if value is not None else None


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _places(value: object) -> list[str]:
    """The places a posting holds: a list of one text, split where the index joins them (`;`), each once."""
    return list(dict.fromkeys(p for p in (_words(x) for x in str(value or "").split(";")) if p))


def cleanse(parsed: dict) -> Cleansed:
    """The cleansed copy of a posting's plain fields (`parsed`: title, company, location, work_mode, comp, comp_min,
    comp_max, posted, text, as a reader made them)."""
    # Imported late: the posting index keeps copies (`raw_copies`), and these modules read the index's own.
    from app.jobsearch.gating import posting_facts as pf
    from app.jobsearch.gating import structured_fill
    from app.jobsearch.postings.record import Field, Record

    text, embedded = drop_embedded(parsed.get("text") or "")
    text, removed = strip_boilerplate(normalize(text), normalized=True)
    removed = [*embedded, *removed]
    title, employer = _words(parsed.get("title")), _words(parsed.get("company"))
    places = _places(parsed.get("location"))
    partial = whole.why_not(text) is not None
    facts = pf.extract(text)
    fields = dict(structured_fill.fill(text, title, partial, facts).fields)
    given_mode = _words(parsed.get("work_mode"))
    mode = given_mode or (facts.mode.value if facts.mode else None)
    if given_mode:
        fields["work_mode"] = Field(given_mode, pf.LISTING, given_mode)
    if places:
        fields["location"] = Field(places, pf.LISTING, "; ".join(places))
    low, high = _int(parsed.get("comp_min")), _int(parsed.get("comp_max"))
    if low is not None or high is not None:
        said = _words(parsed.get("comp")) or "-".join(f"${n:,}" for n in (low, high) if n is not None)
        fields["pay"] = Field(f"${low or high:,}-${high or low:,}", pf.LISTING, said)
    elif facts.bands:
        low, high = facts.bands[0][0], facts.bands[0][1]
    posted = _date(parsed.get("posted"))
    marks = {name: f.as_dict() for name, f in fields.items()}
    Record.from_dict({"fields": marks})  # a mark with no source or quote is refused here, not stored
    return Cleansed(title=title, employer=employer, locations=places, work_mode=mode, pay_min=low, pay_max=high,
                    posted_date=posted, text_clean=text, removed=removed, marks=marks, partial=partial)
