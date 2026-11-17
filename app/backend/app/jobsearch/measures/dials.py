"""The measurable dials (docs/PRD.md path 46): facts about an employer or a posting that the app measures in code,
set by the user as Must or Prefer (or left at Don't care), never judged by a reader.

A dial has no default: it is the user's setting, and a user who sets none has no dial. It is stored as a line of Hard
pass (a Must: a gate, `Gate` of kind `employer_fact` for an employer dial and `job_fact` for a job dial) or of
Prioritization (a Prefer: an `Ask` the app decides, with points), each naming the dial's fact, a comparison
(`op`) and a value. The comparison is the dial's condition, and what the choice does with it:

- Must: the condition has to hold; it fails when it does not, and *waits* when the fact is not stated (46.7).
- Prefer: the condition holding earns the points. There is no Avoid: Prioritization only adds points, and excluding
  something is Hard pass's job (docs/PRD.md 30.10, 46.1).

For a number the condition reads toward what the user wants: "within 50 miles" is a Must or a Prefer.
A pick list is "in" the listed values in both. Title words is a list of the user's own words and phrases and is a
Must only: a title that holds any of them is stopped (op `excludes`; the list is the user's data, never ours). Travel
is a number read as a ceiling ("at most 25%") and a Must only; Employer named is a flag, a Must only, with nothing to
set (op `=`, value `yes`): the posting has to name its employer. Leads business development is a pick of yes and no,
a Must or a Prefer (the owner, 2026-11-05): yes when leading
business development is a main responsibility of the job (owning a sales or new-business pipeline, a revenue, bookings
or quota target, winning new clients or accounts, closing deals), no when it is not (helping with proposals, keeping
existing client relationships and working beside a sales team do not count). It is read once from the posting, by a
short Haiku read of the sentences that speak of selling (`gate_facts`, `posting_facts.bd_lines`), and stored with the
sentence it rests on; unknown is no fact, so a Must on it passes a posting that says nothing (`passes_unstated`), and a
Must stops a role only on the answer the user did not choose, a `no` only when a model read it: the `no` code writes
for a posting with no sentence about selling is a miss of a word list, not a read, and no gate reads it.
This module is a leaf: it holds
the table of dials, the words of their lines and the home place; the checks are `scoring.compare` and `gating`."""

import functools
import re
from dataclasses import dataclass

from app.jobsearch.measures import distance

MUST, PREFER = "must", "prefer"
EMPLOYER, JOB = "employer", "job"
#: The fact the Leads business development dial reads: `yes` or `no`, stored on the role with the sentence it rests on.
LEADS_BD = "leads_bd"
#: The fact the Main work dial reads: one of the owner's words (or `none`), stored on the role with its quote.
MAIN_WORK = "main_work"

#: The headcount bands (docs/PRD.md 21.4), by the number of employees below which each ends. A fixed table: published
#: sizes, not one person's preference.
SIZE_BANDS = (("micro", 10), ("small", 100), ("mid", 1000), ("large", None))
MODES = ("remote", "hybrid", "onsite")

#: What each value of a pick list is called in a sentence.
LABELS: dict[str, str] = {
    "micro": "under 10 employees", "small": "10 to 99 employees", "mid": "100 to 999 employees",
    "large": "1,000 employees or more",
    "public": "public", "private": "private", "pe": "private equity", "vc": "venture-backed", "nonprofit": "nonprofit",
    "government": "government",
    "remote": "remote", "hybrid": "hybrid", "onsite": "onsite",
    "degree": "a degree (level not stated)", "associate": "an associate degree", "bachelor": "a bachelor's degree",
    "master": "a master's degree", "doctorate": "a doctorate",
    "required": "a security clearance required", "preferred": "a security clearance preferred", "none": "no clearance",
}


@dataclass(frozen=True)
class Dial:
    """One dial: its id, whose fact it reads (`side`), the words the page shows, the fact it reads, whether its value is
    a pick list, a number, the user's own words or a flag with nothing to set (`kind`), the choices it takes, and for a
    number the unit it is said in."""

    id: str
    side: str
    label: str
    fact: str
    kind: str
    choices: tuple[str, ...]
    unit: str = ""
    #: What "the employer's ..." or "the posting's ..." says of the fact in a sentence.
    noun: str = ""
    #: The words that stand before a number's figure in a sentence when they are not the comparison's own ("at most").
    lead: str = ""
    #: A Must whose fact the posting does not state passes instead of waiting (docs/PRD.md 46.7): a posting that says
    #: nothing of travel asks none. Leads business development passes too, for another reason: the step that reads it
    #: never reads the roles already past Ready, so a Must that waited would hold back (`dial_hold`) the roles of the
    #: deck that no read will ever reach, and the fact would never arrive.
    passes_unstated: bool = False


_ALL = (MUST, PREFER)
DIALS: dict[str, Dial] = {d.id: d for d in (
    Dial("size", EMPLOYER, "Size", "size_band", "pick", _ALL, noun="the employer's size"),
    Dial("ownership", EMPLOYER, "Ownership", "ownership", "pick", _ALL, noun="the employer's ownership"),
    Dial("industry", EMPLOYER, "Industry", "industry", "pick", _ALL, noun="the employer's industry"),
    Dial("distance", JOB, "Distance from home", "distance_miles", "number", _ALL, "miles", "the distance from home"),
    Dial("experience", JOB, "Years of experience asked", "experience_years", "number", _ALL, "years",
         "the years of experience asked"),
    Dial("degree", JOB, "Degree required", "degree_required", "pick", _ALL, noun="the degree required"),
    Dial("freshness", JOB, "Freshness", "posted_days", "number", _ALL, "days", "the days since it was posted"),
    Dial("travel", JOB, "Travel", "travel_percent", "number", (MUST,), "%", "the travel asked", lead="at most",
         passes_unstated=True),
    Dial("pay", JOB, "Pay", "pay_top", "number", (PREFER,), "a year", "the top of the posted pay"),
    Dial("mode", JOB, "Work mode", "work_mode", "pick", (PREFER,), noun="the way of working"),
    Dial("clearance", JOB, "Clearance", "clearance", "pick", (PREFER,), noun="the security clearance"),
    Dial("title", JOB, "Title words", "title", "words", (MUST,), noun="the title"),
    Dial("employer-named", JOB, "Employer named", "employer_named", "flag", (MUST,), noun="the employer"),
    Dial("business-development", JOB, "Leads business development", LEADS_BD, "pick", _ALL,
         noun="whether the role leads business development", passes_unstated=True),
    Dial("main-work", JOB, "Main work", MAIN_WORK, "words", (MUST,), noun="the role's main work", passes_unstated=True),
)}
BY_FACT = {d.fact: d for d in DIALS.values()}
#: The facts of a posting a job dial reads.
JOB_FACTS = tuple(d.fact for d in DIALS.values() if d.side == JOB)


def size_band(employees: int | None) -> str | None:
    """The headcount band an employer's number of employees is in, or None when the record has no number."""
    if employees is None:
        return None
    return next(name for name, below in SIZE_BANDS if below is None or employees < below)


def values_of(value) -> list[str]:
    """The values a pick list's line holds, in order: its comma-separated words."""
    return [v.strip().lower() for v in str(value or "").split(",") if v.strip()]


@functools.lru_cache(maxsize=1024)
def _word_regex(word: str) -> re.Pattern | None:
    """The whole-word pattern for one Title words entry, or None when it would be empty."""
    pattern = r"[\s\-]+".join(re.escape(part) for part in re.split(r"[\s\-]+", word) if part)
    if not pattern:
        return None
    before = r"(?<!\w)" if word[0].isalnum() or word[0] == "_" else ""
    after = r"(?!\w)" if word[-1].isalnum() or word[-1] == "_" else ""
    return re.compile(f"{before}{pattern}{after}", re.IGNORECASE)


def title_hits(title: str | None, value, except_value=None) -> list[str]:
    """The words and phrases of a Title words list that a title holds, in the list's order. A match is whole words, in
    any case; the spaces and hyphens inside a phrase stand for each other ("entry level" is "Entry-Level"), and every
    other character is itself ("(2027 start)"), so "intern" is not "internal", "international" or "intermediate".
    `except_value` is a list of phrases, found the same way: a listed word that sits wholly inside one of them
    where it occurs is no hit ("assistant vice president" lets that title past "vice
    president"), and one outside them still is."""
    text = title or ""
    spans = []
    for phrase in values_of(except_value):
        ex = _word_regex(phrase)
        if ex is not None:
            spans += [m.span() for m in ex.finditer(text)]
    hits = []
    for word in values_of(value):
        rx = _word_regex(word)
        if rx is None:
            continue
        if any(not any(a <= m.start() and m.end() <= b for a, b in spans) for m in rx.finditer(text)):
            hits.append(word)
    return hits


def title_hit_words(hits: list[str]) -> str:
    """The sentence a role stopped by Title words is given: the word or words of the list that the title holds."""
    quoted = [f'"{h}"' for h in hits]
    named = ", ".join(quoted[:-1]) + " and " + quoted[-1] if len(quoted) > 1 else "".join(quoted)
    return f"The title has {named}, which the Title words exclude."


def number_words(fact: str, op: str, value) -> str:
    """A number's condition in words: "within 50 miles", "more than 8 years", "at least $120,000 a year"."""
    dial = BY_FACT[fact]
    try:
        n = float(value)
    except (TypeError, ValueError):
        return f"{dial.noun} {op} {value}"
    shown = f"{n:,.0f}" if n == int(n) else f"{n:,g}"
    if fact == "pay_top":
        shown = f"${shown} a year"
    elif dial.unit == "%":
        shown = f"{shown}%"
    elif dial.unit:
        shown = f"{shown} {dial.unit}"
    word = dial.lead or {"<=": "within", "<": "under", ">=": "at least", ">": "more than", "=": "exactly",
                         "!=": "other than"}.get(op, op)
    return f"{word} {shown}"


def is_dial(fact: str | None, op: str | None) -> bool:
    """Whether a line on this fact and comparison is a dial's. An employer fact the owner set before the dials
    (`ownership != pe`, `industry contains health`) is not: it keeps the check and the words it always had."""
    dial = BY_FACT.get(fact or "")
    if dial is not None and dial.kind == "words":
        return op == "excludes"
    if dial is not None and dial.kind == "flag":
        return op == "="
    return dial is not None and (op == "in" if dial.kind == "pick" else dial.side == JOB)


def leads_bd_words(have: str, why: str | None = None) -> str:
    """What the posting says of leading business development, as the fact a gate rests on: the sentence that says it
    when it does (`yes`), else that it does not show it as a main responsibility."""
    if have == "yes":
        return "the posting says the role leads business development" + (f': "{why}"' if why else "")
    return "the posting does not show leading business development as a main responsibility"


def main_work_words(word: str, why: str | None = None) -> str:
    """What the posting says of the role's main work, as the fact a gate rests on: the owner's word it matched and the
    quote of the posting that says it."""
    return f"the posting says the role's main work is \"{word}\"" + (f': "{why}"' if why else "")


def says(fact: str, op: str | None, value, except_value=None) -> str | None:
    """The sentence for a dial's line, or None when the line is not a dial's: the condition in plain words."""
    dial = BY_FACT.get(fact)
    if dial is None or not is_dial(fact, op):
        return None
    if dial.kind == "flag":
        return "The posting names its employer."
    if dial.kind == "words":
        words = values_of(value)
        listed = ", ".join(words[:-1]) + " or " + words[-1] if len(words) > 1 else "".join(words) or "nothing listed"
        sentence = f"{dial.noun[0].upper()}{dial.noun[1:]} has none of: {listed}"
        phrases = values_of(except_value)
        if phrases:
            skipped = ", ".join(phrases[:-1]) + " or " + phrases[-1] if len(phrases) > 1 else phrases[0]
            sentence += f", except in: {skipped}"
        return sentence + "."
    if dial.kind == "pick":
        names = [LABELS.get(v, v) for v in values_of(value)]
        if dial.id == "industry":
            names = [str(v) for v in values_of(value)]
        joined = ", ".join(names[:-1]) + " or " + names[-1] if len(names) > 1 else "".join(names) or "nothing chosen"
        verb = "is" if dial.id != "industry" else "is in"
        return f"{dial.noun[0].upper()}{dial.noun[1:]} {verb} {joined}."
    return f"{dial.noun[0].upper()}{dial.noun[1:]} is {number_words(fact, op or '=', value)}."


def home_place(gates) -> str | None:
    """The user's home place: the first in-person place on Hard pass's work location that the gazetteer knows, else the
    first hybrid one. None when neither names a place it knows. (The app holds no other place of the user's.)"""
    location = gates.location
    for way in (location.in_person, location.hybrid):
        for place in way.locations:
            if distance.places_in(place):
                return place
    return None
