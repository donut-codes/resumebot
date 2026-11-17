"""Code fillers for the structured record: pay, years of
experience, degree, work mode and travel, each with the line of the posting it came from; the title from the listing,
never from the text. Pay, work mode and travel are `posting_facts` (and `gates.parse_comp_text` under pay); years and
degree are read here.

What a posting does not state stays empty. A line that says "preferred", "a plus" or "nice to have" is not a
requirement, so it fills neither years nor degree. When one line gives several years figures the largest of the stated
minimums is the value and the others are noted in `also` for a model to settle. Reads no database and calls no model.
"""

import functools
import re

from app.jobsearch.gating import posting_facts as pf
from app.jobsearch.postings import stems
from app.jobsearch.postings.record import Field, Record

_SPLIT = re.compile(r"(?<=[.!?])\s+|\n")
_EXPERIENCE_OR_YEARS = re.compile(r"experience|years", re.I)
_OPTIONAL = re.compile(r"prefer|a plus|nice to have|bonus", re.I)
_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|to)\s*(\d{1,2})\s*)?(?:\+\s*)?years?", re.I)
#: Degree levels, highest first.
_DEGREES = (("phd", r"ph\.?d|doctorate|doctoral"),
            ("master", r"master'?s|\bmba\b|graduate degree|advanced degree"),
            ("bachelor", r"bachelor'?s|\bb\.?[sa]\.?\b(?=\s+(?:degree|in))|undergraduate degree|four[\s-]year degree|"
                         r"college degree|university degree"),
            ("associate", r"associate'?s"), ("degree", r"\bdegree\b"))
_DEGREE_RES = tuple((name, re.compile(pattern, re.I)) for name, pattern in _DEGREES)
#: The words every match of `_DEGREES` holds, one for each of its alternatives (`postings/stems.py`): a new alternative
#: needs its word here.
_DEGREE_STEMS = ("ph", "doctor", "master", "mba", "bachelor", "bs", "ba", "b.s", "b.a", "associate", "degree")


@functools.lru_cache(maxsize=4)
def _sentences(text: str | None) -> tuple[str, ...]:
    """The sentences and lines of a text, kept for the last few texts as a tuple nobody changes: one posting is read for
    its years, its degree and the facts of `posting_facts`, from the same text."""
    return tuple(s.strip() for s in _SPLIT.split(text or "") if s.strip())


def _says_years(sentence: str) -> bool:
    """Whether the sentence says "experience" or "years": `_EXPERIENCE_OR_YEARS`, asked of a plain word test first."""
    low = stems.folded(sentence)
    return bool(_EXPERIENCE_OR_YEARS.search(sentence)) if low is None else "experience" in low or "years" in low


def years_from_text(text: str | None) -> Field | None:
    """The years of experience a posting requires: the largest stated minimum, with the sentence it is in. Other
    figures (the top of a range, a second figure) are `also`."""
    best: tuple[int, str, list[int]] | None = None
    for sentence in _sentences(text):
        if not _says_years(sentence) or _OPTIONAL.search(sentence):
            continue
        low, extra = [], []
        for m in _YEARS.finditer(sentence):
            low.append(int(m[1]))
            if m[2]:
                extra.append(int(m[2]))
        if low and (best is None or max(low) > best[0]):
            best = (max(low), sentence, sorted({*low, *extra} - {max(low)}))
    if best is None:
        return None
    value, sentence, rest = best
    return Field(value, pf.CODE, pf._clip(sentence), tuple(rest))


def degree_from_text(text: str | None) -> Field | None:
    """The highest degree word a required line states (phd, master, bachelor, associate, or just degree)."""
    order = [name for name, _ in _DEGREES]
    best: tuple[str, str] | None = None
    for sentence in _sentences(text):
        low = stems.folded(sentence)
        if low is not None and not any(word in low for word in _DEGREE_STEMS):
            continue
        if _OPTIONAL.search(sentence):
            continue
        for name, pattern in _DEGREE_RES:
            if pattern.search(sentence):
                if best is None or order.index(name) < order.index(best[0]):
                    best = (name, sentence)
                break
    return Field(best[0], pf.CODE, pf._clip(best[1])) if best else None


def _field(found: pf.Found | None) -> Field | None:
    return Field(found.value, found.by, found.quote) if found else None


def _pay(facts: pf.Extracted) -> Field | None:
    """The first range stated, with every other range the posting gives in `also` (one for each place): a gate may stop
    a role only on the most generous of them all (`posting_facts.generous`)."""
    got = _field(facts.pay)
    others = tuple(f"${low:,}-${high:,}" if low != high else f"${low:,}" for low, high, _ in facts.bands[1:])
    return Field(got.value, got.source, got.quote, others) if got else None


def fill(text: str | None, listing_title: str | None = None, partial: bool = False,
         facts: pf.Extracted | None = None) -> Record:
    """The record code can fill from a posting's text and its listing's title: only what the posting states. `facts` are
    the facts already read of this same text (`posting_facts.extract(text)`); without them they are read here."""
    facts = facts if facts is not None else pf.extract(text)
    title = (listing_title or "").strip()
    filled = {
        "title": Field(title, pf.LISTING, title) if title else None,
        "pay": _pay(facts), "work_mode": _field(facts.mode), "travel": _field(facts.travel),
        "years": years_from_text(text), "degree": degree_from_text(text),
    }
    return Record({k: v for k, v in filled.items() if v is not None}, partial)
