"""Is a page the posting of this role? The check on a page the app reads for a role (docs/PRD.md 39.29).

`whole.why_not` says that a page's text is long enough and no wall, never whose it is: a role's kept
text could be an employer's generic careers page (its values and mission). A browser session had handed
back an employer link of the kind that draws a posting in the browser (`/careers/?gh_jid=N`), and the app's own read of
that link kept the text of the page around the posting. So a page read for a role is kept only when the page names the
role, by one of three things:

- **The text**: most of the title's words (`posting_match.TITLE_SHARE`) lie together in it
  (`posting_match.title_together`: not the same words anywhere on a long page).
- **The JobPosting the page embeds**: its title is the role's title (`is_title`).
- **A heading**: the page's `og:title`, an `h1` or its `title` is the role's title (`is_title`, `headings`). This is
  the rule the code-first reader asks of a page too (`browser.code._is_this_job`), and it is the same one.

Many real pages carry a posting's body and not its title (a text-only test turns away many real postings), and the
JobPosting or a heading says whose they are. `is_title` is the title, folded the way titles are compared
(`titles_match`), or the title itself in a heading no more than `HEADING_EXTRA` characters longer: a tail that says
the site or the place ("Title - United States - 44263") is the title's page, a heading that lists many jobs is not,
and the title's words in another job's title ("Senior Analyst, Widgets Lead" for "Senior Widgets Lead") are another
job's. What no title can tell is a page that lists jobs under a heading that is the role's title plus a short tail:
the code-first reader accepts it too.

The same rule says whether an address is this role's posting before it becomes the role's source (`same_job`, asked by
`sourced.accept_source`; a session's link may open another job's closed requisition and be taken
because it is on a known job system). It is asked only of a title that is stated: the JobPosting a page embeds (the
app's read or the session's capture) or the title the job system's own list gave that address. A text that holds no
title, or a heading, says nothing against a link: a page of many jobs and a posting that is drawn in the browser have
both.

Only a page the app read is asked, for a role with a title to check: the text of a job system's own answer and a
session's capture are no page, and a role added by its link holds the link for its title, so its caller passes none. A
title with no word that can be checked (no Latin letter or digit) passes. Pure functions; no I/O."""

from __future__ import annotations

import re

from app.jobsearch import adapters
from app.jobsearch.browser import posting_match
from app.jobsearch.employers import follow as employer_posting
from app.textutil import squash

_HEADINGS = (r"<meta[^>]+property=[\"']og:title[\"'][^>]*content=[\"']([^\"']+)", r"<h1\b[^>]*>(.*?)</h1\s*>",
             r"<title\b[^>]*>(.*?)</title\s*>")
#: The most characters a heading may have beyond the role's title and still be the title's page.
HEADING_EXTRA = 80


def headings(page: str) -> list[str]:
    """What a page's markup calls it: its `og:title`, each `h1` and its `title`, as plain text on one line."""
    found = (m.group(1) for pat in _HEADINGS for m in re.finditer(pat, page, re.I | re.S))
    return [squash(adapters.html_to_text(h)) for h in found]


def is_title(title: str, said: str) -> bool:
    """Whether `said` (a heading, or a JobPosting's title) is the role's title: the same title once folded
    (`titles_match`: "II" is "2", a tag that says where is dropped), or the title itself in it with no more than
    `HEADING_EXTRA` characters besides ("Title - United States - 44263", "Title | Acme Careers"). A heading that holds
    the title among many jobs, or only its words in another order, is not."""
    mine = squash(title).lower()
    return bool(mine and said) and (employer_posting.titles_match(title, said)
                                    or (mine in said.lower() and len(said) <= len(mine) + HEADING_EXTRA))


def same_job(title: str | None, said: str | None) -> bool:
    """Whether `said`, a title stated for a job (a page's JobPosting, a job system's own list), is the role's job: it is
    the role's title (`is_title`), or the role's title holds it with a tag of its own ("Title - (Place) full time" is
    "Title"). Another job's title is not, whatever words they share. True when there is nothing to check: no title, or
    one with no word that can be checked (`posting_match.title_together`), or nothing stated."""
    if not said or posting_match.title_together(title, said) is None:
        return True
    return is_title(title or "", said) or is_title(said, title or "")


def has_title_heading(page: str, title: str | None) -> bool:
    """Whether a heading of the page is the role's title (`is_title`). A role with no title cannot be told, so it is
    not."""
    return bool(title) and any(is_title(title or "", said) for said in headings(page))


def names_the_role(title: str | None, text: str, page: str, posting_title: str | None = None) -> bool:
    """Whether the page is the posting of the role titled `title`: `text` is the text read from it, `page` its markup
    and `posting_title` the title of the JobPosting it embeds, if any."""
    together = posting_match.title_together(title, text)
    if together is None or together >= posting_match.TITLE_SHARE:
        return True  # nothing in the title can be checked, or the text names the role
    return (bool(posting_title) and is_title(title or "", posting_title or "")) or has_title_heading(page, title)
