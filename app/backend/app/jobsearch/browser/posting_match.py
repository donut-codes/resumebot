"""Is the posting we hold still on the page? Go's main check that a posting is open (the owner, 2026-10-16).

Every role in Go's batch holds its posting's whole text (`Opportunity.description`, `postings/text.py`). A page that
still carries the posting carries that text; a page that does not (a "page doesn't exist" notice, a redirect to a job
search or a careers home, an expired or filled notice) carries little or none of it, whatever words it uses. So the
page's visible text is compared with the text held, and no list of phrases is needed:

- **The title**: most of the held title's words are on the page (`TITLE_SHARE`).
- **The text**: the share of the held text's runs of `SHINGLE` words that appear on the page.

`MATCH` (open): at least `OPEN_SHARE` of the text (the title too, as a rule; a role added by its link holds the link as
its title, so the text decides). `MISSING`: under `MISSING_SHARE` of the text; the caller closes the role on it only
when the title is not there either (`go_checks.page_verdict`). `CHANGED`: in between: the posting is still up in other
words, so it counts as open and is flagged. `None`: no verdict, because the role holds too little text to compare
(`MIN_SHINGLES`) or the page shows too little to judge (`MIN_PAGE_WORDS`: a page that has not drawn yet is not a
missing posting).

The thresholds were set on real pages (a batch of postings and a job system's "doesn't exist" pages): the pages that
showed their posting shared 0.91 to 1.0 of its text, those that did not 0.0 to 0.08. Pure functions; no I/O."""

from __future__ import annotations

import html as htmllib
import re
import unicodedata
from dataclasses import dataclass

#: Words in a run that is compared, the share of runs that says the posting is there or missing, and the share of the
#: title's words that must be on the page.
SHINGLE = 5
OPEN_SHARE, MISSING_SHARE = 0.5, 0.15
TITLE_SHARE = 0.75
#: How many words more than its own length a title may be spread over in a text and still be the title there
#: (`title_together`).
TITLE_SLACK = 4
#: Below these the comparison says nothing: the held text has too few runs, or the page too few words.
MIN_SHINGLES, MIN_PAGE_WORDS = 20, 40

MATCH, CHANGED, MISSING = "match", "changed", "missing"

_WORD = re.compile(r"[a-z0-9]+")
#: Title words that say nothing on their own, and the short forms a page may spell out.
_SMALL = frozenset({"a", "an", "and", "the", "of", "for", "to", "in", "at", "on", "with", "or", "remote", "hybrid"})
_SAME = {"sr": "senior", "jr": "junior", "mgr": "manager", "dir": "director", "vp": "vice president",
         "ai": "ai", "ops": "operations", "eng": "engineer", "pm": "program manager"}


def words(text: str | None) -> list[str]:
    """The text as lower-case words, accents and punctuation dropped."""
    plain = unicodedata.normalize("NFKD", htmllib.unescape(text or "")).encode("ascii", "ignore").decode()
    return _WORD.findall(plain.lower())


def _shingles(ws: list[str]) -> set[tuple[str, ...]]:
    return {tuple(ws[i:i + SHINGLE]) for i in range(max(0, len(ws) - SHINGLE + 1))}


def _title_words(title: str | None) -> set[str]:
    out: set[str] = set()
    for w in words(title):
        out.update(_SAME.get(w, w).split())
    return {w for w in out if w not in _SMALL and len(w) > 1}


@dataclass(frozen=True)
class Match:
    """What the comparison found: the verdict (`MATCH`, `CHANGED`, `MISSING`, or None), the share of the held text on
    the page, and whether the title is there."""

    verdict: str | None
    share: float
    title: bool

    def words(self) -> str:
        return f"{round(self.share * 100)}% of the posting's text is on the page, and its title is " + (
            "there" if self.title else "not")


def _spelled_both_ways(page_words: list[str]) -> set[str]:
    """The words of a page, and each short form's long form beside it: a page that spells "Sr." as "Senior"."""
    out = set(page_words)
    for w in list(out):
        out.update(_SAME.get(w, w).split())
    return out


def _title_share(wanted: set[str], page_set: set[str]) -> float:
    return len(wanted & page_set) / len(wanted)


def title_together(title: str | None, text: str | None) -> float | None:
    """The share of the title's words that lie together in the text, or None when the title has no word that can be
    checked (it is empty, or has no Latin letter or digit): the most of them found within one stretch of the text as
    long as the title and `TITLE_SLACK` more words, a short form counting as the long one ("Sr." as "Senior").
    `TITLE_SHARE` of them together make the title there. The same words far apart are not the title: a long page of
    values says "program" in one paragraph and "manager" in another."""
    wanted = _title_words(title)
    if not wanted:
        return None
    span = len(words(title)) + TITLE_SLACK
    found = [(at, word) for at, page_word in enumerate(words(text))
             for word in {page_word, *_SAME.get(page_word, page_word).split()} if word in wanted]
    best, first = 0, 0
    inside: dict[str, int] = {}
    for at, word in found:
        inside[word] = inside.get(word, 0) + 1
        while at - found[first][0] >= span:  # the stretch is longer than the title may be spread over
            gone = found[first][1]
            inside[gone] -= 1
            if not inside[gone]:
                del inside[gone]
            first += 1
        best = max(best, len(inside))
    return best / len(wanted)


def compare(title: str | None, held: str | None, page_text: str | None) -> Match:
    """How much of the posting held (`title`, `held` text) the page's visible text (`page_text`) still carries."""
    held_runs = _shingles(words(held))
    page_words = words(page_text)
    wanted = _title_words(title)
    has_title = bool(wanted) and _title_share(wanted, _spelled_both_ways(page_words)) >= TITLE_SHARE
    if len(held_runs) < MIN_SHINGLES:
        return Match(None, 0.0, has_title)
    share = len(held_runs & _shingles(page_words)) / len(held_runs)
    if share >= OPEN_SHARE:  # most of the posting's own words: it is there, even where the title held is the app's
        return Match(MATCH, share, has_title)  # stand-in ("<board>/jobs/view/…") or longer than the page's
    if share < MISSING_SHARE:
        return Match(MISSING if len(page_words) >= MIN_PAGE_WORDS else None, share, has_title)
    return Match(CHANGED, share, has_title)
