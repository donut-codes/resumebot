"""What makes a text a role's text: whole, verbatim, from a source (docs/PRD.md 39.29, the owner, 2026-10-14).

This is the one rule every way in of a posting's text asks, and it asks it here and nowhere else: the owner's browser's
capture (`browser/handback.py`), the employer's own page as the app reads it (`postings/index.py`), a scout's and a
reader's description (`missions/search_run.py`, `browser/reader.py`), a site recipe's text (`browser/recipes`,
`sites/recipes.py`), the text a job added by its link names itself with (`browser/add_link.py`), what the cleaner marks
as partial (`postings/cleanse.py`) and the text a role holds (`postings/text.py`). A leaf: it imports no service
module, so the cleaner and the index, which sit below `text.py`, can ask it. A text handed in whole (a capture, a
board's own posting) is asked as one question, `refusal` (`why_not`, then `too_long`), by `take` and by the way in that
must tell its sender why.

- **Whole** (the owner, 2026-10-14: a partial result is bad data). A text is kept only when nothing says it is not the
  whole posting (`why_not`): a fragment, a text cut off where a page says "see more", a text that says it is shortened,
  a sign-in wall or a bot check, a web page read whole (`page_dump`).
- **Verbatim, nothing cut.** A text past what one reply to an agent can hold (`CEILING_CHARS`) is refused where an agent
  sends it (`too_long`), with its size, and never cut; the text the app reads itself is stored as it is.
- **From a source** (the owner, 2026-10-15). Only a job system's own answer, the app's own read of a role's source, or
  the owner's browser's capture of it is a posting's text (`of_posting`): never a listing's snippet, a scout's or
  reader's report, the tracker's, or the analyst's summary.

Two thresholds stay two on purpose (the tests pin it): a job system's own answer is kept from `cleansed.MIN_TEXT`
characters (`of_posting`, `thin`), because the system's reader gave it; a page the app read, or the owner's browser
captured, is kept only from 500 (`why_not`), because a short page is more often a cookie notice than a posting. The
origin of a text picks the test; no caller does. Pure functions, no database, no network (`thin_sql` only builds
the expression a query asks the same rule with)."""

import re
from typing import TYPE_CHECKING

from sqlalchemy import case, false, func

from app.jobsearch import ats
from app.jobsearch.owner_text import ROOM_CHARS
from app.jobsearch.postings import cleansed, stems

if TYPE_CHECKING:
    from app.jobsearch.models import JobPosting

#: The most of a posting's text an agent may send the app: the room one reply to an agent leaves for a text beside the
#: rest of its task (`owner_text.ROOM_CHARS`). A posting is a few thousand characters, so one past it is a page scraped
#: whole, and it is refused with its size (`too_long`); the text the app holds is never cut to this, or to anything.
CEILING_CHARS = ROOM_CHARS
#: A career page with less text than this did not show the posting (a cookie notice, a page the browser draws), and its
#: text is not kept as the posting's (`why_not`).
_MIN_PAGE_CHARS = 500
#: Under this many characters a text that names a wall is the wall, not the posting.
_WALL_MAX_CHARS = 2_000
#: A text that ends this way was cut where the page shows "see more" (a listing's preview, a clipped description).
_CUT_OFF = re.compile(r"(\.\.\.|…|\b(see|show|read)\s+more\b|…\s*more)\s*$", re.I)
#: What a shortened, summarised or placeholder text says of itself.
_SHORTENED = re.compile(
    r"\[\s*(?:\.\.\.|…|truncated|shortened|abridged|omitted|summar(?:y|i[sz]ed)|placeholder|rest of [^\]]*|"
    r"continued[^\]]*|full (?:text|description)[^\]]*)\s*\]"
    r"|\((?:truncated|shortened|abridged|summari[sz]ed|omitted|text cut)\)"
    r"|\bplaceholder\b|\blorem ipsum\b"
    r"|\b(?:rest|remainder) of (?:the )?(?:description|posting|text|page)\b[^.\n]{0,40}"
    r"\b(?:omitted|truncated|not (?:shown|copied|included))"
    r"|\b(?:description|text|posting) (?:was |has been )?(?:truncated|shortened|abridged|summari[sz]ed)\b", re.I)
#: What a sign-in wall, a sign-up prompt or a bot check says in place of a posting. Only a short text is taken for one:
#: a whole posting may well say "sign in" somewhere in its middle.
_WALL = re.compile(
    r"(sign in to (view|see|apply|continue|read)|log in to (view|see|apply|continue)|join (now )?to (view|see|apply)|"
    r"create (your|a) (free )?account|verify you are (a )?human|are you a robot|unusual activity|"
    r"enable javascript|access denied|captcha)", re.I)
#: What a page read whole leaves in its text: markup, the skip link, or a language menu beside "Log in".
_MARKUP = re.compile(r"<!doctype\b|<html\b|<body\b|</?(?:div|span|script|style|nav|head|meta|link)\b[^<>]*>"
                     r"|<[a-z]+-[a-z-]+\b[^<>]*>", re.I)
_SKIP_LINK = re.compile(r"^\s*skip to (?:main )?content\s*$", re.I | re.M)
_LANGUAGE = re.compile(r"^\s*(?:-\s*)?(?:english|french|german|deutsch|spanish|japanese|italian|italiano|portuguese|"
                       r"dutch|nederlands|chinese|korean|polish|polski)\s*$", re.I | re.M)
_SIGN_IN = re.compile(r"^\s*(?:-\s*)?(?:log ?in|sign ?in)\s*$", re.I | re.M)


def thin(text: str | None) -> bool:
    """Whether a text is too short to be a posting's (under `cleansed.MIN_TEXT` characters: a title and a few header
    lines, what a list gave), the rule that leaves a role in Followup needed (`lifecycle.stage_of`)."""
    return len((text or "").strip()) < cleansed.MIN_TEXT


#: What `thin_sql` trims: the ASCII white space `str.strip` takes off a text, which a database does not by default.
_WHITE = " \t\r\n\f\v"
#: How much of a text `thin_sql` looks at first: `MIN_TEXT` characters and room for white space at the two edges of the
#: prefix. Longer costs more than it settles (a database unpacks four bytes for each character asked of a stored text),
#: shorter leaves a text whose 300th character is a space to the whole (the cost grows with the length looked at, and
#: most with measuring each whole text).
_LOOK_AT = cleansed.MIN_TEXT + 20


def thin_sql(column):
    """`thin` as a SQL expression over a text column (none counts as no text): the same rule asked by a query, so a
    posting a query picks as thin is one `thin` calls thin (`tests/jobsearch/test_whole.py` holds the two together).

    A text whose first `_LOOK_AT` characters hold `MIN_TEXT` between their first and last non-white character is not
    thin, whatever follows (white space inside or after the prefix only adds to the trimmed text), so the query unpacks
    only that prefix of such a text (`substr` slices a stored text) and measures the whole text, trimmed, for the few
    the prefix does not settle: a snippet, or a text padded with white space."""
    kept = func.coalesce(column, "")
    return case((func.length(func.trim(func.substr(column, 1, _LOOK_AT), _WHITE)) >= cleansed.MIN_TEXT, false()),
                else_=func.length(func.trim(kept, _WHITE)) < cleansed.MIN_TEXT)


def page_dump(text: str | None) -> bool:
    """Whether a text is a web page read whole rather than a posting: it holds markup (`<!DOCTYPE html>`, a tag), a
    "Skip to content" line, or a menu of languages beside a "Log in" line. "Whole and verbatim" is the whole posting,
    never the page's chrome (the owner, 2026-10-17)."""
    kept = text or ""
    if _MARKUP.search(kept) or _SKIP_LINK.search(kept):
        return True
    return len(_LANGUAGE.findall(kept)) >= 3 and bool(_SIGN_IN.search(kept))


#: The words every match of `_SHORTENED` holds, one for each of its alternatives (`postings/stems.py`): a new
#: alternative needs its word here. Single spaces, as the pattern has them.
_SHORT_STEMS = ("...", "…", "truncated", "shortened", "abridged", "omitted", "summar", "placeholder", "rest of",
                "continued", "full text", "full description", "text cut", "lorem ipsum", "remainder of")


def _maybe_shortened(kept: str) -> bool:
    low = stems.folded(kept)
    return low is None or any(word in low for word in _SHORT_STEMS)


def why_not(text: str | None) -> str | None:
    """Why a text cannot be taken for the whole of a posting, in a few words, or None when nothing says it is not. The
    owner's rule (2026-10-14): a partial result is bad data, so a posting's text is kept only when it is the whole of
    it. A fragment (under 500 characters: a snippet, a preview, a cookie notice), a text cut off where a page says "see
    more", a text that says it is shortened, summarised or a placeholder, and a sign-in wall or a bot check are not.
    Pure: it cannot tell a posting cut in its middle, so the reader that copied it says it is whole as well
    (`browser.texts`), and the app's fetch reads a page's own embedded posting first."""
    kept = (text or "").strip()
    if not kept:
        return "no text"
    if len(kept) < _MIN_PAGE_CHARS:
        return f"only {len(kept)} characters, a snippet or a preview, not the whole posting"
    if _CUT_OFF.search(kept):
        return "it ends where the page says 'see more': the posting was cut off"
    if found := (_SHORTENED.search(kept) if _maybe_shortened(kept) else None):
        return f"it says it is shortened or a placeholder ({found.group(0)[:40]!r}), not the whole posting"
    if len(kept) < _WALL_MAX_CHARS and _WALL.search(kept):
        return "it is a sign-in wall or a check, not the posting"
    if page_dump(kept):
        return "it is a web page read whole (its markup, menus or links), not the posting's own text"
    return None


def too_long(text: str | None, what: str) -> str | None:
    """The sentence that refuses a posting's text sent by an agent that no reply could hold, or None when it fits. It is
    never cut: the sender is told its size and sends the posting's own words."""
    size = len((text or "").strip())
    if size <= CEILING_CHARS:
        return None
    return (f"{what} is {size:,} characters, over the {CEILING_CHARS:,} one posting's text may hold "
            "(an agent is sent no more at once): send the posting's own words, not a whole page. It was not taken and "
            "nothing was cut.")


def refusal(text: str | None, what: str) -> str | None:
    """Why a whole text handed in (the owner's browser's capture, a board's own posting) is not kept, or None when it
    is: `why_not`, else `too_long`, as one question. It is what `take(Capture)` asks of a capture; a way in that must
    tell its sender why asks it here, so none asks half of it and none can say a different reason than `take` acted on.
    `what` names the text for the size sentence."""
    return why_not(text) or too_long(text, what)


def of_posting(posting: "JobPosting") -> str:
    """The text a posting holds when it is a posting's text, else "": from a source's own system (`ats.from_source`: a
    job system the app knows, or an employer's own site), not a listing's snippet (`thin`), not a page read whole
    (`page_dump`). A posting read in the owner's browser is never text, opened or not: what a listing showed is a
    snippet, and what the owner's browser captured of the page is kept on the role it was captured for."""
    kept = (posting.text or "").strip()
    return kept if ats.from_source(posting.ats) and not thin(kept) and not page_dump(kept) else ""
