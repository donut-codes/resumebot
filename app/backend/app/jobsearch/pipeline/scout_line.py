"""The scout's one line about a role's score, as the role's history shows it once the posting's text has been read.

The scout writes `fit_evidence` as it scores a role, from the page in front of it. When that page was a sign-in wall or
came back empty, the line says so ("Text missing (sign-in job site); scored from title only."). The app reads the
posting's text later, from the employer's own page, and the line stays on the role, on roles the app
has read and researched whose Scored event still says the text was missing. Read-only: nothing is cleared in the
database, so the line is still what the scout said, and a role whose text is still missing still shows it.

The line is the scout's own prose, so `says_text_missing` knows the shapes its sentences take, not a list of phrases:
"Text missing ...", "Posting text could not be read ...", "No posting body ...", "Only the title ...", "scored from
title only". A sentence of another shape stays on the page, which is the mild way to be wrong; a line the app composes
(the notes after ` | `, `actions.keep`) is never touched. A pure function: no database, no network."""

import re

#: One sentence of the scout's line that says the text of the posting was not there to read. Each alternative is a shape
#: the live lines take (the first is the scout's own "Text missing", the commonest): the text named and not read,
#: "no posting text", "no duties could be read", "could not read the posting", only the title or header, a score "from
#: title" (alone, only, or followed by what else it came from), not fetched, a page that returned only its site shell.
_TEXT_MISSING = re.compile(r"""
    \b(?:text|body)\b[^.;:]{0,40}?\b(?:missing|unreadable|unavailable
        |not\s+(?:be\s+)?(?:read|readable|fetched|available|loaded|found|returned|reach(?:ed|able)|retriev(?:ed|able))
        |could\s+not\s+be\s+read|did\s+not\s+(?:load|return)|came\s+back\s+empty)
  | \b(?:no|without)\s+(?:the\s+)?(?:job\s+|posting\s+|page\s+)?(?:text|body)\b
  | \bno\s+(?:job\s+)?description\b(?!\s+of)
  | \bno\s+duties\b[^.;]{0,25}\b(?:read|readable)\b
  | \bcould\s+not\s+read\s+the\s+posting\b
  | \b(?:only|just)\s+(?:a\s+|the\s+|one\s+)?(?:page\s+)?(?:title|header)\b
  | \btitle\s+(?:only|alone)\b
  | \b(?:scored|judged|assessed|answered|answers?)\b[^.;]{0,40}\b(?:from|on)\s+(?:the\s+|its\s+)?title
        (?=\s*(?:,|\.|;|$|only\b|alone\b|and\b))
  | \bnot\s+fetched\b
  | \breturned\s+only\s+(?:the\s+)?(?:site|careers)\b
""", re.I | re.X)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
#: What `actions.keep` puts between the scout's line and the notes the app adds to it.
_NOTES = " | "


def says_text_missing(sentence: str) -> bool:
    """Whether one sentence of a scout's line says the posting's text was missing, unreadable or only a title."""
    return _TEXT_MISSING.search(sentence) is not None


def shown(evidence: str | None, text_read: bool) -> str | None:
    """The line to show beside a role's Scored event: `evidence` as stored while the text is still missing, and, once
    `text_read`, without the sentences of the scout's own line that say it was (what the app added after ` | ` stays).
    None when nothing is left. A line with no such sentence comes back as it was, character for character."""
    if not evidence or not text_read:
        return evidence
    said, bar, notes = evidence.partition(_NOTES)
    sentences = [s for s in _SENTENCE.split(said) if s]
    kept = [s for s in sentences if not says_text_missing(s)]
    if len(kept) == len(sentences):
        return evidence
    return _NOTES.join(p for p in (" ".join(kept), notes if bar else "") if p) or None
