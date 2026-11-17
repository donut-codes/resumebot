"""The outline Gates and Scoring follow, and the plain sentences the page shows for the gates (docs/PRD.md 30.4, 30.6).

Gates and Scoring both follow the outline of a posting (Company, Job posting, Description, Responsibilities,
Qualifications, Preferred, Benefits / Compensation). Each of those sections has one On switch. An off section stops
nothing and counts for nothing, but stays on the page with everything in it. The switches are stored as `sections_off`,
the ids of the sections that are off; empty means every section is on.

This module holds the section ids, the pure sentences the page shows for the gates (so a note, a reason a posting was
stopped and the page all say the same thing), and the flat list of locations the work location keeps for each way of
working.
"""

from app.jobsearch.measures import dials

#: The seven parts of a posting, in the order a posting reads (profile_base.SECTIONS holds their names).
SECTION_IDS = ("company", "posting", "description", "responsibilities", "qualifications", "preferred", "compensation")

#: The part a gate sits in when none was stored, from what it is and never from its words (profile_gates.Gate).
GATE_HOME: dict[str, str] = {"rule": "description", "employer_fact": "company", "posting_age": "posting",
                             "job_fact": "posting"}


def in_order(ids, allowed: tuple[str, ...]) -> list[str]:
    """Section ids once each, in the order of the outline; anything that is not a section is dropped."""
    wanted = {i for i in ids or [] if isinstance(i, str)}
    return [i for i in allowed if i in wanted]


# --- the sentences the page shows for the gates -------------------------------------------------------------------


def list_words(items: list[str], limit: int = 6) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


def or_words(items: list[str], limit: int = 6) -> str:
    """Items as "a, b or c" (the first few, then how many more)."""
    shown = list(items[:limit])
    more = len(items) - len(shown)
    if more > 0:
        return f"{', '.join(shown)} and {more} more"
    return shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} or {shown[-1]}" if shown else ""


def location_words(remote: tuple[bool, list[str]], hybrid: tuple[bool, list[str]],
                   in_person: tuple[bool, list[str]]) -> str:
    """The work location as one sentence: the ways of working that are allowed, and where each must be. Each way is
    (allowed, its locations)."""
    ways = []
    if remote[0]:
        ways.append("remote" + (f" (in {list_words(remote[1])})" if remote[1] else ""))
    for label, (allowed, places) in (("hybrid", hybrid), ("in person", in_person)):
        if allowed:
            ways.append(f"{label} (in {list_words(places)})" if places else f"{label} (anywhere)")
    sentence = f"Work must be {or_words(ways)}" if ways else "No way of working is allowed yet"
    return sentence + "."


def compensation_words(amount: int | None, against: str) -> str:
    if amount is None:
        return "Pay: no amount set yet."
    way = {"minimum": "the posted minimum", "maximum": "the posted maximum", "ask": "the estimated ask"}.get(
        against, "the estimated ask")
    return f"Pay must be at least ${amount:,} a year, on {way}."


def line_words(kind: str, text: str, days, fact, op, value, except_words=None) -> str:
    """One plain sentence for a gate in a part of a posting, from its kind and values."""
    if kind == "posting_age":
        return f"Postings older than {days} days are gated out." if days else "Posting age: no limit set."
    if kind in ("employer_fact", "job_fact") and (dial := dials.says(str(fact or ""), op, value, except_words)):
        return dial
    if kind == "job_fact":
        return "Job fact: none chosen yet."
    if kind == "employer_fact":
        if not fact:
            return "Employer fact: none chosen yet."
        words = {">=": "at least", "<=": "at most", ">": "more than", "<": "fewer than", "=": "is", "!=": "is not",
                 "contains": "includes", "excludes": "doesn't include"}.get(op or "=", "is")
        return f"The employer's {str(fact).replace('_', ' ')} {words} {value}."
    return text or "A rule with no words yet."


# --- the work location: one list of locations for each way of working ---------------------------------------------


def flat_locations(names: list[str]) -> list[str]:
    """Locations once each in the order given, compared without regard to case (the first spelling stays)."""
    seen, out = set(), []
    for name in names:
        name = str(name).strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out
