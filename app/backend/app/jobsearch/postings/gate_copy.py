"""Which posting of a role judges its place and its pay (the owner, 2026-11-04, docs/PRD.md 40.1): the employer's main
career
site is the source of truth, and when that does not work, whichever posting is newest.

A role with several postings (one on the employer's job system, one on a board, one on another board) is one job listed
in several places and bands. The hard gates on the place, the way of working and the pay read one posting's cleansed
copy for it, and the choice was the posting with the most text, ties broken by the id of the copy, which is made again
every time the posting is cleansed. A role listed in two places could be stopped one night and released the next
. Now:

1. **The employer's own posting.** The role's Sourced posting (`Opportunity.source_url` when the role is Sourced to the
   employer's own posting), or a posting read from the employer's own job system or its own site and not from a board
   (`ats.from_source`). With several of those, the newest.
2. **Else the newest posting.**

Newest is the day each posting was posted, counted as a role's is (`freshness.posted_basis`, over the one posting: the
employer's date, held to the day the app first saw the posting). Where the day is the same, the posting the employer
dated beats one counted from the day it was seen, then the one with the most text, then the posting's own id, which is
made once: nothing in the choice is redrawn between nights. Only a posting that has been read is a choice: one with no
current cleansed copy, or a copy of under `cleansed.MIN_TEXT` characters (a title and a few header lines, a posting
whose text has not been fetched yet), states nothing the gates could stop a role on, and does not "work" until it has
been read (the newest of a role's postings is often one not read yet, and would release
been read. When no posting of a role has been read, the rule is over all of those that have a copy.

This is the choice of the gates alone (`cleansed.gate_copy_of`, `cleansed.gate_copies_by_role`). The text a reader reads
(the ranker's, the analyst's, the gate-facts step's) is the role's own, else the fullest posting's, unchanged
(`cleansed.copy_of`, `posting_text.held`). Pure: this module reads no database."""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from app.jobsearch import ats
from app.jobsearch.fit import freshness
from app.jobsearch.postings import same_role


@dataclass(frozen=True)
class Choice:
    """One posting of a role that has a current cleansed copy: the posting's id and its copy's, the job system it was
    read from (`JobPosting.ats`), every address it is known by, its own date and the time the app first saw it, and the
    characters of its copy's text."""

    posting_id: uuid.UUID
    cleansed_id: uuid.UUID
    system: str | None
    links: tuple[str, ...]
    posted: date | None
    first_seen: datetime | None
    size: int = 0


def _address(link: str) -> str:
    return link.strip().rstrip("/").lower()


def at_source(links: Iterable[str | None], source: str | None) -> bool:
    """Whether a posting known by `links` is the posting at `source`: the same address, or the same posting under
    another writing of it (the same host and the same id: a title slug, a tracking parameter, a `www.`, a job system's
    language)."""
    held = [link for link in links if link and link.strip()]
    if not source or not source.strip() or not held:
        return False
    mine = _address(source)
    return any(_address(link) == mine for link in held) or bool(same_role.marks([source]) & same_role.marks(held))


def is_employers(choice: Choice, source: str | None) -> bool:
    """Whether the posting is the employer's own: read from a job system the app knows or the employer's own site
    (`ats.from_source`), or the role's Sourced employer posting (`source`: its address)."""
    return ats.from_source(choice.system) or at_source(choice.links, source)


def _newest(choice: Choice) -> tuple:
    """Sort descending by this: the day posted, then the employer's own date over the day the app saw it, then the most
    text, then, so that nothing is left to chance, the lowest posting id."""
    day = freshness.posted_basis(None, None, [(choice.posted, choice.first_seen)])
    return (day.day or date.min, day.basis == freshness.POSTED, choice.size, -choice.posting_id.int)


def pick(choices: Sequence[Choice], source: str | None = None, min_text: int = 0) -> Choice | None:
    """The posting the gates read for a role: the newest of its employer's own postings, else the newest of them all,
    among the postings that have been read (a copy of at least `min_text` characters), or all of them when none has.
    `source` is the address of the role's Sourced employer posting, when it has one (`Opportunity.source_url` with
    `source_kind` employer); None when the role is not Sourced to the employer's own posting. None when no posting has a
    copy."""
    read = [c for c in choices if c.size >= min_text] or list(choices)
    employers = [c for c in read if is_employers(c, source)]
    return max(employers or read, key=_newest, default=None)
