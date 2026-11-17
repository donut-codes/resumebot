"""The resolution pass (docs/PRD.md 28.2): which job system each employer posts on. First from the links the app already
holds (its roles' postings and apply pages, its careers page), with no network; then, for employers still unknown, by
asking the systems with open feeds whether a board under the employer's name exists, accepted only when the answer
names the same employer, or, where a system never says a name, marked as unconfirmed.

What the owner set by hand is never changed. A gated employer is never asked about. A board is one employer's own: its
tenant, id, slug or host on the job system, never the system's root host (`ats.own`), and never one that two employers
hold, which the one whose name it carries keeps and the other looks for again (`from_links`). A board the open feeds
guessed from the name (`probe`) is no word of the employer's, and gives way to the board its links name, unless it is
known to be real (`_guesses_that_stand`)."""

import asyncio
import logging
import re
import uuid
from collections import Counter
from collections.abc import Iterable
from urllib.parse import urljoin

import httpx
from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import adapters, ats, off_turn
from app.jobsearch.employers.core import ask_scout_again, key_of, learn_careers_url
from app.jobsearch.fetch.polite import NOT_NOW, Fetcher, NotPublic, Refused, RobotsRefused
from app.jobsearch.models import Employer, JobPosting, Opportunity
from app.jobsearch.postings.coop import Cooperate
from app.jobsearch.states import FetchKind
from app.sites import registry, where

log = logging.getLogger(__name__)


def _settable(employer: Employer) -> bool:
    return employer.ats_found_by not in ("owner",) and "ats" not in (employer.owner_fields or [])


#: Keys a board keeps beside its address: the boards found gone, and the tenant a read learned.
NOT_ADDRESS = ("gone", "api_tenant")


def address(board: dict | None) -> dict:
    return {k: v for k, v in (board or {}).items() if k not in NOT_ADDRESS}


def _set(employer: Employer, system: str, board: dict, by: str) -> None:
    gone = _gone(employer)
    employer.ats, employer.ats_found_by = system, by
    employer.board = {**address(board), **({"gone": gone} if gone else {})}
    employer.board_url = ats.board_url(system, employer.board)
    learn_careers_url_from_board(employer)


def take(employer: Employer, system: str, board: dict, by: str) -> None:
    """Give the employer the job system a link named (the researcher's word for it, checked by the caller: the board is
    no other employer's). The caller has seen it is the app's to set (`_settable`)."""
    _set(employer, system, board, by)


#: What an employer's `sources` say of a careers page the app took from its job system's board.
BOARD_SOURCE = "its job system's board"


def learn_careers_url_from_board(employer: Employer) -> bool:
    """An employer on a job system whose board address is whole has its careers site: the board's own page, kept as the
    employer's careers page when it has none (and the owner set none)."""
    if not (employer.ats and employer.board_url and ats.complete(employer.ats, address(employer.board))):
        return False
    return learn_careers_url(employer, employer.board_url, BOARD_SOURCE)


def _gone(employer: Employer) -> list:
    """Boards this employer had that turned out to be gone, so a link to them isn't taken again."""
    return list((employer.board or {}).get("gone") or [])


def best(seen: list[ats.Board], gone: list | None = None) -> tuple[str, dict] | None:
    """The board the links point at most, a whole address before a partial one, never one known to be gone."""
    gone = gone or []
    seen = [d for d in seen if {"ats": d.system, "board": d.parts} not in gone]
    if not seen:
        return None
    tally = Counter((d.system, tuple(sorted(d.parts.items()))) for d in seen)
    (system, board), _n = max(tally.items(), key=lambda kv: (ats.complete(kv[0][0], dict(kv[0][1])), kv[1]))
    return system, dict(board)


def is_gone(employer: Employer, found: ats.Board) -> bool:
    """Whether this board is one the employer had that turned out to be gone (`forget`), so it is not taken again."""
    return best([found], _gone(employer)) is None


def forget(employer: Employer, why: str) -> None:
    """A board that is gone: the employer's system is looked for again, and this board is never taken again. If the
    scout found the board, the scout is asked again too (`ask_scout_again`): its answer was for a board that is not
    there."""
    if employer.ats_found_by == "owner" or "ats" in (employer.owner_fields or []) or not employer.ats:
        return
    gone = [*_gone(employer), {"ats": employer.ats, "board": address(employer.board)}]
    if employer.careers_url == employer.board_url and "careers_url" not in (employer.owner_fields or []):
        employer.careers_url = None  # the careers page the app took from this board goes with it
    employer.ats, employer.board, employer.board_url, employer.ats_found_by = None, {"gone": gone[-5:]}, None, None
    employer.fetch_note, employer.fetch_kind = why, None
    ask_scout_again(employer)


def _board_of(link: str | None, known: dict | None = None) -> ats.Board | None:
    """The employer's board a link names, or None. `known` is a call's own link -> board table (`from_links`), looked up
    before the link is parsed."""
    if known is not None and link in known:
        return known[link]
    found = ats.parse(link)
    return found.board if found else None


#: How many links or employers a loop works through before it gives the event loop back.
_COOP_EVERY = 250


async def _coop(done: int) -> None:
    """Gives the event loop back every `_COOP_EVERY` items of a loop that does no I/O of its own."""
    if done and done % _COOP_EVERY == 0:
        await asyncio.sleep(0)


def _compact(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def spoken(label: str) -> str:
    """A name on a board's address read as an employer's key is (`key_of`: "&", "and" and the legal words out), run
    together: `Acme_and_Co` is `acme`, as is "Acme & Co"."""
    return _compact(key_of(re.sub(r"[^a-z0-9]+", " ", label.lower())))


def owns(employer: Employer, system: str, board: dict) -> bool:
    """Whether a board carries the employer's name: its slug, tenant, site, company or customer host
    (`ats.board_labels`) is its name, or its name's, or one of its aliases', both read the same way (`spoken`). What
    settles whose a board is when two employers claim it."""
    keys = {_compact(employer.key), *(_compact(key_of(a)) for a in employer.aliases or [])}
    names = [n for n in (spoken(label) for label in ats.board_labels(system, board)) if len(n) >= 3]
    return any(len(k) >= 3 and (n == k or n in k or k in n) for k in keys for n in names)


def board_key(system: str, board: dict) -> tuple | None:
    """What two employers share when they hold one board: its system and address, once the address is one customer's own
    (`ats.own`: never a vendor's root host, nor an address with only its host). None for any other address."""
    parts = {k: v for k, v in (board or {}).items() if k not in NOT_ADDRESS and k != "confirmed"}
    return (system, tuple(sorted(parts.items()))) if ats.own(system, parts) else None


def losers(claims: dict[tuple, list[Employer]]) -> set[uuid.UUID]:
    """The employers that may not hold a board others claim, by the board's key (`board_key`): of those claiming it, the
    ones the owner set it for keep it, else the ones whose name it carries (`owns`); every other claimant loses it, and
    where nobody's name is on it nobody keeps it (two records of one employer both carry its name, and both keep it)."""
    out: set[uuid.UUID] = set()
    for (system, parts), claimants in claims.items():
        if len(claimants) < 2:
            continue
        keep = [e for e in claimants if not _settable(e)] or [e for e in claimants if owns(e, system, dict(parts))]
        out |= {e.id for e in claimants if e not in keep}
    return out


async def held_boards(session: AsyncSession, owner: str) -> dict[tuple, list[Employer]]:
    """The employers that hold each own board (`board_key`), to ask whether a board a page gives is taken (`taken`)."""
    held: dict[tuple, list[Employer]] = {}
    employers = await session.scalars(select(Employer).where(Employer.owner == owner, Employer.ats.is_not(None)))
    for e in employers.all():
        key = board_key(e.ats or "", e.board or {})
        if key is not None:
            held.setdefault(key, []).append(e)
    return held


def taken(held: dict[tuple, list[Employer]], employer: Employer, system: str, board: dict) -> bool:
    """Whether a board a page gives the employer is another employer's: another holds it (`held_boards`) and it does not
    carry this employer's name (`owns`). A page that points at a board is the employer's word for it only when nobody
    else has it, as `from_links` settles two claims."""
    key = board_key(system, board)
    return key is not None and any(o.id != employer.id for o in held.get(key, [])) and not owns(employer, system, board)


def clear(employer: Employer, why: str) -> None:
    """A board that is not the employer's: it is taken from it, with the careers page the app took from that board, and
    the employer's system is looked for again. (Unlike `forget`, the board is not marked gone: it is somebody's.)"""
    _drop_careers_copy(employer)
    gone = _gone(employer)
    employer.ats, employer.board_url, employer.ats_found_by = None, None, None
    employer.board = {"gone": gone} if gone else {}
    employer.fetch_note, employer.fetch_kind = why, None


def _circular(employer: Employer) -> bool:
    """Whether the employer's careers page is the app's own copy of the board it holds, which says nothing new of it."""
    return bool(employer.careers_url) and employer.careers_url == employer.board_url \
        and "careers_url" not in (employer.owner_fields or [])


def _drop_careers_copy(employer: Employer) -> None:
    """The careers page the app took from the board the employer holds goes with it (one the owner wrote stays)."""
    if _circular(employer):
        employer.careers_url = None
        employer.sources = {k: v for k, v in (employer.sources or {}).items() if k != "careers_url"}


def _same_board(employer: Employer, system: str, board: dict) -> bool:
    """Whether the board is the one the employer holds, whatever else its record says of it (`confirmed`, `gone`)."""
    held = {k: v for k, v in address(employer.board).items() if k != "confirmed"}
    return employer.ats == system and held == board


def _let_go_of_guess(employer: Employer) -> None:
    """The employer is to have the board its links name instead of the one the open feeds guessed: the guess's careers
    page goes with it, and so does the read of it (a board is taken or left as one piece, `records._BOARD`)."""
    _drop_careers_copy(employer)
    employer.fetched_at = employer.fetch_ok = employer.fetch_kind = employer.fetch_note = employer.open_roles = None


def _empty_accounts() -> set[str]:
    """The job systems that keep accounts listing no posting, so that a board guessed on one says nothing of whether the
    employer posts there until it has held a posting: the runbooks whose `probe` does not prove a board
    (`probe.proves_board`). Every other open feed answers only for a board that exists (one that lists a posting, or one
    that carries the employer's name), so a board taken from one is real."""
    return {r.key for r in registry.current().runbooks if r.probe is not None and not r.probe.proves_board}


async def _held_postings(session: AsyncSession, owner: str, employers: Iterable[Employer]) -> set[uuid.UUID]:
    """Of these employers, the ones whose board has held a posting: one its own system's reader listed, open or closed
    now. A posting the employer's own site, the owner's browser or the scout gave is not the board's."""
    ids = [e.id for e in employers]
    if not ids:
        return set()
    return set((await session.scalars(
        select(JobPosting.employer_id).join(Employer, Employer.id == JobPosting.employer_id)
        .where(JobPosting.owner == owner, JobPosting.employer_id.in_(ids), JobPosting.ats == Employer.ats)
        .distinct())).all())


async def _guesses_that_stand(session: AsyncSession, owner: str, guesses: list[Employer]) -> set[uuid.UUID]:
    """Of the employers holding a board the open feeds guessed, the ones whose guess stands against what their links
    say: it has held a posting (`_held_postings`), or its system is not one that keeps empty accounts
    (`_empty_accounts`) and no read of it has found it empty. A guess not read yet is real, as its feed answered only
    for a board that exists: the employer's site was never switched on."""
    return _stand_among(await _held_postings(session, owner, guesses), guesses)


def _stand_among(held: set[uuid.UUID], guesses: list[Employer]) -> set[uuid.UUID]:
    """`_guesses_that_stand`, from the employers that held a posting already read."""
    empty = _empty_accounts()
    return {e.id for e in guesses if e.id in held or (e.ats not in empty and e.fetch_ok is not True)}


async def _held_by_guesses(session: AsyncSession, owner: str) -> set[uuid.UUID]:
    """`_held_postings` for every employer of the owner that holds a board the open feeds guessed, in slices and in
    one statement that names no id list (read apart from the writer's turn by `from_links`)."""
    rows = await off_turn.rows_in_slices(session, (
        select(JobPosting.employer_id).join(Employer, Employer.id == JobPosting.employer_id)
        .where(JobPosting.owner == owner, Employer.owner == owner, Employer.ats_found_by == "probe",
               Employer.ats.is_not(None), JobPosting.ats == Employer.ats).distinct()))
    return {r[0] for r in rows}


async def flush_changes(session: AsyncSession, employers: Iterable[Employer]) -> list[uuid.UUID]:
    """Write the changes made to `employers`, except to a record that is gone: one a merge has deleted since this pass
    loaded it (an update that matches no row fails the whole flush: `StaleDataError`). The change to such a record is
    dropped, and logged; the next pass works its change out again from what is there then. The others are written at
    once. The database is SQLite with one writer, so no record is held by another transaction to be waited for or
    skipped, and the check and the flush are one transaction's: nothing can merge a record away between them. It is a
    read of which records are there and no claim (`work/claim.py` is for a row taken by changing a column of it; a
    pass's own changes are made through the session). Returns the ids left out."""
    changed = {e.id: e for e in employers if session.is_modified(e)}
    left: list[uuid.UUID] = []
    if changed:
        free = set((await session.scalars(
            select(Employer.id).where(Employer.id.in_(changed))
            .execution_options(autoflush=False))).all())
        for employer_id, employer in changed.items():
            if employer_id not in free:
                session.expunge(employer)  # its change goes with it
                left.append(employer_id)
    if left:
        log.warning("%d employer record%s merged away or held by another transaction, so %s left for the next pass: %s",
                    len(left), " was" if len(left) == 1 else "s were", "its change was" if len(left) == 1
                    else "their changes were", ", ".join(str(i) for i in left))
    await session.flush()
    return left


async def from_links(session: AsyncSession, owner: str) -> dict:
    """Each employer's system from the links the app holds: the most common system among them wins. A board is one
    employer's: when two employers claim the same own address (`board_key`), the one it is the owner's word for, else
    the one whose name it carries, keeps it (`losers`), the other is denied it, or has it taken if it held it, and with
    nobody's name on it neither has it and both look again. A careers page that is the app's own copy of a board is not
    a link to it (`_circular`). A board the open feeds guessed (`probe`) is replaced by the one the links name unless it
    stands (`_guesses_that_stand`), and the site the app made for the employer is aimed at the new board
    (`where.reaim_sites`). A record another transaction has merged away or holds is left for the next pass
    (`flush_changes`). Returns how many employers each system now has, and how many are still unknown."""
    # The roles' links are read in a reader's session (no writer's turn) and parsed in slices, before the employers are
    # loaded in `session`: the parse of thousands of addresses is no part of a writer's turn.
    # The held-postings join (`_held_postings`) is read there too, for every employer holding a guess: a superset of
    # the employers loaded below, whichever of them a writer has changed in between.
    async with off_turn.reading(session) as reader:
        link_rows = [tuple(r) for r in await off_turn.rows_in_slices(
            reader, select(Opportunity.employer_id, Opportunity.url, Opportunity.apply_url).where(
                Opportunity.owner == owner, Opportunity.employer_id.is_not(None)))]
    async with off_turn.reading(session) as reader:  # a read of its own: the link read's turn names no employers
        held = await _held_by_guesses(reader, owner)
    from_roles: dict = {}
    boards: dict[str | None, ats.Board | None] = {}
    coop = Cooperate()
    for n, (employer_id, url, apply_url) in enumerate(link_rows, 1):
        from_roles.setdefault(employer_id, []).extend([apply_url, url])
        for link in (apply_url, url):
            if link not in boards:
                boards[link] = _board_of(link)
        await _coop(n)
        await coop()
    employers = await off_turn.scalars_in_slices(
        session, select(Employer).where(Employer.owner == owner).execution_options(populate_existing=True))
    links: dict = {e.id: [None if _circular(e) else e.careers_url, e.website, *from_roles.get(e.id, [])]
                   for e in employers}
    picked: dict[uuid.UUID, tuple[str, dict]] = {}
    claims: dict[tuple, list[Employer]] = {}
    stands = _stand_among(held, [e for e in employers if e.ats_found_by == "probe" and e.ats])
    coop = Cooperate()
    for n, e in enumerate(employers, 1):
        await _coop(n)
        await coop()
        if _settable(e) and (e.ats_found_by != "probe" or e.id not in stands):
            seen = [d for d in (_board_of(u, boards) for u in links.get(e.id, [])) if d]
            pick = best(seen, _gone(e))
            keep = e.ats == (pick or ("",))[0] and ats.complete(e.ats, address(e.board))
            # unchanged is kept as it is, and what a read learned (a system's real tenant) with it
            if pick is not None and not (keep and not ats.complete(*pick)) and (e.ats, address(e.board)) != pick:
                picked[e.id] = pick
        held = picked.get(e.id) or ((e.ats, address(e.board)) if e.ats else None)
        key = board_key(*held) if held else None
        if key is not None:
            claims.setdefault(key, []).append(e)
    denied = losers(claims)
    found: list[uuid.UUID] = []
    cleared: list[uuid.UUID] = []
    let_go: list[tuple[Employer, set[str]]] = []  # each employer that dropped a guess, with the guess's addresses
    for e in employers:
        if e.id in denied:
            if e.id not in picked and _settable(e):  # it holds a board that is not its own
                clear(e, "its board was another employer's too, so its job system will be looked for again")
                cleared.append(e.id)
            continue
        if e.id in picked:
            if e.ats_found_by == "probe" and not _same_board(e, *picked[e.id]):
                let_go.append((e, {a.strip() for a in (e.board_url, e.careers_url) if a}))
                _let_go_of_guess(e)
            _set(e, picked[e.id][0], picked[e.id][1], "link")
            found.append(e.id)
    for e in employers:  # an employer whose system was found before its careers page was kept from it
        learn_careers_url_from_board(e)
    left = set(await flush_changes(session, employers))
    await where.reaim_sites(session, owner, [(e, guess) for e, guess in let_go if e.id not in left])
    return {"found": len(set(found) - left), "cleared": len(set(cleared) - left), **await coverage(session, owner)}


# --- reading careers pages ------------------------------------------------------------------------------------------

_LINK = re.compile(r"""(?:href|src|data-src|action)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_URL = re.compile(r"""https?://[^\s"'<>)]+""")


def links_in(html: str, base: str) -> list[str]:
    """Every link and embedded frame a page names, made whole against its address, plus bare addresses in its
    scripts (careers pages often load their job system's widget from one)."""
    found = [urljoin(base, m) for m in _LINK.findall(html or "")]
    found += _URL.findall(html or "")
    return list(dict.fromkeys(found))


#: What an employer's note begins with when its careers page could not be read, however it failed.
COULDNT_READ = "careers page: couldn't be read"
#: Who refused the server a careers page (`from_pages` returns it): robots.txt asks not to read it, or the site turned
#: the server away (a 403).
BY_ROBOTS, BY_THE_SITE = "robots", "site"


def page_settled(employer: Employer) -> bool:
    """Whether the employer's careers page needs no second look: it was read and names no job system the app knows, or
    the site refused the server (it is not asked again; a site handed to the browser leaves the night's reads for good).
    A page that could not be read for a reason that may pass (the site did not answer, or said "not now") is tried again
    the next night."""
    if employer.fetch_kind == FetchKind.REFUSED:
        return True
    return employer.fetch_kind == FetchKind.CAREERS_PAGE and not (employer.fetch_note or "").startswith(COULDNT_READ)


async def from_pages(session: AsyncSession, owner: str, fetch: Fetcher, limit: int | None = None,
                     only: set | None = None) -> dict:
    """For employers still unknown that have a careers page, read the page (plain HTTP) and take the job system its
    links or frames point at. A page that names no system is noted and not read again; one that could not be read
    because the site did not answer is read again the next night (`page_settled`). One the site refused the server
    (robots.txt, or a 403) is noted as refused (`FetchKind.REFUSED`) and not asked again: `refused` says which, by
    employer id, so the caller can hand its site to the owner's browser (docs/PRD.md 30.3). A board another employer
    holds is not taken from a page unless it carries this employer's name (`taken`). `only` (employer ids) limits it to
    the employers whose site the owner switched on."""
    rows = (await session.scalars(select(Employer).where(
        Employer.owner == owner, Employer.ats.is_(None), Employer.gated.is_(False),
        Employer.careers_url.is_not(None)).order_by(Employer.name).execution_options(populate_existing=True))).all()
    rows = [e for e in rows if _settable(e) and not page_settled(e) and (only is None or e.id in only)]
    rows = rows[:limit] if limit else rows
    found: dict[uuid.UUID, str] = {}
    refused: dict[uuid.UUID, str] = {}
    held = await held_boards(session, owner)
    # The pages are read with no transaction open: the loop touches the database no more until `flush_changes`, which
    # writes each record as it is then.
    await db.release_if_free(session)
    for employer in rows:
        try:
            reply = await fetch.get(employer.careers_url)
        except NotPublic as exc:
            _not_read(employer, str(exc))
            continue
        except RobotsRefused as exc:
            if exc.unreachable:  # the site's rules could not be had: it did not answer, which is not a no
                _not_read(employer, str(exc))
            else:
                _refused(employer, str(exc))
                refused[employer.id] = BY_ROBOTS
            continue
        except Refused as exc:
            _refused(employer, str(exc))
            refused[employer.id] = BY_THE_SITE
            continue
        except httpx.HTTPError as exc:
            _not_read(employer, str(exc))
            continue
        if reply.status_code >= 500 or reply.status_code in NOT_NOW:
            _not_read(employer, f"answered {reply.status_code}")
            continue
        seen = [d for d in (_board_of(u) for u in [str(reply.url), *links_in(reply.text, str(reply.url))]) if d]
        pick = best(seen, _gone(employer))
        if pick is None:
            employer.fetch_note = "careers page: names no job system the app knows"
            employer.fetch_kind = FetchKind.CAREERS_PAGE
            continue
        if taken(held, employer, *pick):
            employer.fetch_note = "careers page: points at a job system board that is another employer's"
            employer.fetch_kind = FetchKind.CAREERS_PAGE
            continue
        _set(employer, pick[0], pick[1], "link")
        key = board_key(*pick)
        if key is not None:
            held.setdefault(key, []).append(employer)
        if employer.fetch_kind == FetchKind.CAREERS_PAGE:  # the page opened this time: the note of the night it did not
            employer.fetch_note = employer.fetch_kind = None
        found[employer.id] = pick[0]
    left = await flush_changes(session, rows)  # a record merged away while its page was read is not written
    return {"read": len(rows), "found": dict(Counter(s for i, s in found.items() if i not in left)),
            "refused": {i: why for i, why in refused.items() if i not in left}}


def _not_read(employer: Employer, why: str) -> None:
    """The careers page could not be read tonight: say so on the employer; the next night reads it again."""
    employer.fetch_note = f"{COULDNT_READ} ({why[:120]})"
    employer.fetch_kind = FetchKind.CAREERS_PAGE


def _refused(employer: Employer, why: str) -> None:
    """The site refused the server its careers page: say so on the employer, which is not asked again."""
    employer.fetch_note = f"{COULDNT_READ} ({why[:120]})"
    employer.fetch_kind = FetchKind.REFUSED


async def complete_boards(session: AsyncSession, owner: str, fetch: Fetcher, limit: int = 20,
                          only: set | None = None) -> dict:
    """For employers whose board address is partial (a link to one job, not to the board), read one of their job pages
    or their careers page and take the whole address its links give (a board's id, a site's name). Only for a
    system the app has a reader for: a whole address is of no use to one it has none for, and its careers page would be
    read every night for nothing, using up the night's `limit`. A whole address another employer holds is not taken
    unless it carries this employer's name (`taken`). `only` (employer ids) limits it to the employers whose site the
    owner switched on."""
    rows = [e for e in (await session.scalars(select(Employer).where(
        Employer.owner == owner, Employer.ats.is_not(None), Employer.gated.is_(False)
    ).execution_options(populate_existing=True))).all()
        if e.ats in adapters.ADAPTERS and not ats.complete(e.ats, e.board) and _settable(e)
        and (only is None or e.id in only)][:limit]
    links: dict = {}
    for employer_id, url, apply_url in (await session.execute(select(
            Opportunity.employer_id, Opportunity.url, Opportunity.apply_url).where(
            Opportunity.employer_id.in_([e.id for e in rows] or [None])))).all():
        links.setdefault(employer_id, []).extend(u for u in (apply_url, url) if u)
    completed: set[uuid.UUID] = set()
    held = await held_boards(session, owner)
    await db.release_if_free(session)  # the pages are read with no transaction open, as in `from_pages`
    for employer in rows:
        # Its own job pages on its system first (a link to a job board elsewhere says nothing), then its careers page.
        own = [u for u in links.get(employer.id, []) if (b := _board_of(u)) and b.system == employer.ats]
        for page in [*dict.fromkeys(own[:2]), employer.careers_url]:
            if not page:
                continue
            try:
                reply = await fetch.get(page)
            except (Refused, httpx.HTTPError):
                continue
            seen = [d for d in (_board_of(u) for u in [str(reply.url), *links_in(reply.text, str(reply.url))])
                    if d and d.system == employer.ats and ats.complete(*d)]
            pick = best(seen, _gone(employer))
            if pick is not None and not taken(held, employer, *pick):
                _set(employer, pick[0], pick[1], employer.ats_found_by or "link")
                key = board_key(*pick)
                if key is not None:
                    held.setdefault(key, []).append(employer)
                completed.add(employer.id)
                break
    left = await flush_changes(session, rows)  # a record merged away while its pages were read is not written
    return {"partial": len(rows), "completed": len(completed - set(left))}


# --- asking the open feeds ------------------------------------------------------------------------------------------


def slugs(name: str) -> list[str]:
    """The board names an employer is likely to use: its name run together, then hyphenated."""
    key = key_of(name)
    out = [key.replace(" ", ""), key.replace(" ", "-")]
    return list(dict.fromkeys(s for s in out if len(s) >= 3))


def _same(a: str | None, employer: Employer) -> bool:
    keys = {employer.key, *(key_of(x) for x in employer.aliases or [])}
    return bool(a) and key_of(a) in keys


async def probe_one(fetch: Fetcher, employer: Employer) -> tuple[str, dict] | None:
    """The open-feed board under this employer's name, or None. The systems whose feed names the employer are asked
    first, each in its place in `adapters.ADAPTERS`, and an answer is taken only when it names this employer; then the
    two whose feed never names one, whose answer stands, marked unconfirmed."""
    for named in (True, False):
        asked = [a for a in adapters.ADAPTERS.values() if a.probe_names is named]
        for slug in slugs(employer.name):
            answers = await asyncio.gather(*(a.probe(fetch, slug) for a in asked))
            for a, found in zip(asked, answers, strict=True):
                if found is not None and (not named or _same(found.name, employer)):
                    return a.key, {"slug": slug, **({} if named else {"confirmed": False})}
    return None


async def probe(session: AsyncSession, owner: str, fetch: Fetcher, limit: int | None = None,
                only: set | None = None) -> dict:
    """Ask the open feeds about every employer still unknown (not gated, not set by the owner). `only` (employer ids)
    limits it to those employers (the catch-up after an import asks about the ones it brought in). Returns what was
    found."""
    rows = (await session.scalars(select(Employer).where(
        Employer.owner == owner, Employer.ats.is_(None), Employer.gated.is_(False)).order_by(Employer.name)
        .execution_options(populate_existing=True))).all()
    rows = [e for e in rows if _settable(e) and (only is None or e.id in only)]
    rows = rows[:limit] if limit else rows
    found: Counter = Counter()
    for employer in rows:
        await db.release_if_free(session)  # each employer is written in its own transaction, none held while asking
        hit = await probe_one(fetch, employer)
        if hit:
            _set(employer, hit[0], hit[1], "probe")
            if not await flush_changes(session, [employer]):  # not one merged away while the feeds were asked
                found[hit[0]] += 1
    return {"asked": len(rows), "found": dict(found), "requests": fetch.requests,
            "refused_hosts": sorted(fetch.refused_hosts), **await coverage(session, owner)}


async def coverage(session: AsyncSession, owner: str) -> dict:
    """How many employers are on each system, and how many of them the app can read by feed, by pages, or not yet."""
    grouped = (await session.execute(select(Employer.ats, func.count()).where(Employer.owner == owner).group_by(
        Employer.ats).order_by(func.min(literal_column(f"{Employer.__tablename__}.rowid"))))).all()
    by: Counter = Counter()
    for k, n in grouped:  # '' and NULL are both "unknown"; first appearance is kept for ties
        by[k or "unknown"] += n
    total = sum(by.values())
    systems = [{"ats": k, "label": ats.label(None if k == "unknown" else k), "how": ats.how(k), "employers": n}
               for k, n in by.most_common()]
    how = Counter()
    for s in systems:
        how[s["how"]] += s["employers"]
    return {"employers": total, "systems": systems,
            "by_how": {k: how.get(k, 0) for k in ("feed", "pages", "hard", "unknown")}}
