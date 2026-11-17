"""The owner's documents, stored as versions (docs/PRD.md 2.2): the profile's four parts (`profile/schema.py`) and the
resume, application info and writing rules (`documents.py`). A save adds the next version and never changes
an earlier one; the newest is in force; any earlier one can be read and brought back (as a new version, so bringing
one back is in the history too). Services flush; the route or command that owns the request commits.
"""

import asyncio
import contextlib
import logging
import re
import uuid
from collections.abc import AsyncIterator, Hashable
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import Text, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.errors import DomainError
from app.jobsearch import as_read, judged, off_turn
from app.jobsearch.actors import BY_APP
from app.jobsearch.models import JobPosting, Opportunity, ProfileVersion
from app.jobsearch.profile import base as profile_base
from app.jobsearch.profile import documents, schema
from app.jobsearch.work import queue
from app.timeutil import iso_or_none

log = logging.getLogger(__name__)

#: Every kind of document the store holds.
ALL_KINDS = profile_base.KINDS + documents.DOCUMENT_KINDS


def parse(kind: str, data: dict | None):
    """A document of any kind from stored data (empty when there is none). Raises pydantic's ValidationError, or
    ValueError for a kind that doesn't exist."""
    if kind in documents.MODELS:
        return documents.MODELS[kind].model_validate(data or {})
    if kind in schema.MODELS:
        return schema.parse(kind, data)
    raise ValueError(f"no document called {kind!r}; the kinds are {', '.join(ALL_KINDS)}")


class ProfileError(DomainError):
    """A document that doesn't fit its kind, or a version that isn't there. The message says what to fix."""

    status = 422


class NoSuchVersion(ProfileError):
    status = 404


class StaleSave(ProfileError):
    """A save made from a copy of the document that is older than the one in force: it would overwrite what was saved
    since. Nothing is stored."""

    status = 409


#: What the owner is told when a page's copy is out of date (the page shows it as it is).
STALE_SAVE = ("This page is out of date: it was changed elsewhere since you opened it. Reload to see the latest, "
              "then make your change again.")


def _check(kind: str, data: dict | None):
    try:
        return parse(kind, data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or kind}: {e['msg']}" for e in exc.errors()[:10])
        raise ProfileError(f"the {kind} don't fit: {problems}") from exc


def checked(kind: str, data: dict | None):
    """A document of any kind from stored data (`parse`), for a caller outside this module that reads one: data that no
    longer fits the model raises `ProfileError`, which says in words what to fix, instead of pydantic's own error."""
    return _check(kind, data)


def reader_lines(kind: str, data: dict | None) -> list[str]:
    """The ids of the lines of Hard pass or Prioritization in `data` that only a reader could judge: a sentence rule
    that is not decided from the analyst's classes, and an ask or a statement of a row with no fact, posting age, pay
    amount or class for the app to check (docs/PRD.md 30, 46: settings are only what code checks, the owner,
    2026-10-26). The built-in ask that follows a role's Responsibilities (`derived`) is the analyst's own read and
    is not one."""
    data = data or {}
    if kind == "gates":
        return [str(g.get("id")) for g in data.get("other") or []
                if g.get("kind", "rule") == "rule" and not g.get("from_classes")]
    if kind != "scoring":
        return []

    def by_reader(a: dict) -> bool:
        return (a.get("checked_by", "reader") == "reader" and not a.get("derived") and not a.get("from_classes")
                and not a.get("fact") and a.get("days") is None and a.get("amount") is None)

    asks = [*(data.get("asks") or []), *(m for g in data.get("groups") or [] for m in g.get("members") or [])]
    return [str(a.get("id")) for a in asks if by_reader(a)]


def refuse_reader_lines(kind: str, data: dict | None) -> None:
    """Refuse a Hard pass or Prioritization that holds a line only a reader could judge."""
    if found := reader_lines(kind, data):
        raise ProfileError(
            f"the {kind} take only what the app checks itself (a fact of the employer or the posting, a posting's age, "
            f"a pay amount, the analyst's classes); no reader judges a line: {', '.join(found[:10])}")


def negative_lines(kind: str, data: dict | None, stored: dict | None = None) -> list[str]:
    """The ids of the asks and statements of Prioritization in `data` with negative points (docs/PRD.md 30.10:
    Prioritization only adds points). A line that is already in `stored`, the version in force, with the very same
    points is the owner's from before the rule and is let through, so it is never rewritten and the other lines still
    save."""
    if kind != "scoring":
        return []
    data, stored = data or {}, stored or {}

    def lines(doc: dict) -> list[dict]:
        return [*(doc.get("asks") or []), *(m for g in doc.get("groups") or [] for m in g.get("members") or [])]

    def points(line: dict) -> int | float | None:
        value = line.get("points")
        return value if isinstance(value, int | float) and not isinstance(value, bool) else None

    kept = {str(line.get("id")): points(line) for line in lines(stored)}
    rows = [g for g in data.get("groups") or [] if g.get("kind", "any") == "any"]
    found = [str(line.get("id")) for line in lines(data)
             if (points(line) or 0) < 0 and kept.get(str(line.get("id"))) != points(line)]
    found += [str(g.get("id")) for g in rows
              if (points(g) or 0) < 0 and kept.get(str(g.get("id"))) != points(g)]
    return found


def refuse_negative_points(kind: str, data: dict | None, stored: dict | None = None) -> None:
    """Refuse a Prioritization that gives a line negative points: use Hard pass to exclude something."""
    if found := negative_lines(kind, data, stored):
        raise ProfileError(
            "Prioritization only adds points: a line cannot have negative points (use Hard pass to exclude "
            f"something): {', '.join(found[:10])}")


async def stored_data(session: AsyncSession, owner: str, kind: str) -> dict | None:
    """The data of the version of `kind` in force (None when the owner has saved none)."""
    row = await _newest(session, owner, kind)
    return row.data if row else None


async def refuse_what_a_save_refuses(session: AsyncSession, owner: str, kind: str, data: dict | None) -> None:
    """Refuse `data` for `kind` as the owner's own save is refused: a Hard pass or Prioritization that holds a line only
    a reader could judge (docs/PRD.md 30, 46), and a Prioritization with negative points that the version in force does
    not hold as they are (30.10). The page's save, the page's restore of an earlier version and the owner door's
    `profile.restore` (50.13) all ask this, so a version saved before the rules is never brought back past them."""
    refuse_reader_lines(kind, data)
    refuse_negative_points(kind, data, await stored_data(session, owner, kind))


def _check_patterns(doc) -> None:
    """A legacy "Never write" pattern written as a regular expression has to be one: the packet check compiles it, and a
    pattern that does not compile would check nothing."""
    for rule in doc.legacy_patterns:
        if rule.regex:
            try:
                re.compile(rule.pattern)
            except re.error as exc:
                raise ProfileError(f"the writing rules don't fit: never write {rule.pattern!r} isn't a valid "
                                   f"regular expression ({exc})") from exc


async def _newest(session: AsyncSession, owner: str, kind: str) -> ProfileVersion | None:
    return await session.scalar(
        select(ProfileVersion).where(ProfileVersion.owner == owner, ProfileVersion.kind == kind)
        .order_by(ProfileVersion.version.desc()).limit(1)
    )


async def newest_version(session: AsyncSession, owner: str, kind: str) -> int:
    """The number of the version of `kind` in force, or 0 when the owner has saved none."""
    row = await _newest(session, owner, kind)
    return row.version if row else 0


class OwnerCache[V]:
    """What a process builds from the database and keeps for the next call: one entry for each owner, which is the one
    built for the key it was built under. It is a cache of a pure function of the rows it was built from, so a call
    always asks the database for the key first (the ids of the rows, or a digest of every row the build reads: a few
    statements for the thousands of rows and the tens of milliseconds of code the build costs), and the entry answers
    only for the very key it holds: a restart, another process, a database emptied and filled again, a changed row all
    give another key, and nothing here is cleared or aged. The entry is built by one task at a time: the others wait and
    look at the entry again (`building`). The caller returns a copy of a value it keeps (or one nothing writes to)."""

    def __init__(self) -> None:
        self._held: dict[str, tuple[Hashable, V]] = {}
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}

    def get(self, owner: str, key: Hashable) -> V | None:
        """The owner's entry when it was built for exactly this key."""
        held = self._held.get(owner)
        return held[1] if held is not None and held[0] == key else None

    def put(self, owner: str, key: Hashable, value: V) -> None:
        self._held[owner] = (key, value)

    @contextlib.asynccontextmanager
    async def building(self, owner: str) -> AsyncIterator[None]:
        """Held while this task builds the owner's entry (a lock for each event loop: the tests run one loop each)."""
        async with self._locks.setdefault((owner, id(asyncio.get_running_loop())), asyncio.Lock()):
            yield


@dataclass(frozen=True)
class InForce:
    """Which row is the newest of each of the four documents, from one small statement (`in_force`): `ids` in the order
    of `profile_base.KINDS` (None for a kind the owner has saved none of) and the `versions` (0 for none) of those rows.
    A version is a row that is never changed (`save` adds the next one), so its id says every word of what it holds."""

    ids: tuple[uuid.UUID | None, ...]
    versions: tuple[int, ...]

    def version_of(self, kind: str) -> int:
        return self.versions[profile_base.KINDS.index(kind)]


_PROFILES: OwnerCache[schema.Profile] = OwnerCache()


async def in_force(session: AsyncSession, owner: str) -> InForce:
    """The newest version of each of the four documents, by its row: one statement for all four, through the unique
    index on owner, kind and version."""
    row = aliased(ProfileVersion)
    newest = (select(func.max(ProfileVersion.version))
              .where(ProfileVersion.owner == row.owner, ProfileVersion.kind == row.kind).scalar_subquery())
    found = {kind: (i, version) for kind, i, version in (await session.execute(
        select(row.kind, row.id, row.version).where(
            row.owner == owner, row.kind.in_(profile_base.KINDS), row.version == newest))).all()}
    return InForce(tuple(found[k][0] if k in found else None for k in profile_base.KINDS),
                   tuple(found[k][1] if k in found else 0 for k in profile_base.KINDS))


async def _profile_of_rows(session: AsyncSession, held: InForce) -> schema.Profile | None:
    """The profile the rows `held` name, read by their ids; None when one of them is gone (a database emptied since
    `in_force` was asked: the caller reads the newest again)."""
    wanted = [i for i in held.ids if i is not None]
    rows: dict[str, ProfileVersion] = {}
    if wanted:
        rows = {r.kind: r for r in (await session.scalars(
            select(ProfileVersion).where(ProfileVersion.id.in_(wanted)))).all()}
    if len(rows) != len(wanted):
        return None
    return schema.Profile.model_validate({kind: rows[kind].data if kind in rows else {} for kind in profile_base.KINDS})


async def profile_of(session: AsyncSession, owner: str, held: InForce) -> schema.Profile:
    """The four documents in force as `held` names them (`in_force`), the profile built for those very rows when this
    process has built it already: a copy, so what a caller changes in it is its own."""
    kept = _PROFILES.get(owner, held.ids)
    if kept is None:
        async with _PROFILES.building(owner):
            kept = _PROFILES.get(owner, held.ids)
            if kept is None:
                kept = await _profile_of_rows(session, held)
                if kept is None:
                    return await _current_from_newest(session, owner)
                _PROFILES.put(owner, held.ids, kept)
    return kept.model_copy(deep=True)


async def _current_from_newest(session: AsyncSession, owner: str) -> schema.Profile:
    rows = {}
    for kind in profile_base.KINDS:
        row = await _newest(session, owner, kind)
        rows[kind] = row.data if row else {}
    return schema.Profile.model_validate(rows)


async def current(session: AsyncSession, owner: str) -> schema.Profile:
    """All four documents in force (empty where the owner has saved none). The newest version of each is named by one
    statement and its profile is built once for as long as no document is saved (each call built it)."""
    return await profile_of(session, owner, await in_force(session, owner))


async def owner_documents(session: AsyncSession, owner: str) -> documents.Documents:
    """The resume, application info and writing rules in force (empty where none was saved)."""
    rows = {}
    for kind in documents.DOCUMENT_KINDS:
        row = await _newest(session, owner, kind)
        rows[kind] = row.data if row else {}
    return documents.Documents.model_validate(rows)


async def _write(session: AsyncSession, owner: str, kind: str, stored: dict, by: str, why: str) -> int | None:
    newest = await _newest(session, owner, kind)
    if newest is not None and newest.data == stored:
        return None
    version = (newest.version if newest else 0) + 1
    session.add(ProfileVersion(owner=owner, kind=kind, version=version, data=stored, saved_by=by, why=why))
    await session.flush()
    return version


async def _removed_tabs(session: AsyncSession, owner: str, saving) -> set[str]:
    """The ids of the target roles the criteria in force have and the ones being saved don't: tabs being deleted."""
    newest = await _newest(session, owner, "criteria")
    if newest is None:
        return set()
    before = {r.id for r in parse("criteria", newest.data).target_roles}
    return before - {r.id for r in saving.target_roles}


async def release_tabs(session: AsyncSession, owner: str, removed: set[str]) -> dict[str, int]:
    """What a deleted tab leaves behind (docs/PRD.md 31.4): its roles are unmatched (their target role cleared, to be
    matched again), never left on an id that is no tab. A posting that one of the tabs said yes to and no reader has
    read goes on to become a role (route `roles`), since the tab that judged it is gone; any other posting that named
    the tab forgets it. Returns how many roles were unmatched (`roles`) and postings sent on to become roles
    (`postings`)."""
    if not removed:
        return {"roles": 0, "postings": 0}
    sent_back = await session.execute(update(JobPosting).where(
        JobPosting.owner == owner, JobPosting.target_role.in_(removed), JobPosting.opportunity_id.is_(None),
        JobPosting.is_open, JobPosting.filtered_by.is_(None), JobPosting.route == judged.READER,
        JobPosting.judged_by == judged.TITLE_CHECK,
    ).values(route=judged.ROLES, route_why=None, judged_by=None, target_role=None))
    await session.execute(update(JobPosting).where(
        JobPosting.owner == owner, JobPosting.target_role.in_(removed)).values(target_role=None))
    await session.execute(update(JobPosting).where(
        JobPosting.owner == owner, JobPosting.nearest_role.in_(removed)).values(nearest_role=None))
    unmatched = list((await session.execute(update(Opportunity).where(
        Opportunity.owner == owner, Opportunity.target_role.in_(removed)
    ).values(target_role=None, matched_by_version=None, matched_by=None).returning(Opportunity.id))).scalars())
    # The postings sent back become roles, the unmatched roles are matched again (the title check), the deck is
    # looked at.
    queue.post_all(session, owner, [
        (queue.POSTING_ROLES, [""]), (queue.DECK_DIRTY, [""]), (queue.ready("titles"), [""]),
        (queue.ROLE_SETTLE, unmatched)], cause="core:profile.release_tabs")
    return {"roles": len(unmatched), "postings": sent_back.rowcount}


#: The kinds whose save sets code going at once over what is already stored, here: Prioritization (every score is worked
#: out again). The drafting bar, a Settings field, is applied by the route that saves it (`mission_numbers.rerun`).
WORKS_OUT_AGAIN = ("scoring",)
#: The kinds whose save can add a gate or an ask that reads an employer fact (docs/PRD.md 39.18): the employers of the
#: roles in play it reaches, whose record lacks the fact, are sent back to the employer researcher for the next night.
READS_EMPLOYER_FACTS = ("gates", "scoring")


async def save(session: AsyncSession, owner: str, kind: str, data: dict, *, by: str, why: str = "",
               base_version: int | None = None, said: list[str] | None = None,
               defer_works_out: bool = False) -> int | None:
    """Store `data` as the next version of `kind`. Returns the new version number, or None when nothing changed. A
    save that names the version it was made from (`base_version`, what a page sends: the version it loaded) is refused
    with `StaleSave` when a newer one is in force, so a page left open cannot write its old copy over what was saved
    since; a save that names none (an agent, a script, an import) is stored as it comes. A
    version of What to look for without a target role the one before had unmatches that role's roles
    (`release_tabs`) in the same save, whichever way it is made (the page, a restored version, an import). A version of
    Prioritization works every score out again in the same save (`prioritize.after_save`); the line that says what it
    did is added to `said`, which a caller that shows the owner a line passes, and so is how many postings a deleted
    tab's release sent on to become roles.

    With `defer_works_out` the scores are not worked out here: the caller commits the version and then calls
    `works_out_again` with the session free, so the scores are written a page to a turn instead of all under the
    save's one (the engine does, asked by the page's save and restore: `engine_asks`). Everything else a save does stays
    in the version's transaction."""
    # A Hard pass: the roles whose answers lack their rule's words are found before the first statement of `session`,
    # on a reader where it is free, so the turn is not held for that read.
    unstamped = await _lacking_rule_words(session, owner) if kind == "gates" else []
    if base_version is not None and await newest_version(session, owner, kind) > base_version:
        raise StaleSave(STALE_SAVE)
    doc = _check(kind, data)
    if kind == "writing":
        _check_patterns(doc)
    removed = await _removed_tabs(session, owner, doc) if kind == "criteria" else set()
    before = await current(session, owner) if kind in (*WORKS_OUT_AGAIN, *READS_EMPLOYER_FACTS) else None
    version = await _write(session, owner, kind, doc.model_dump(mode="json"), by, why)
    if kind == "resume" and version is not None:
        await _read_master(session, owner)
    if kind in ("resume", "writing") and version is not None:
        await _build_library(session, owner)
    if kind == "gates" and before is not None and version is not None:
        await _keep_rule_words(session, owner, before, unstamped)
    if removed and version is not None:
        released = (await release_tabs(session, owner, removed))["postings"]
        if released and said is not None:  # the rows this save changed are all in its line (docs/PRD.md 39.16)
            posting, goes, role = ("posting", "goes", "a role") if released == 1 else ("postings", "go", "roles")
            said.append(f"{released:,} {posting} the deleted role had said yes to {goes} on to become {role}.")
    if before is not None and version is not None:
        lines = []
        if kind in WORKS_OUT_AGAIN and not defer_works_out:
            lines.append(await _works_out_again(session, owner, kind, before, version))
        if kind in READS_EMPLOYER_FACTS:
            lines.append(await _wants_facts(session, owner, kind, before))
        if said is not None:
            said.extend(line for line in lines if line)
    return version


async def _read_master(session: AsyncSession, owner: str) -> None:
    """A new master version gets its structured record built once (`structured_profile.ensure`: one small session of
    the parser kind). In a savepoint: no agent to take it now, or any failure, keeps the save, and the master is never
    touched."""
    # they read the store: imported here so they never loop
    from app.jobsearch.jobs import parse as role_parse
    from app.jobsearch.resume import cleansed_master, structured_profile

    for ensure in (structured_profile.ensure, cleansed_master.ensure):  # its record, then its claims (a Sonnet job)
        try:
            async with session.begin_nested():
                await ensure(session, owner)
        except role_parse.NotReady:
            log.info("no agent to read the new master resume for its record just now")
        except Exception:
            log.exception("couldn't ask for the new master resume's record")


async def _build_library(session: AsyncSession, owner: str) -> None:
    """A new master or new writing rules start a story library build (`story_library.ensure_current`): only what
    changed is written again. In a savepoint: any failure keeps the save, and the next packet asked starts it."""
    from app.jobsearch.writing import (
        library as story_library,  # it reads the store: imported here so the two never loop
    )

    try:
        async with session.begin_nested():
            await story_library.ensure_current(session, owner)
    except Exception:
        log.exception("couldn't start a story library build after a saved master or writing rules")


async def _lacking_rule_words(session: AsyncSession, owner: str) -> list[uuid.UUID]:
    """The roles holding an answer to a sentence rule of the profile in force that names no words (`_keep_rule_words`
    puts them on). Read on a reader where `session` is free (`off_turn.reading`): no turn for it."""
    from app.jobsearch.gating import sentence_gates  # sentence_gates reads scoring: imported here so the two never loop

    async with off_turn.reading(session) as reader:
        before = await current(reader, owner)
        rows = (await reader.execute(
            select(Opportunity.id, Opportunity.gate_answers)
            .where(Opportunity.owner == owner, cast(Opportunity.gate_answers, Text) != "[]"))).all()
    return [row.id for row in rows if sentence_gates.stamp(row.gate_answers, before) != row.gate_answers]


async def _keep_rule_words(session: AsyncSession, owner: str, before: schema.Profile, roles: list[uuid.UUID]) -> None:
    """A reader's answers to a sentence rule that were given before their words were kept stand for the words the rule
    had until this save (docs/PRD.md 39.26): the words are put on them now, so a rule this save words otherwise is a
    new question, and nothing is read again just because the words were never kept. The migration did this for the
    answers there were; this keeps it so for any that come without words. `roles` are the ones found lacking words
    before the save began (`_lacking_rule_words`): each is read again here, under the save's turn, and written only
    if it still lacks them."""
    from app.jobsearch.gating import sentence_gates  # sentence_gates reads scoring: imported here so the two never loop

    held = await as_read.lock(session, Opportunity, roles, defer=(Opportunity.description, Opportunity.analysis))
    for role in held.rows.values():
        kept = sentence_gates.stamp(role.gate_answers, before)
        if role.owner == owner and kept != role.gate_answers:
            role.gate_answers = kept
    await session.flush()


async def _wants_facts(session: AsyncSession, owner: str, kind: str, before: schema.Profile) -> str | None:
    """A gate or an ask that is new reads an employer fact some record lacks: the employers it reaches go back to the
    researcher (`employer_research.want_facts`), and the line says how many wait for tonight. In a savepoint: a failure
    keeps the save."""
    from app.jobsearch.research import (
        employer as employer_research,  # it reads the store: imported here so the two never loop
    )

    try:
        async with session.begin_nested():
            return employer_research.said_for(
                await employer_research.want_facts(session, owner, before, await current(session, owner)))
    except Exception:
        log.exception("couldn't send employers back to the researcher after a saved %s", kind)
        return None


async def _works_out_again(session: AsyncSession, owner: str, kind: str, before: schema.Profile, version: int) -> str:
    """What a save of Prioritization does to the roles already stored, and the line that says it. It runs in a
    savepoint: a failure keeps the save (the new version is stored) and says so, rather than losing the owner's edit."""
    from app.jobsearch.research import prioritize  # prioritize reads the store: imported here so the two never loop

    try:
        async with session.begin_nested():
            return await prioritize.after_save(
                session, owner, before=before, after=await current(session, owner), version=version)
    except Exception:
        log.exception("couldn't apply a saved %s to the roles already stored", kind)
        return COULD_NOT_WORK_OUT


#: What the owner is told when a save was stored and the roles could not be worked out again.
COULD_NOT_WORK_OUT = "Saved, but the roles already stored could not be worked out again just now."


async def works_out_again(session: AsyncSession, owner: str, kind: str, version: int | None) -> str | None:
    """What `save` leaves to its caller with `defer_works_out`: what version `version` of `kind` does to the roles
    already stored (Prioritization: every score worked out again), and the line that says it. None for a save that
    stored nothing or a kind with nothing to work out. Called with `session` free, after the version is committed:
    the profiles are read on a reader, and the scores are read and worked out a page at a time on one and written a
    page to a turn, each page committed (`prioritize.rescore`). A failure keeps the save and the pages already
    written, and says so."""
    if version is None or kind not in WORKS_OUT_AGAIN:
        return None
    from app.jobsearch.research import prioritize  # prioritize reads the store: imported here so the two never loop

    try:
        async with off_turn.reading(session) as reader:
            after = await current(reader, owner)
            earlier = await reader.scalar(select(ProfileVersion.data).where(
                ProfileVersion.owner == owner, ProfileVersion.kind == kind, ProfileVersion.version == version - 1))
        # The profile as it was before this version: the same but for this document, as its version before held it.
        before = after.model_copy(update={kind: parse(kind, earlier)})
        return await prioritize.after_save(session, owner, before=before, after=after, version=version)
    except Exception:
        log.exception("couldn't apply a saved %s to the roles already stored", kind)
        await session.rollback()
        return COULD_NOT_WORK_OUT


async def ensure_starter_writing(session: AsyncSession, owner: str) -> int | None:
    """A new install's Rules for ATS screening start with four plain starter rules (`documents.STARTER_ATS`, docs/PRD.md
    33.6): when the owner has no writing rules at all, they are saved as version 1, by the app, so the history says
    where they came from and a default is never a rule the owner has not seen saved. It does nothing to an install that
    has any version of the writing rules, whatever is in it. Safe to run again. Returns the version it made, or None."""
    if await _newest(session, owner, "writing") is not None:
        return None
    starter = {"ats_rules": [rule.model_dump(mode="json") for rule in documents.STARTER_ATS]}
    return await save(session, owner, "writing", starter, by=BY_APP, why="starter rules")


async def save_parts(session: AsyncSession, owner: str, parts: dict, *, by: str,
                     why: str = "") -> dict[str, int | None]:
    """Save several documents given together (a profile file), each as its next version."""
    return {kind: await save(session, owner, kind, parts[kind], by=by, why=why) for kind in ALL_KINDS if kind in parts}


async def history(session: AsyncSession, owner: str, kind: str) -> list[dict]:
    """Every version of `kind`, newest first: who saved it, when, why, and what changed from the one before."""
    _check(kind, {})
    rows = (await session.scalars(
        select(ProfileVersion).where(ProfileVersion.owner == owner, ProfileVersion.kind == kind)
        .order_by(ProfileVersion.version.desc())
    )).all()
    out = []
    for i, row in enumerate(rows):
        before = rows[i + 1].data if i + 1 < len(rows) else {}
        out.append({
            "version": row.version, "saved_by": row.saved_by, "why": row.why, "saved_at": iso_or_none(row.saved_at),
            "changed": changes(before, row.data),
        })
    return out


async def version(session: AsyncSession, owner: str, kind: str, number: int) -> dict:
    _check(kind, {})
    row = await session.scalar(select(ProfileVersion).where(
        ProfileVersion.owner == owner, ProfileVersion.kind == kind, ProfileVersion.version == number))
    if row is None:
        raise NoSuchVersion(f"there is no version {number} of the {kind}")
    return {"version": row.version, "saved_by": row.saved_by, "why": row.why, "saved_at": iso_or_none(row.saved_at),
            "data": row.data}


async def set_why(session: AsyncSession, owner: str, kind: str, number: int, why: str, *, by: str) -> dict:
    """Say why a version was saved, after it was ("Saving": a field saves itself, and its
    Saved line offers "Add a reason"). Only the newest version, and only one the same hand saved: an older version's
    record, and an agent's reason, stay as they were. The reason is the owner's text, kept whole. Answers the version as
    `history` lists it, without `changed`."""
    _check(kind, {})
    row = await _newest(session, owner, kind)
    if row is None or row.version != number:
        raise ProfileError(f"version {number} of the {kind} is not the newest, so its reason stays as it was")
    if row.saved_by != by:
        raise ProfileError(f"version {number} of the {kind} was saved by {row.saved_by}, so its reason is theirs")
    row.why = why.strip()
    await session.flush()
    return {"version": row.version, "saved_by": row.saved_by, "why": row.why, "saved_at": iso_or_none(row.saved_at)}


async def restore(session: AsyncSession, owner: str, kind: str, number: int, *, by: str, why: str | None = None,
                  said: list[str] | None = None, defer_works_out: bool = False) -> int | None:
    """Bring back an earlier version: it becomes the newest, and the history says so (`why`, else "brought back version
    N"). `said` and `defer_works_out` are as `save` takes them. A version with a line a save refuses
    (`refuse_what_a_save_refuses`) is refused in the same words, with nothing stored."""
    old = await version(session, owner, kind, number)
    await refuse_what_a_save_refuses(session, owner, kind, old["data"])
    return await save(session, owner, kind, old["data"], by=by, why=why or f"brought back version {number}", said=said,
                      defer_works_out=defer_works_out)


async def restore_plan(session: AsyncSession, owner: str, kind: str, number: int) -> dict:
    """What `restore` would do, with nothing written: `in_force`, the number of the version now in force (0: none);
    `version`, the number the restore would make, or None when it would save nothing (the version asked for is the
    one in force, or holds just what that one does); and `changes`, what differs between the two in short plain
    phrases (`changes`). The version asked for is read as `save` reads it (`_check`), so it is compared as it would be
    stored. It raises what `restore` would: no such version, or a line a save refuses."""
    old = await version(session, owner, kind, number)
    await refuse_what_a_save_refuses(session, owner, kind, old["data"])
    doc = _check(kind, old["data"])
    if kind == "writing":
        _check_patterns(doc)
    stored = doc.model_dump(mode="json")
    newest = await _newest(session, owner, kind)
    in_force = newest.version if newest else 0
    if newest is not None and newest.data == stored:
        return {"in_force": in_force, "version": None, "changes": []}
    return {"in_force": in_force, "version": in_force + 1, "changes": changes(newest.data if newest else {}, stored)}


async def counts(session: AsyncSession, owner: str) -> dict[str, int]:
    """How many versions each kind has (0: never saved)."""
    rows = (await session.execute(
        select(ProfileVersion.kind, func.count()).where(ProfileVersion.owner == owner).group_by(ProfileVersion.kind)
    )).all()
    found = dict(rows)
    return {kind: found.get(kind, 0) for kind in ALL_KINDS}


#: Fields the app keeps for itself (a record of what it did), never listed as something that changed.
KEPT_BY_THE_APP = ("built_ins_given",)


def changes(before: dict, after: dict, limit: int = 12) -> list[str]:
    """What changed between two versions, in short plain phrases: a field set or cleared, and list items added,
    removed or changed by their id ("added gate pay-floor", "changed modifier comp-above-N")."""
    out: list[str] = []
    for key in sorted(set(before) | set(after)):
        if key in KEPT_BY_THE_APP:
            continue
        old, new = before.get(key), after.get(key)
        if old == new or (not old and not new):  # [] and nothing are the same: nothing set
            continue
        name = key.replace("_", " ")
        if isinstance(old or new, list) and all(isinstance(x, dict) and "id" in x for x in (old or []) + (new or [])):
            was = {x["id"]: x for x in old or []}
            now = {x["id"]: x for x in new or []}
            these = [f"added {name}: {i}" for i in now if i not in was]
            these += [f"removed {name}: {i}" for i in was if i not in now]
            these += [f"changed {name}: {i}" for i in now if i in was and was[i] != now[i]]
            out += these or [f"reordered {name}"]
        elif not old:
            out.append(f"set {name}")
        elif not new:
            out.append(f"cleared {name}")
        else:
            out.append(f"changed {name}")
    return out[:limit] + ([f"and {len(out) - limit} more"] if len(out) > limit else [])
