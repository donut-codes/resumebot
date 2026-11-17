"""Keeping a posting's text, its raw copy and the cleansed copy that follows as one act.

A posting's text and the copies that hold it are written together or the copies lag the text. `hold` is the one place
that does it, in the one order: put the text (when the caller says to), flush, keep the raw copy of what the read
brought, make the cleansed copy that follows, point the posting at it and tell its role (`raw_copies._keep_sent`).

What `hold` does not do is decide. Whether a text replaces the posting's (longer, undated, thin, a snippet's place)
is each caller's own rule and stays at its call: it hands over a `Put` only when it has decided to replace. `hold`
never writes `Opportunity.description` (`postings.text.take` is its only writer), and it only flushes: no commit, no
`db.release`, no I/O, so it is safe inside a process step and under `db.strict_io`.

Left out on purpose, each its own write: `add_link.name_posting` (no copy by design), `repairs` (keeps the copy it
has), `records._fold` (its copy follows the merge delete), `postings.text.let_go`, the bridges (`take_whole`,
`keep_whole_text`, `keep_legacy`) and a write of `detail_at` alone."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.models import JobPosting
from app.jobsearch.postings import raw_copies

#: `Put.detail_at`: leave the posting's date as it is (a snippet filled in, which is not a read of the posting's page).
KEEP: Final = object()
#: `hold`'s `copy_text`: the text the copy holds is the posting's own, after the put (the default).
POSTING: Final = object()


@dataclass(frozen=True, slots=True)
class Put:
    """The text the posting holds after this act. The caller has already decided to replace; `hold` never decides.
    `detail_at` is a time, None (clear it) or `KEEP`; there is no default: the caller says."""

    text: str
    detail_at: datetime | None | object


@dataclass(frozen=True, slots=True)
class Source:
    """What the copy is made from: the reader's word, the kind of row, the content type and what the reader brought.
    Made by the constructors below, a closed set; none is built by hand."""

    reader: str
    kind: str
    content_type: str | None
    payload: Any
    replies: bool = False


def item(raw: Any) -> Source:
    """A listing item the owner's browser reported (a card of a scan), as it came."""
    return Source("browser", raw_copies.ITEM, None, raw)


def page(body: Any, reader: str) -> Source:
    """A posting's page as it came (`browser:page` from the owner's browser, `code:page` from the app's own read)."""
    return Source(reader, raw_copies.DETAIL, "text/html", body)


def answer(more: Any) -> Source:
    """A detail answer that came with the replies (or the raw JSON) it was made from, kept as the bytes the site sent
    (reader `adapter:<ats>`). An answer with nothing to keep writes nothing."""
    return Source("", raw_copies.DETAIL, None, more, replies=True)


def plain(reader: str) -> Source:
    """A text the app read as plain data (`code:text`, `browser:page` for a capture): the copy's text is the raw one."""
    return Source(reader, raw_copies.DETAIL, "text/plain", None)


def reported(raw: Any, reader: str) -> Source:
    """A role reported as it stands (reader `scout` or `import`), kept as it came."""
    return Source(reader, raw_copies.ITEM, None, raw)


def board_page(read: dict) -> Source:
    """What the board's posting page said, as the read-out JSON (the board's capture, `READ_KEYS`)."""
    return Source(raw_copies.board_page(), raw_copies.DETAIL, None, read)


async def hold(session: AsyncSession, posting: JobPosting, source: Source, *, put: Put | None = None,
               copy_text: str | None | object = POSTING, now: datetime) -> int:
    """Put `put` on the posting (when given), flush, keep `source` as the posting's newest raw copy, make the cleansed
    copy that follows, point the posting at it and tell its role. Returns how many raw rows were written (0: the hash
    already held it, or an answer with nothing to keep).

    The copy's text is the posting's text after the put, so a caller cannot pair a text with a different copy by
    accident; `copy_text` says otherwise on purpose (the role's text for a posting that did not take it, the posting's
    own text, an answer's text), and the call says why. Raises `ValueError` for a `Put` with no text, or a `plain`
    source with none. Database errors go to the owner's rollback."""
    if put is not None:
        if not put.text:
            raise ValueError("a text put on a posting has words")
        posting.text = put.text
        if isinstance(put.detail_at, datetime) or put.detail_at is None:
            posting.detail_at = put.detail_at
    await session.flush()  # the posting's UPDATE is sent before the copy is built from it
    text = (posting.text or None) if copy_text is POSTING else cast(str | None, copy_text)
    if source.replies:
        return await raw_copies._keep_detail(session, posting, source.payload, now, text=text)
    payload = source.payload
    if payload is None:  # plain: the copy's text is what was read
        if not text:
            raise ValueError("a plain text has words")
        payload = text.encode("utf-8")
    return await raw_copies._keep_report(session, posting, payload, reader=source.reader, text=text,
                                         kind=source.kind, content_type=source.content_type, now=now)
