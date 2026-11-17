"""The Haiku ranker, First Impression: every role that has a cleansed copy is scored
from 0 to 100 against the owner's short profile, about 25 roles to one Haiku session, with two lines of why
(`models.RoleRank`). The rank orders First Impression and decides which roles go on deck for the line-by-line read
(`pipeline/deck.py`). Nothing here closes a role: a role is never gated on a score.

**The short profile** is made by code from what the owner already saved, never written by hand and never holding
anyone's preferences in code: the positioning and kind of employer, each target role (its name, example titles and
its words), and every gate that is on, each as one plain sentence. A gate is a hard no, level and field exclusions
among them (the owner's gate rules, the work location and the pay): a posting that clearly hits one scores 0 and says
so, and one that does not clearly hit it passes the gate. It is stored as a versioned text (`ProfileVersion`, kind
`rank_profile`): a changed profile is the next version, an unchanged one is not saved again, and a role is ranked again
under a new version.

**The jobs** are parse jobs of the kind `rank` (`jobs/parse.py`), taken by the pool's parser sessions: `queue` makes
one waiting job for each batch of roles with a current copy and no rank at the version of
the profile in force, behind anything the owner waits on. The pool makes them itself, one batch each time a free session
finds nothing else queued (`pool.pull.start_next`, its rank step, at any hour), so every new role is ranked and,
on the first pulls, every role already there. Its pull decides the batch in a reader's session (`plan`) and holds the
writer's turn only to add it (`add`). A job's text is the profile and the cleansed postings; the
cleansed copy is what is read (`cleansed.py`, the one way to read it), never a raw row.

**A posting is sent whole** (the owner, 2026-10-14: an agent is never cut to fit a small model's budget). What one
session is sent is limited by making the batch smaller, never the posting: a batch holds up to `RANK_BATCH` roles and
`BATCH_CHARS` of postings (`batches`), fewer roles when their texts are long. A posting too big to go in a batch alone
is not queued: it is refused in the log with its size, and never cut (`queue`, `ask_first`).

**A rank made on a cut text is made again.** A rank is kept for one role, one cleansed copy and one version of the
profile. The ranks made before the whole text was sent are marked `RoleRank.text_cut` (migration 0144, the copies over
4,000 characters): `unranked` counts one as no rank, so the pool's ordinary ranking makes it again, and `keep` puts the
new rank in the old one's row, so the role has a rank all the while.

Services flush; the route or command commits."""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import off_turn
from app.jobsearch.measures.pay_place import mostly_below_floor
from app.jobsearch.models import CleansedPosting, Opportunity, ParseJob, ProfileVersion, RoleRank
from app.jobsearch.postings import cleansed
from app.jobsearch.profile import gates as profile_gates
from app.jobsearch.profile import schema
from app.jobsearch.profile import store as profile_store
from app.jobsearch.resume import cleansed_master
from app.jobsearch.stages import PRE_READY
from app.jobsearch.states import ParseKind, ParseState
from app.jobsearch.work import queue as todo_queue
from app.planengine.budget import REPLY_CEILING_CHARS, payload_chars

log = logging.getLogger(__name__)

#: How many roles one session ranks at most: the profile and the session's start-up are paid once for all of them.
RANK_BATCH = 25
#: The most posting text one session is sent in a batch, in characters as the reply carries them (`payload_chars`): the
#: size a batch
#: of 25 ordinary postings comes to. A batch of
#: long postings holds fewer roles; a posting is never cut to fit.
BATCH_CHARS = 100_000
#: What the reply that hands a job out keeps for its instructions, its answer shape and its envelope. The profile and
#: the postings take the rest of `REPLY_CEILING_CHARS`, the most the door sends an agent (it refuses a reply over it).
REPLY_RESERVE_CHARS = 20_000
PROFILE_KIND = "rank_profile"
#: The longest a line of why may be, and how many lines.
WHY_CHARS, WHY_LINES = 300, 2
#: What the line of why that names a gate starts with: a posting that clearly hits a gate scores 0 and says so.
GATE_MARK = "Gate:"

HOW = (
    "The text is a candidate profile and then job postings, numbered from 1. Score each posting from 0 to 100 for how "
    "well it fits the profile, judging only from the posting's own text and the profile; you are given no outcomes and "
    "no history. Scale: 90 to 100 squarely one of the target roles at a level the candidate wants; 70 to 89 strong; "
    "50 to 69 plausible with a stretch; 30 to 49 weak; 0 to 29 not a fit. Use the whole range: most postings in a "
    "typical pile score below 40. A posting that clearly hits a gate of the profile scores 0 and the first of its two "
    f"lines of why starts with \"{GATE_MARK}\" and names the gate; when it is not clear that it hits a gate, it passes "
    "the gate and is scored on fit alone. Score each posting on its own, whatever the others are. Send them with "
    "jobs.parse_report: {id, fields} where fields is {ranks: [...]}, one object for each posting, {n, fit, why}: n the "
    "posting's number, fit a whole number from 0 to 100, why a list of two short lines (what the posting is, then what "
    "decides the score). A posting with a \"Pay note\" line has the middle of its posted pay under the candidate's pay "
    "floor though its top reaches it: that is no gate and never scores 0, but weigh it as a real drawback and score it "
    "lower than the same posting without the note. Then call jobs.parse_next again."
)


class RankError(ValueError):
    """Nothing can be ranked yet; the message says why."""


# --- the short profile ----------------------------------------------------------------------------------------------


#: How the pay gate's amount is compared with a posted band, in the words a ranker is given.
_PAY_OF_BAND = {"minimum": "the bottom of a posted band", "maximum": "the top of a posted band",
                "ask": "the middle of a posted band"}


def _where(loc: profile_gates.Location) -> str:
    """The work location gate as one sentence with every place the owner named (nothing is left out for length)."""
    ways = []
    for way, label in ((loc.remote, "remote"), (loc.hybrid, "hybrid"), (loc.in_person, "in person")):
        if way.on:
            ways.append(f"{label} ({'only in ' + '; '.join(way.locations) if way.locations else 'anywhere'})")
    return "Work must be " + ", or ".join(ways) + "." if ways else ""


def _pay(comp: profile_gates.Compensation) -> str:
    return (f"Base pay must be at least ${comp.amount:,} a year, a posted band judged by "
            f"{_PAY_OF_BAND.get(comp.against, 'its middle')}.") if comp.amount else ""


def _gate_sentences(profile: schema.Profile, names: dict[str, str]) -> list[str]:
    """The gates a ranker can judge from a posting alone, as the owner wrote them: the work location, the pay and the
    owner's rules (their own words, with their Verify line). A gate decided from the employer's record, the posting's
    age or the analyst's classes of the requirements is not here: a posting does not say it."""
    g = profile.gates
    out: list[str] = []

    def add(sentence: str, roles: list[str]) -> None:
        if sentence.strip():
            only = f" (only for postings matched to {', '.join(names.get(r, r) for r in roles)})" if roles else ""
            out.append(f"{sentence.strip()}{only}")

    add(_where(g.location) if g.checks_location() else "", [])
    add(_pay(g.compensation) if g.checks_pay() else "", [])
    for role_id, own in g.by_role.items():
        add(_where(own.location) if own.checks_location() else "", [role_id])
        add(_pay(own.compensation) if own.checks_pay() else "", [role_id])
    for line in g.checked_lines():
        add(line.text if line.kind == "rule" and not line.follows_classes() else "", [])
    for role_id in dict.fromkeys(r for line in g.other for r in line.roles):
        for line in g.lines_for(role_id):
            if role_id in line.roles:
                add(line.text if line.kind == "rule" and not line.follows_classes() else "", [role_id])
    return out


def render_profile(profile: schema.Profile, claims: str = "") -> str:
    """The short profile as text, from the owner's saved criteria and gates, and `claims`, the cleansed master (one line
    each, `cleansed_master.render`) standing for what the master shows; empty when they hold no target role and no gate
    a ranker can judge. Pure."""
    c = profile.criteria
    roles = [r for r in c.target_roles if not r.is_blank()]
    names = {r.id: r.name or (r.titles[0] if r.titles else r.id) for r in c.target_roles}
    gates = _gate_sentences(profile, names)
    if not roles and not gates:
        return ""
    out = ["CANDIDATE PROFILE"]
    if c.positioning.strip():
        out.append(f"Positioning: {c.positioning.strip()}")
    if c.company.strip():
        out.append(f"Kind of employer wanted: {c.company.strip()}")
    if roles:
        block_ = ["Target roles (a posting that is one of these, at a level the candidate wants, fits best):"]
        for i, r in enumerate(roles, 1):
            bits = [f"{i}. {r.name or 'Unnamed role'}."]
            if r.titles:
                bits.append(f"Example titles: {'; '.join(r.titles)}.")
            for label, key in (("Description", "description"), ("Responsibilities", "responsibilities"),
                               ("Qualifications", "qualifications"), ("Preferred", "preferred")):
                if value := getattr(r, key).strip():
                    bits.append(f"{label}: {value}")
            block_.append(" ".join(bits))
        out.append("\n".join(block_))
    if claims.strip():
        out.append("What the candidate has done (claims drawn from the candidate's resume):\n" + claims.strip())
    if gates:
        out.append("\n".join([
            "Gates (each is a hard no; a posting that clearly hits one scores 0 and its why starts with "
            f"\"{GATE_MARK}\" and names it; a posting that does not clearly hit a gate passes it):",
            *(f"- {g}" for g in gates)]))
    return "\n\n".join(out)


async def _newest_profile(session: AsyncSession, owner: str) -> ProfileVersion | None:
    return await session.scalar(select(ProfileVersion).where(
        ProfileVersion.owner == owner, ProfileVersion.kind == PROFILE_KIND).order_by(ProfileVersion.version.desc()))


async def _profile_now(session: AsyncSession, owner: str) -> tuple[str, ProfileVersion | None]:
    """The short profile as the owner's saved profile makes it now, and the newest version saved of it (None when there
    is none): reads only. Raises `RankError` when the owner has saved no target role and no gate."""
    text = render_profile(await profile_store.current(session, owner),
                          await cleansed_master.short_profile_text(session, owner))
    if not text:
        raise RankError("no target role or gate is saved: there is nothing to rank against")
    return text, await _newest_profile(session, owner)


def _version_of(text: str, newest: ProfileVersion | None) -> int:
    """The version this text of the short profile has: the newest one's when that holds the same text, else the next,
    which `ensure_profile` saves. Pure."""
    if newest is not None and newest.data.get("text") == text:
        return newest.version
    return (newest.version if newest else 0) + 1


async def ensure_profile(session: AsyncSession, owner: str) -> ProfileVersion:
    """The short profile in force: made from the owner's saved profile now, saved as the next version when it differs
    from the newest, else that one. Raises `RankError` when the owner has saved no target role and no gate."""
    return await _saved(session, owner, *await _profile_now(session, owner))


async def _saved(session: AsyncSession, owner: str, text: str, newest: ProfileVersion | None) -> ProfileVersion:
    """The version of the short profile that holds `text`: `newest` when it does, else the next one, saved now."""
    if newest is not None and newest.data.get("text") == text:
        return newest
    made = {kind: await profile_store.newest_version(session, owner, kind) for kind in ("criteria", "gates")}
    row = ProfileVersion(owner=owner, kind=PROFILE_KIND, version=(newest.version if newest else 0) + 1,
                         data={"text": text, "made_from": made}, saved_by="app", why="made from the saved profile")
    session.add(row)
    await session.flush()
    return row


# --- the jobs -------------------------------------------------------------------------------------------------------


def block(n: int, c: CleansedPosting, floor: int = 0) -> str:
    """One posting as the session is sent it, its whole cleansed text; `floor` is the owner's pay floor, for the pay
    note."""
    lines = [f"### Posting {n}", f"Title: {c.title or ''}", f"Company: {c.employer or ''}"]
    if c.locations:
        lines.append(f"Location: {'; '.join(c.locations)}")
    if c.work_mode:
        lines.append(f"Work mode: {c.work_mode}")
    if c.pay_min or c.pay_max:
        lines.append(f"Pay: ${c.pay_min or c.pay_max:,} to ${c.pay_max or c.pay_min:,} a year")
    if band := mostly_below_floor(c.pay_min, c.pay_max, floor):
        lines.append(f"Pay note: the middle of ${band[0]:,} to ${band[1]:,} is under the candidate's ${floor:,} floor")
    return "\n".join(lines) + "\n" + c.text_clean



def room(profile_text: str) -> int:
    """The most posting text one job holds beside this profile: `BATCH_CHARS`, and less when the profile is so long that
    the reply would pass `REPLY_CEILING_CHARS` (negative when the profile leaves none). Pure."""
    return min(BATCH_CHARS, REPLY_CEILING_CHARS - REPLY_RESERVE_CHARS - payload_chars(profile_text))


def batches[T](sized: Sequence[tuple[T, int]], limit: int) -> tuple[list[list[T]], list[tuple[T, int]]]:
    """`(item, characters)` in order, split into batches of at most `RANK_BATCH` items and `limit` characters, the order
    kept, and the items whose text alone is over `limit`, which are in no batch: a posting is never cut to fit. Pure."""
    made: list[list[T]] = []
    refused: list[tuple[T, int]] = []
    used = 0
    for item, chars in sized:
        if chars > limit:
            refused.append((item, chars))
            continue
        if not made or len(made[-1]) >= RANK_BATCH or used + chars > limit:
            made.append([])
            used = 0
        made[-1].append(item)
        used += chars
    return made, refused


async def _open_roles(session: AsyncSession, owner: str) -> set[str]:
    """The roles an open rank job holds."""
    out: set[str] = set()
    for fields in (await session.scalars(select(ParseJob.fields).where(
            ParseJob.owner == owner, ParseJob.kind == ParseKind.RANK,
            ParseJob.state.in_((ParseState.WAITING, ParseState.READING))))).all():
        out.update(str(i["role_id"]) for i in (fields or {}).get("items", []))
    return out


async def unranked(session: AsyncSession, owner: str, version: int) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """The roles with a current cleansed copy and no rank for it at this profile version, the newest posting first (its
    posted date, else the day the app found it, then when the role was made), with the copy: `(role id, cleansed
    id)`. Roles an open job already holds are left out.

    The database does the narrowing: one statement gives the roles in play that have a copy and hold no rank for
    and the rest is decided over it here."""
    # A role a gate closed, or one past its deck, is not ranked (nothing is spent on a role that is out of play). The
    # roles in play are named by their stage, in a statement that binds no ids, and only
    # their postings are read for a copy.
    current = cleansed.current_among(owner, select(Opportunity.id).where(
        Opportunity.owner == owner, Opportunity.status.in_(PRE_READY))).subquery("current")
    # A rank made on a cut text is no rank: the role is ranked again, on the whole text.
    ranked = exists().where(
        RoleRank.owner == owner, RoleRank.role_id == current.c.role_id, RoleRank.cleansed_id == current.c.cleansed_id,
        RoleRank.profile_version == version, RoleRank.text_cut.is_(False))
    rows = await off_turn.rows_in_slices(session, select(
        current.c.role_id, current.c.cleansed_id, Opportunity.posted, Opportunity.found, Opportunity.created_at,
    ).join(Opportunity, Opportunity.id == current.c.role_id).where(~ranked))
    if not rows:
        return []
    # Each role's posted day, else the day the app found it, then when it was made, which orders the line.
    made = {i: (posted or found or created.date(), created) for i, _, posted, found, created in rows}
    copies = {i: c for i, c, *_ in rows}
    held = await _open_roles(session, owner)
    short = await cleansed.short_ids(session, copies.values())  # a title alone is never ranked: it waits for its text
    waiting = [(r, c) for r, c in copies.items() if str(r) not in held and c not in short]
    return sorted(waiting, key=lambda rc: (made[rc[0]], str(rc[0])), reverse=True)


async def queue(session: AsyncSession, owner: str, n: int | None = None, *, dry_run: bool = False,
                jobs: int | None = None) -> dict:
    """Make a waiting rank job for each batch of the roles that need a rank (at most `n` roles), the newest first: a
    batch holds up to `RANK_BATCH` roles and `BATCH_CHARS` of postings (`batches`), so fewer roles when their postings
    are long, each posting whole. A role whose posting is too big for a batch alone is left out, and the log says so.
    `jobs` stops after that many (the pool makes one at a time). `dry_run` only counts. Returns
    `{version, roles, jobs}`; raises `RankError` when there is no profile to rank against."""
    profile = await ensure_profile(session, owner)
    made = await _decide(session, owner, profile.version, profile.data["text"], n, jobs)
    if not dry_run:
        for decided in made:
            session.add(_job(owner, decided.batch()))
        await session.flush()
    return {"version": profile.version, "roles": sum(len(b.pairs) for b in made), "jobs": len(made)}


@dataclass(frozen=True)
class Batch:
    """One rank job as it was decided, in plain values (no row of a session): the version and the text of the short
    profile it is ranked against, its roles with their copies (`items`, as the job keeps them) and the text a session
    is sent."""

    version: int
    profile_text: str
    items: tuple[dict, ...]
    text: str


@dataclass(frozen=True)
class _Decided:
    """One batch of `_decide`: its `(role id, cleansed id)` in order, and what its job's text is made from;
    `batch` makes the `Batch` (the text is built then, so a count builds none)."""

    version: int
    profile_text: str
    pairs: list[tuple[uuid.UUID, uuid.UUID]]
    found: dict[uuid.UUID, CleansedPosting]
    floor: int

    def batch(self) -> Batch:
        items = tuple({"n": i, "role_id": str(r), "cleansed_id": str(c)} for i, (r, c) in enumerate(self.pairs, 1))
        body = "\n\n".join(block(i, self.found[c], self.floor) for i, (_, c) in enumerate(self.pairs, 1))
        return Batch(self.version, self.profile_text, items, f"{self.profile_text}\n\nJOB POSTINGS\n\n{body}")


async def _decide(session: AsyncSession, owner: str, version: int, profile_text: str, n: int | None,
                  jobs: int | None) -> list[_Decided]:
    """The batches `queue` makes jobs of, against this version and text of the short profile: reads only. A role whose
    posting is too big for a batch alone is in none, and the log says so."""
    gates = (await profile_store.current(session, owner)).gates
    floor = (gates.compensation.amount or 0) if gates.checks_pay() else 0
    todo = (await unranked(session, owner, version))[:n]
    if jobs is not None:  # a batch holds at most RANK_BATCH roles: only the first `jobs` batches' roles need sizing
        todo = todo[:jobs * RANK_BATCH]
    found = await cleansed.copies(session, owner, [c for _, c in todo])
    # Sized as the reply carries it, and numbered as the largest number is: the digits are the only difference.
    sized = [((r, c), payload_chars(block(RANK_BATCH, found[c], floor))) for r, c in todo if c in found]
    limit = room(profile_text)
    made, refused = batches(sized, limit)
    for (role_id, _), chars in refused:
        log.warning("rank: role %s is not ranked: its posting is %s characters, over the %s one batch of postings "
                    "holds beside the profile; it is not cut", role_id, f"{chars:,}", f"{max(limit, 0):,}")
    return [_Decided(version, profile_text, pairs, found, floor) for pairs in made[:jobs]]


def _job(owner: str, batch: Batch) -> ParseJob:
    return ParseJob(owner=owner, kind=ParseKind.RANK, role_id=f"rank:{batch.version}", text=batch.text,
                    state=ParseState.WAITING, fields={"profile_version": batch.version, "items": list(batch.items)})


async def plan(session: AsyncSession, owner: str) -> Batch | None:
    """The one batch the pool's pull would queue now (what `queue` makes with `jobs=1`), decided without a write, so a
    reader's session can ask: the short profile is not saved here (`add` saves it with the job). None when no role
    needs a rank; raises `RankError` when there is no profile to rank against."""
    text, newest = await _profile_now(session, owner)
    made = await _decide(session, owner, _version_of(text, newest), text, None, 1)
    return made[0].batch() if made else None


async def add(session: AsyncSession, owner: str, planned: Batch) -> bool:
    """Queue the batch `plan` decided, under the writer's turn. Only what makes the add safe is read again (picking
    the batch under the turn would hold it for just two inserts): the short profile is still the text and
    the version the batch was planned against, and no open rank job holds one of its roles. When either has changed
    nothing is added and the answer is False: the next pull plans again. Services flush."""
    try:
        text, newest = await _profile_now(session, owner)
    except RankError:
        return False
    if text != planned.profile_text or _version_of(text, newest) != planned.version:
        return False
    if await _open_roles(session, owner) & {str(i["role_id"]) for i in planned.items}:
        return False
    await _saved(session, owner, text, newest)  # a text never saved is saved now: the job's version is a saved one
    session.add(_job(owner, planned))
    await session.flush()
    return True


#: Kept on the `fields` of a rank job the owner's star asked for one role (`ask_first`): the pool hands it out before
#: the ordinary batches (`role_parse.waiting`).
FIRST = "first"


async def ask_first(session: AsyncSession, owner: str, role: Opportunity) -> bool:
    """The rank of one starred role, first in the line (the owner, 2026-10-28: a star runs First Impression first): a
    waiting rank job of that one role, handed out before the batches the pool queued. Nothing is made when the role has
    no usable current cleansed copy (the research reads the posting itself then), when a first job or a session holds it
    already, when there is no profile to rank against, or when the posting is too big for a job alone (the log says so;
    it is never cut). A role an ordinary batch holds that no session has taken is
    still asked here: whichever job is reported first ranks it, and a second report of the same role stands
    (`keep`). Returns whether a job was made. Services flush."""
    copy = await cleansed.copy_of(session, owner, role.id)
    if copy is None or len(copy.text_clean or "") < cleansed.MIN_TEXT:
        return False
    held = str(role.id)
    for state, fields in (await session.execute(select(ParseJob.state, ParseJob.fields).where(
            ParseJob.owner == owner, ParseJob.kind == ParseKind.RANK,
            ParseJob.state.in_((ParseState.WAITING, ParseState.READING))))).all():
        if held in {str(i["role_id"]) for i in (fields or {}).get("items", [])} and (
                state == ParseState.READING or (fields or {}).get(FIRST)):
            return False
    try:
        profile = await ensure_profile(session, owner)
    except RankError:
        return False
    gates = (await profile_store.current(session, owner)).gates
    floor = (gates.compensation.amount or 0) if gates.checks_pay() else 0
    posting = block(1, copy, floor)
    if (chars := payload_chars(posting)) > (limit := room(profile.data["text"])):
        log.warning("rank: starred role %s is not ranked first: its posting is %s characters, over the %s one batch of "
                    "postings holds beside the profile; it is not cut", held, f"{chars:,}", f"{max(limit, 0):,}")
        return False
    items = [{"n": 1, "role_id": held, "cleansed_id": str(copy.id)}]
    session.add(ParseJob(owner=owner, kind=ParseKind.RANK, role_id=f"rank:{profile.version}",
                         text=f"{profile.data['text']}\n\nJOB POSTINGS\n\n{posting}",
                         state=ParseState.WAITING,
                         fields={"profile_version": profile.version, "items": items, FIRST: True}))
    await session.flush()
    return True


# --- a gate zero ----------------------------------------------------------------------------------------------------


def gate_zero():
    """Whether a rank is a gate zero (SQL): the ranker scored the posting 0 and a line of its why starts with
    `GATE_MARK`, which is how it says the posting clearly fails a hard ask of the profile. A zero that names no gate (a
    page that is no posting) is not one, nor is a score above 0 that mentions a gate: the ranker was told a gate scores
    0, so only that is a hard no. The one spelling of the rule; `deck.ranked` reads it and the card says its line
    (`gate_line`)."""
    return and_(RoleRank.fit == 0, or_(RoleRank.why.startswith(GATE_MARK), RoleRank.why.contains("\n" + GATE_MARK)))


def gate_line(why: str) -> str:
    """The line of a gate zero's why that names the gate, as the ranker wrote it, with a full stop if it left one off:
    the first that starts with `GATE_MARK`, else the whole of why."""
    line = next((x for x in why.splitlines() if x.startswith(GATE_MARK)), why.strip())
    return line if line.endswith((".", "!", "?")) else f"{line}."


# --- what a session answers -----------------------------------------------------------------------------------------


def _lines(value: object, where: str, problems: list[str]) -> str | None:
    lines = [value] if isinstance(value, str) else value
    if not isinstance(lines, list) or not lines or not all(isinstance(x, str) and x.strip() for x in lines):
        problems.append(f"{where}: why must be one or two short lines of text")
        return None
    if len(lines) > WHY_LINES or any(len(x.strip()) > WHY_CHARS for x in lines):
        problems.append(f"{where}: why is at most {WHY_LINES} lines of {WHY_CHARS} characters")
        return None
    return "\n".join(x.strip() for x in lines)


def check(raw: object, count: int) -> tuple[dict[int, tuple[int, str]], list[str]]:
    """The ranks a session sent for a job of `count` postings: `({n: (fit, why)}, problems)`. A malformed entry is a
    problem and is left out; a posting the session left out is not (it is ranked by the next job), and one it sent
    twice keeps the first."""
    problems: list[str] = []
    ranks = raw.get("ranks") if isinstance(raw, dict) else None
    if not isinstance(ranks, list):
        return {}, ["fields must be {ranks: [{n, fit, why}, ...]}"]
    ok: dict[int, tuple[int, str]] = {}
    for i, item in enumerate(ranks, 1):
        where = f"ranks item {i}"
        if not isinstance(item, dict):
            problems.append(f"{where} must be an object {{n, fit, why}}")
            continue
        n, fit = item.get("n"), item.get("fit")
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= count:
            problems.append(f"{where}: n must be a posting's number, 1 to {count}")
            continue
        if isinstance(fit, bool) or not isinstance(fit, (int, float)) or not 0 <= fit <= 100:
            problems.append(f"{where}: fit must be a number from 0 to 100")
            continue
        why = _lines(item.get("why"), where, problems)
        if why is not None and n not in ok:
            ok[n] = (int(round(fit)), why)
    return ok, problems


async def keep(session: AsyncSession, owner: str, job: ParseJob, ranks: dict[int, tuple[int, str]]) -> int:
    """Keep the ranks of a job's postings (a role or copy that has gone since is passed over). Returns how many. A rank
    already kept for the same role, copy and profile version stands, unless it was
    made on a cut text (`RoleRank.text_cut`): the new rank, made on the whole text, takes its row."""
    items = {int(i["n"]): i for i in (job.fields or {}).get("items", [])}
    version = int((job.fields or {}).get("profile_version") or 0)
    roles = {r for (r,) in (await session.execute(select(Opportunity.id).where(
        Opportunity.owner == owner, Opportunity.id.in_([uuid.UUID(i["role_id"]) for i in items.values()])))).all()}
    copies = await cleansed.copies(session, owner, [uuid.UUID(i["cleansed_id"]) for i in items.values()])
    kept = 0
    touched: set[uuid.UUID] = set()
    # In the order of the keys, never of the postings: two reports over the same roles that each took their own order
    # waited on one another's unique key until the server killed one as a deadlock.
    keyed = sorted((uuid.UUID(items[n]["role_id"]), uuid.UUID(items[n]["cleansed_id"]), fit, why)
                   for n, (fit, why) in ranks.items())
    for role_id, copy_id, fit, why in keyed:
        if role_id in roles and copy_id in copies:
            made = insert(RoleRank).values(
                id=uuid.uuid4(), owner=owner, role_id=role_id, cleansed_id=copy_id, profile_version=version, fit=fit,
                why=why, text_cut=False, created_at=datetime.now(UTC))
            # A rank made on a cut text is replaced in its row by the one on the whole text; any other stands.
            await session.execute(made.on_conflict_do_update(
                index_elements=["role_id", "cleansed_id", "profile_version"],
                set_={"fit": made.excluded.fit, "why": made.excluded.why, "text_cut": False,
                      "created_at": made.excluded.created_at},
                where=RoleRank.text_cut.is_(True)))
            kept += 1
            touched.add(role_id)
    # A rank for a role is read by the deck (`rank.unranked`, `deck.ranks`): the role is looked at, the deck with it.
    todo_queue.post_roles(session, owner, touched, cause="core:rank.keep")
    await session.flush()
    return kept
