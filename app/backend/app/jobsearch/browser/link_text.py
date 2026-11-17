"""The text the owner pasted to add a job (docs/PRD.md 24.10): whether it is a link the app can use, and the job's
key and clean address. The page checks it before it asks the engine to read the link and add the job (`add_link.add`,
which is the engine's), so this module imports nothing of the engine: the page answers a text that is not a public web
address with 422 and no ask."""

from urllib.parse import urlsplit

from app.errors import Unprocessable
from app.jobsearch.browser.reader import URL_CHARS, BrowserError, canonical
from app.jobsearch.fetch import polite
from app.sites import avoid, registry
from app.web_address import http_url


class LinkError(Unprocessable):
    """What was pasted is not a link the app can use, said in plain words."""


def _followed_board(host: str) -> str | None:
    """The domain of the board whose roles are followed to the employer's own site (a runbook's `follow_to_employer`)
    that `host` is, or is under; None when it is none."""
    return next((h for r in registry.current().runbooks if r.kind == "board" and r.follow_to_employer
                 for h in r.hosts if avoid.covers(h, host)), None)


def _single_job(link: str) -> str:
    """A followed board's address that has one job open on a page of several (what the address bar shows while the
    owner reads a list) as the job's own page: the analyst is handed a page it reads signed out, and a list is not one
    job. The board's own shape of such a link is its reader's (`registry.board_reader("links")`). Any other address,
    and a board's with no job open, with more than one, or with an id that is not a number, is as it was pasted."""
    host = avoid.host_of(link)
    board = _followed_board(host) if host is not None else None
    links = registry.board_reader("links")
    return link if board is None or links is None else links.single_job_page(link, board)


def read_link(text: object) -> tuple[str, str]:
    """(the job's key, its clean address) for what the owner pasted, or a `LinkError`. Only a public http(s) address is
    kept: the analyst reads it, so an address on the owner's own network, or one with a sign-in in it, is refused."""
    link = http_url(text)
    if link is None:
        raise LinkError("That is not a web address. Paste the link to the job's page, starting with https://.")
    parts = urlsplit(link)
    if parts.username or parts.password or not polite.public_host(parts.hostname or ""):
        raise LinkError("That address is not on the public internet, so the analyst could not read it. "
                        "Paste the link to the job's own page.")
    try:
        return canonical(_single_job(link))
    except BrowserError:
        raise LinkError(f"That link is too long to be a job's page (the most is {URL_CHARS:,} characters).") from None
