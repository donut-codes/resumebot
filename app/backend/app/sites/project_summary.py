"""What a site project says up top, worked out from what the app already holds (no model; the same inputs give the same
words). The few pieces the loop needs; the project page's own views are step 5's.

Written for this app: `purpose_of` and
`finished_words` as the finished project and its resolution say them.
"""

from __future__ import annotations

import re

from app.textutil import squash

FINISHED = "Finished."
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_FINISHED_MAX = 160
#: The check report's own count ("0 of 1 on plan; 1 off plan."), bookkeeping that is never a result.
PLAN_COUNTS = re.compile(r"[0-9]+ of [0-9]+ on plan; [0-9]+ off plan", re.I)


def purpose_of(outcome: str | None) -> str:
    """What the project is for: its outcome, on one line, without the closing full stop."""
    return squash(outcome).rstrip(".").strip()


def finished_words(note: str | None) -> str:
    """What a project that finished says it did: the first whole sentence of its Act note, else "Finished.". The check
    report's counts and a missing note both read "Finished.", never a number."""
    text = squash(note)
    if not text or PLAN_COUNTS.search(text):
        return FINISHED
    first = _SENTENCE.split(text, maxsplit=1)[0]
    if len(first) > _FINISHED_MAX:
        return FINISHED
    out = first.rstrip(". ")
    return f"{out}." if out else FINISHED
