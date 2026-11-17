"""A posting made again from its raw copy, with no fetch (a cleaner version bump rebuilds a night's rows from raw).
`parsed` is the reader's own parse of a raw row (`raw_copies.RawPosting.parsed`): what a reader made of the bytes it
fetched. Here the reader's parse is run again on the bytes, so a better parse and a better cleaner both reach every copy
without a request:

- a list item of a job system (`reader` `adapter:<system>`) is made again by that adapter's `parse_item`, with the
  employer's board address;
- a detail answer of a job system is made again by running the adapter's own `detail` on the stored reply, which is
  handed back to it by address (`Replay`); one job system's detail is a list item, so `parse_item` makes it;
- an item of a site's recipe (`recipe:<kind>:<skill id>`) is made again by that version of the recipe
  (`skills.reparse_item`).

**What is not made again, and starts from the stored parse instead** (`NOT_REPARSED`): the reads that were never a
fetch of bytes to parse: what the owner's browser reported (`browser`) or captured (`browser:page`), a posting page the
app's own fetch kept whole (`code:page`), the text of a posting the app read as plain data with no reply to keep (an
employer's own page, a site recipe's detail: `code:text`), what the app's Chrome read out of a board posting's own
page (`raw_copies.board_page()`, the board's capture: the read-out as JSON, never the page), a role the scout reported
(`scout`), a tracker row (`import`), and the stored text of a posting read before raw copies were kept (`legacy`); and
any other row whose reader can no longer be run: its employer or recipe is gone, its system has no adapter now, or a
replayed answer is not enough for the adapter. Pure of the network: nothing here fetches."""

import json
import uuid
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import adapters
from app.jobsearch.models import Employer, RawPosting
from app.jobsearch.postings import raw_copies
from app.jobsearch.postings.raw_copies import DETAIL, ITEM, inflate, snapshot

_NOT_REPARSED = ("legacy", "browser", "browser:page", "code:page", "code:text", "scout", "import")


def not_reparsed() -> tuple[str, ...]:
    """The readers whose rows are not made again: those above and the board's page read-out (`raw_copies.board_page`,
    none where no board is followed to the employer's site)."""
    board = raw_copies.board_page()
    return (*_NOT_REPARSED, board) if board else _NOT_REPARSED


def __getattr__(name: str) -> tuple[str, ...]:
    if name == "NOT_REPARSED":
        return not_reparsed()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
#: What a detail answer's update may take from a list item (a detail that is one).
FROM_ITEM = ("text", "posted", "location", "work_mode")


class Replay:
    """Stands where the polite fetcher stood for an adapter's `detail`: answers each address with the reply that was
    kept for it, and anything else is an error (the stored answers are not enough)."""

    deadline = None
    requests = 0

    def __init__(self, replies: dict[str, tuple[int, str | None, bytes]]) -> None:
        self.replies = replies

    async def request(self, method: str, url: str, **_kwargs: Any) -> httpx.Response:
        found = self.replies.get(str(httpx.URL(url)))
        if found is None:
            raise LookupError(f"no kept answer for {url}")
        status, content_type, body = found
        return httpx.Response(status, content=body, headers={"content-type": content_type or ""},
                              request=httpx.Request(method, url))

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)


def _item(row: RawPosting, body: bytes) -> Any:
    return json.loads(body) if (row.content_type or "").startswith("application/json") else body.decode("utf-8")


async def _board(session: AsyncSession, row: RawPosting) -> dict | None:
    employer = await session.get(Employer, row.source_id)
    return None if employer is None else dict(employer.board or {})


async def parsed(session: AsyncSession, row: RawPosting, details: list[RawPosting]) -> dict | None:
    """The plain posting the reader makes of this raw row from its bytes, or None when it cannot be made again (the
    caller starts from the row's stored parse). `details` are the posting's detail rows, oldest first, for the replay of
    a detail answer."""
    if row.reader in not_reparsed() or row.kind not in (ITEM, DETAIL):
        return None
    from app.sites import skills  # the recipes read the job systems, which read this module's neighbours

    try:
        body = inflate(row.body)
        company = (row.parsed or {}).get("company")
        if row.reader.startswith("recipe:"):  # a recipe's own read (`site_reads.py`): a skill's or a live recipe's
            from app.sites.models import SiteRecipe, SiteSkill

            owner_of = await session.get(SiteSkill, uuid.UUID(row.reader.split(":", 2)[2]))
            if owner_of is None:
                owner_of = await session.get(SiteRecipe, uuid.UUID(row.reader.split(":", 2)[2]))
            posting = skills.reparse_item(owner_of.recipe, _item(row, body)) if owner_of is not None else None
            return None if posting is None else snapshot(posting, company)
        adapter = adapters.ADAPTERS.get(row.reader.removeprefix("adapter:"))
        board = await _board(session, row)
        if adapter is None or board is None:
            return None
        if row.kind == ITEM:
            posting = adapter.parse_item(_item(row, body), board)
            return None if posting is None else snapshot(posting, company)
        if adapter.detail_is_item:
            posting = adapter.parse_item(_item(row, body), board)
            update = {k: posting.get(k) for k in FROM_ITEM} if posting is not None else {}
        else:
            stored = row.parsed or {}
            replies = {str(httpx.URL(d.url)): (d.http_status or 200, d.content_type, inflate(d.body))
                       for d in details if d.url}
            fetch: Any = Replay(replies)
            update = await adapter.detail(fetch, board, {
                "ext_id": row.ext_id, "url": stored.get("url"), "title": stored.get("title"),
                "location": stored.get("location")})
        made = {**(row.parsed or {}), **{k: v for k, v in update.items() if v is not None}}
        made["text"] = update.get("text")
        return made
    except Exception:  # a reader that cannot be run on this row: its stored parse stands in
        return None
