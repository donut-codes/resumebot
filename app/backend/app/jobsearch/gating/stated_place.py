"""A place the posting states decides the work location gate (the owner, 2026-10-17).

A posting can say "Locations - San Francisco, CA", "not eligible for 100% remote work" and "onsite at the client up
to 5 days a week", yet the role holds only "United States", onsite, so the gate for a hybrid or in person role in the
owner's own towns came back undecided and the role went on to the writer. A missing
fact never fails a role (docs/PRD.md 30.2), but a fact the posting states is not missing. So:

- **The posting's own label decides first** (`stated`): a line "Location: <place>" or "Locations" followed by its list
  ("- San Francisco, CA"), read in code from the text the role holds, word for word. Only a place that names a town is
  taken, and it is put on a role whose place names none ("United States", "Not stated", empty), never over a town the
  role holds unless the stated list names that town and more (an employer's job list often gives only the first city
  of a multi-city posting: "Boston, Boston, MA, United States" against a list of 28 cities that includes Boston), and
  never on a role that is remote (`fills`). The analyst's own `location` is held to the same rule
  (`analysis.submit`).
- **The analyst is asked once** when the gate stays undecided and the text the role holds may say where the work is:
  research is refused until it sends the place the posting states, or `place_not_stated` when the posting and the
  employer's page truly do not say (`problem`). What it answered is kept on the research (`PLACE_KEY`).
- **Before the writer drafts** (`recheck`, from `packets.start` for the app's own asks) every gate is checked again on
  the role's facts: a role that now fails goes to Gated out with its reason and is not drafted; one whose place is
  still undecided while its research never answered the place goes back to the analyst; one whose posting really does
  not say goes on, as before.

A role in Ready to Launch whose place names a town and whose work mode is hybrid or on-site is judged against the
owner's place rule at once (`decide`: a role that fails is Gated out, whether or not it waits on the analyst; it runs
whenever a place input may have changed: every source read of the role, and every turn of the deck for the roles in
Ready to Launch, which is when a changed copy of a posting is looked at, `deck.gate`; research settles the role itself).

**One place.** Every reader here reads the subject the re-gate and the settle read (`_subject`: the role with its
cleansed copy's stated facts in place of its own, `gating.role_subject`), never the role's columns alone. A copy's
tag does not lift a way of working the posting's own words state (`check._own_way_stands`); where a role and
its copy otherwise differ on the way of working, the copy's stands (docs/PRD.md 39.11). Services flush; the caller
commits."""

import re
import uuid
from collections.abc import Iterable
from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app import db
from app.jobsearch import stages
from app.jobsearch.gating import check as gating
from app.jobsearch.measures.pay_place import counted_places, names_a_town, says_no_place, says_remote
from app.jobsearch.models import JobPosting, Opportunity
from app.jobsearch.pipeline import placement
from app.jobsearch.postings import cleansed
from app.jobsearch.postings import text as posting_text
from app.jobsearch.research import settle
from app.trail import service as trail

#: Where the research keeps what the analyst said of the place: the place was stated, the posting does not say, or it
#: was asked and never answered (refusals ran out). A role researched before this has none.
PLACE_KEY = "place"
STATED, NOT_STATED, ASKED = "stated", "not_stated", "asked"
#: What `recheck` did with a role.
GATED, PASSED, SENT_BACK, LEFT = "gated", "passed", "sent_back", "left"
#: The longest place a label line is read for: a longer one is a sentence, not a place.
PLACE_CHARS = 120

#: A label line: "Location: Austin, TX", "Job Location", "Work locations:", "Locations". Never "Our Locations" (a
#: site's footer).
_LABEL = re.compile(r"^(?:job |work |primary |office )?locations?\s*(?::\s*(?P<place>.*))?$", re.I)


def stated(text: str | None) -> str | None:
    """The places a posting's text states on its own label lines, each once, joined with "; ", or None: the value of a
    "Location: <place>" line, and the list items ("- <place>") right under a "Locations" heading. Only a place that
    names a town is taken (`gates.names_a_town`); a country, "Remote" or "Multiple" alone says no town."""
    lines = [line.strip() for line in (text or "").splitlines()]
    found: list[str] = []
    for at, line in enumerate(lines):
        label = _LABEL.match(line)
        if not label:
            continue
        if label.group("place"):
            found.append(label.group("place"))
            continue
        for item in lines[at + 1:]:
            if not item.startswith("- "):
                break
            found.append(item[2:])
    places = (" ".join(p.split()) for p in found)
    return "; ".join(dict.fromkeys(p for p in places if p and len(p) <= PLACE_CHARS and names_a_town(p))) or None


def _towns(place: str | None) -> set[str]:
    """The towns a written place names, lowercase: the first piece of each "; "-joined entry ("Boston, Boston, MA,
    United States" and "Boston, MA" are both "boston")."""
    return {" ".join(entry.split(",")[0].split()).lower() for entry in (place or "").split(";") if entry.strip()}


def fills(held: str | None, work_mode: str | None, place: str | None) -> bool:
    """Whether `place` should take the place of what a role holds: it names a town, and the role's place names none
    and does not say the work is remote (a remote role's place is its country, kept as it is). One more case: a role
    that holds a town takes a list of places that includes that town and names more (a job list gives only the first
    city of a multi-city posting, `stated`): a held town the list does not name is never overwritten."""
    if not place or not names_a_town(place):
        return False
    if (work_mode or "").strip().lower() == "remote" or says_remote((held or "").lower()):
        return False
    if not names_a_town(held):
        return True
    held_towns, stated_towns = _towns(held), _towns(place)
    return bool(held_towns) and held_towns < stated_towns


def fill(role: Opportunity, text: str | None) -> str | None:
    """Put the place the posting's text states (`stated`) on a role whose place names no town (`fills`). Returns the
    place put, or None when nothing changed."""
    place = stated(text)
    if not fills(role.location, role.move_type, place):
        return None
    role.location = place
    return place


def keep_facts(role: Opportunity, facts: dict) -> None:
    """Put a posting's work mode and place on the role, for what the role has none of: the role's own word stands. A
    place that names no town ("United States") is none for the work location gate, so a town the posting states takes
    its place (`fills`)."""
    if facts.get("work_mode") and not (role.move_type or "").strip():
        role.move_type = facts["work_mode"]
    if facts.get("location") and (not (role.location or "").strip()
                                  or fills(role.location, role.move_type, facts["location"])):
        role.location = facts["location"]


def from_postings(held: str | None, listed: Iterable[str | None]) -> str | None:
    """The places the postings that are this role give, for a role that holds only a count or a placeholder
    ("2 Locations", "Multiple Locations", nothing) while a posting of its own names them (a job system's list gives
    only the count; its page gives the places, and a posting keeps them: `postings._keeps_places`). The distinct places
    of the postings that name some, joined with "; ", only when they are at least as many as the count the role holds;
    None when the role's place says something already, or no posting gives more."""
    if not says_no_place(held):
        return None
    places = list(dict.fromkeys(" ".join(x.split()) for x in listed if x and not says_no_place(x)))
    found = [p for place in places for p in place.split(";") if p.strip()]
    if not places or len(found) < (counted_places(held) or 1):
        return None
    return "; ".join(places)


#: How many roles one transaction of `settle_places` writes: each is a place, a history line and a settle (its score and
#: every gate again; the rules, the posted days and where each stands are read once for the batch): a few
#: milliseconds a role warm and more with the other tasks running, so a batch of fifteen holds the turn well under a
#: second. The shape's constant,
#: not a setting; the tests patch it.
PLACES_BATCH = 15


async def naming_places(session: AsyncSession, owner: str, roles: Iterable[Opportunity]) -> dict[uuid.UUID, str]:
    """The places the postings of these roles name, for the roles that hold only a count or a placeholder
    (`says_no_place`) while a posting of their own names them (`from_postings`): role id to the place, in the roles'
    order. Reads only. It is the one question `settle_places` writes the answer of, and a caller that must know
    whether anything would be written asks it first."""
    blank = {r.id: r for r in roles if says_no_place(r.location)}
    if not blank:
        return {}
    listed: dict[uuid.UUID, list[str | None]] = {}
    for ident, place in (await session.execute(select(JobPosting.opportunity_id, JobPosting.location).where(
            JobPosting.owner == owner, JobPosting.opportunity_id.in_(list(blank)))
            .order_by(JobPosting.first_seen, JobPosting.id))).all():
        if ident is not None:
            listed.setdefault(ident, []).append(place)
    named = {ident: from_postings(role.location, listed.get(ident, [])) for ident, role in blank.items()}
    return {ident: place for ident, place in named.items() if place}


async def settle_places(session: AsyncSession, owner: str, roles: Iterable[Opportunity]) -> list[Opportunity]:
    """Give each of these roles whose place is only a count or a placeholder the places its postings name
    (`naming_places`), with a line on its history, and settle each at once (`settle.roles`: its score and every gate
    again on the place; a role that now fails a gate is Gated out, its packet kept; one the owner moved by hand is
    left). Returns the roles whose place was set. Services flush; the caller commits, except that the roles are written
    `PLACES_BATCH` to a transaction (`db.release_if_free`, which a caller inside a savepoint or a process step does not
    allow: there the batches share its transaction): the first turn after a restore may have thousands to write, which
    one transaction would hold for seconds. The roles of a batch after the first are read again as they are
    then, and asked again which place their postings name."""
    named = await naming_places(session, owner, roles := list(roles))
    waiting = [r for r in roles if r.id in named]
    done: list[Opportunity] = []
    for at in range(0, len(waiting), PLACES_BATCH):
        batch = waiting[at:at + PLACES_BATCH]
        if at and await db.release_if_free(session):
            batch = list((await session.scalars(select(Opportunity).where(
                Opportunity.owner == owner, Opportunity.id.in_([r.id for r in batch]))
                .options(defer(Opportunity.analysis), defer(Opportunity.description))
                .execution_options(populate_existing=True))).all())
            asked = await naming_places(session, owner, batch)  # the answer for this batch replaces the first one
            named = {i: p for i, p in named.items() if i not in {r.id for r in batch}} | asked
        placed = []
        for role in batch:
            if place := named.get(role.id):
                if not says_no_place(role.location):
                    continue
                was, role.location = role.location, place
                placed.append(role)
                await trail.record(session, owner, event="place_stated", actor="app", who=trail.APP_WHO,
                                   link=("opportunity", role.id),
                                   sentence=f"The role's postings name where the work is: {place}.",
                                   detail=f"The role held \"{was or 'no place'}\"; its own postings, read from the job "
                                          "system's page, name the places. The work location gate is decided on them.")
        await settle.roles(session, placed, cause="The places its postings name were put on the role")
        done += placed
    return done


#: The stages a role is open in that the deck does not read: its place is settled here (`settle_open_places`).
OPEN_STAGES = (stages.CLEANSED_DATA, stages.GATED_OUT)


async def settle_open_places(session: AsyncSession, owner: str) -> list[Opportunity]:
    """`settle_places` for the roles in Cleansed Data and Gated out, which the deck's own call (First Impression, Second
    Impression and Ready to Launch) does not reach: a role born from a job system's list holds only a count ("2
    Locations"), and with no town on the role or its copy the work location gate has nothing to judge and passes. The
    roles that hold no place or a count are found from their place alone, then read whole and given the places their
    postings name, on the same condition (`from_postings`). Run by the re-gate before it judges. Returns the roles whose
    place was set."""
    ids = [ident for ident, place in (await session.execute(select(Opportunity.id, Opportunity.location).where(
        Opportunity.owner == owner, Opportunity.status.in_(OPEN_STAGES)))).all() if says_no_place(place)]
    placed: list[Opportunity] = []
    for at in range(0, len(ids), PLACES_BATCH):  # a batch to a turn: a big one holds the turn for long
        roles = (await session.scalars(select(Opportunity).where(
            Opportunity.owner == owner, Opportunity.id.in_(ids[at:at + PLACES_BATCH]))
            .options(defer(Opportunity.analysis), defer(Opportunity.description)))).all()
        placed += await settle_places(session, owner, roles)
        await db.release_if_free(session)  # each batch is a turn of its own
    return placed


def _subject(role: Opportunity, rules: gating.Rules, copy=None, read: bool | None = None) -> gating.Subject:
    """The role as the work location gate reads it: the one subject every path reads (`gating.role_subject`, with the
    role's cleansed copy `copy` when it has one, and `read`: whether the posting was read whole by every reader of a
    way of working, so one it does not state is assumed on site). (2026-11-03): this module read the role's own columns
    while the re-gate and the settle read the copy's, so a role
    the copy placed elsewhere (or made remote) was judged one way here and the other there."""
    return gating.role_subject(role, rules.index.of(role.employer_id, role.company), copy, None, read)


def _known_town(subject: gating.Subject) -> bool:
    """Whether the place a subject holds names a town and its way of working is hybrid or on-site: the places
    `fails_known_place` judges at once."""
    return (subject.work_mode or "").strip().lower() in ("hybrid", "onsite") and names_a_town(subject.location)


def copy_moves_place(role: Opportunity, copy) -> bool:
    """Whether the role's cleansed copy states a place or a way of working that changes what the gate reads from the
    role's own (`_subject` against the role alone), and what it reads is one `fails_known_place` judges: when the copy
    only repeats the role's own place, that is what was judged when it last changed, and a copy that gives no town with
    a way of working tied to it leaves nothing for the known-town check to fail."""
    if copy is None:
        return False
    own, read = gating.role_subject(role, None), gating.role_subject(role, None, copy)
    return (own.location, own.work_mode) != (read.location, read.work_mode) and _known_town(read)


def undecided(role: Opportunity, rules: gating.Rules, location: str | None = None,
              work_mode: str | None = None, copy=None, read: bool | None = None) -> bool:
    """Whether the work location gate (All's, or the role's own) cannot be decided on the role's facts (and its cleansed
    copy's, `copy`), or on `location` and `work_mode` in place of the role's own (nothing is set on the role). A way of
    working the posting does not state is assumed on site once every reader is done with the posting (`read`, as
    `gating.role_subject` has it; `False`: the gate as the posting states it, which is what a reader is asked about)."""
    subject = _subject(role, rules, copy, read)
    if location is not None:
        subject = replace(subject, location=location)
    if work_mode is not None:
        subject = replace(subject, work_mode=work_mode)
    return any(line.kind == "location" and line.result == gating.UNKNOWN for line in gating.results(subject, rules))


def _says_of_the_place(subject: gating.Subject, rules: gating.Rules) -> list[tuple[str, str]]:
    """What the work location gate (All's, and the role's own) says of a subject: each line's id and result."""
    return [(line.id, line.result) for line in gating.results(subject, rules) if line.kind == "location"]


def asks_the_way(role: Opportunity, rules: gating.Rules, copy=None) -> bool:
    """Whether a reader is to be asked for the way of working (and the place, when the role's names no town) before the
    gate judges the role: the gate cannot be decided on the posting as it states it (`undecided`, `read=False`), or can
    and would say something else judged as in person, which is what a posting that states no way is judged as (the
    owner, 2026-11-05): a town in the owner's hybrid places passes for want of a way and fails as in person. Only a copy
    that is whole and says nothing of remote or hybrid work is judged as in person at all (`gating.silent_copy`); for
    any other, what the posting states is all the gate reads and an unknown passes."""
    stated = _says_of_the_place(_subject(role, rules, copy, False), rules)
    if any(result == gating.UNKNOWN for _, result in stated):
        return True
    return gating.silent_copy(copy) and stated != _says_of_the_place(_subject(role, rules, copy, True), rules)


#: The stages whose roles a known town is judged for at once: Ready to Launch.
DECIDED_STAGES = (stages.READY_TO_LAUNCH,)


def fails_known_place(role: Opportunity, rules: gating.Rules, copy=None) -> bool:
    """Whether the place the gate reads names a town, its way of working is hybrid or on-site, and the owner's work
    location gate fails on it: decided in code, from what the role holds and its cleansed copy states (`copy`: the one
    subject, `_subject`), with no wait for the analyst (the owner, 2026-10-17). The gate's own code reads the place
    (`gating.results`); remote and nationwide roles are left to the rule as it is."""
    subject = _subject(role, rules, copy)
    if not _known_town(subject):
        return False
    return any(line.kind == "location" and line.result == gating.FAIL for line in gating.results(subject, rules))


async def decide(session: AsyncSession, owner: str, role: Opportunity, *, cause: str) -> bool:
    """Judge a role in `DECIDED_STAGES` whose town is known against the owner's place rule now (`fails_known_place`, on
    the role and its cleansed copy): one that fails goes to Gated out with the reason, its packet kept (`settle.roles`;
    a role the owner moved by hand is left). Run whenever a place input may have changed: a source read gave the role
    or its posting a place or a way of working (`posting_text`), or its copy changed (`deck.gate`). Returns whether the
    role is now Gated out. Services flush; the caller commits."""
    if role.status not in DECIDED_STAGES:
        return False
    return await decide_on(session, owner, role, await cleansed.gate_copy_of(session, owner, role.id),
                           await gating.rules(session, owner), cause=cause)


async def decide_on(session: AsyncSession, owner: str, role: Opportunity, copy, rules: gating.Rules, *,
                    cause: str) -> bool:
    """`decide` for a caller that holds the role's cleansed copy `copy` and the owner's `rules` already (a loop over the
    roles in `DECIDED_STAGES`)."""
    if role.status not in DECIDED_STAGES or not fails_known_place(role, rules, copy):
        return False
    await settle.roles(session, [role], cause=cause)
    return role.status == stages.GATED_OUT


def _mode_after(held: str | None, work_mode: str | None) -> str | None:
    """The way of working the gate reads (`held`) once the analyst's is put where there is none (as `analysis.submit`
    does)."""
    if work_mode and (held or "").strip().lower() in ("", "unknown"):
        return work_mode
    return held


def problem(role: Opportunity, rules: gating.Rules, place: str | None, work_mode: str | None, text: str | None,
            not_stated: bool, copy=None) -> str | None:
    """Why the analyst's findings are refused for the place, or None: the work location gate would still be undecided
    on what the role would hold (the analyst's `place`, else what the text's own labels state; its cleansed copy's
    stated facts as the gate reads them, `copy`), and the analyst did not say the posting gives no place."""
    if not_stated:
        return None
    subject = _subject(role, rules, copy)
    mode = _mode_after(subject.work_mode, work_mode)
    labelled = stated(text)
    where = next((p for p in (place, labelled) if fills(subject.location, mode, p)), None)
    if not undecided(role, rules, where, mode, copy):
        return None
    held = " · ".join(x for x in (subject.location, mode) if x) or "no place"
    return (f"the work location gate can't be decided on what this role holds ({held}): send `location` as the "
            "posting states where the work is (the city and state, or each place it lists) and `work_mode`, or "
            "`place_not_stated: true` when neither the posting nor the employer's page says where the work is")


def ask_first(role: Opportunity, rules: gating.Rules, text: str | None, copy=None) -> str | None:
    """What the hand-out tells the analyst when the work location gate is undecided on what the role holds: send the
    place in the first submission. A submission without it is refused and re-sent whole, which costs a
    tenth of a night's cost or more. None when the
    role's place is decided, or the posting's own labels state it."""
    if problem(role, rules, None, None, text, False, copy) is None:
        return None
    held = " · ".join(x for x in (role.location, (role.move_type or "").strip()) if x and x.lower() != "unknown")
    return (f"This role's place is undecided ({held or 'no place held'}). Your first submission must carry "
            "`location` (where the posting states the work is: the city and state, or each place it lists) and "
            "`work_mode` (remote, hybrid or onsite), or `place_not_stated: true` when neither the posting nor the "
            "employer's page says where the work is. A submission without one is sent back whole.")


async def recheck(session: AsyncSession, owner: str, role: Opportunity, *, cause: str) -> str:
    """Every gate again on the role's facts before the writer drafts it: the place its posting states put on it first
    (`fill`). Returns `GATED` (it fails a gate and is now Gated out, with the reason, its packet kept), `SENT_BACK`
    (the place is still undecided, the role holds its posting's text and its research never answered the place: it
    goes back to the analyst, once), `PASSED` or `LEFT` (a role no re-run may move: moved by hand, or not in a stage
    the gates move). Services flush; the caller commits."""
    from app.jobsearch.gating import regate  # the engine's work: loaded when a recheck runs, never at the top
    from app.jobsearch.research import analysis  # analysis reads this module; imported here, never at the top

    if role.status not in regate.MOVABLE or role.status == stages.GATED_OUT \
            or (await placement.one(session, owner, role, placement.Purpose.GATES)).held:
        return LEFT
    text = (await posting_text.held(session, role)).text
    if place := fill(role, text):
        await trail.record(session, owner, event="place_stated", actor="app", who=trail.APP_WHO,
                           link=("opportunity", role.id),
                           sentence=f"The posting states where the work is: {place}.",
                           detail="Read from the posting's own location line; the work location gate is decided on it.")
    await settle.roles(session, [role], cause=cause)
    if role.status == stages.GATED_OUT:
        return GATED
    copy = await cleansed.gate_copy_of(session, owner, role.id)
    if (text and PLACE_KEY not in (role.analysis or {}) and role.analysis_state == analysis.DONE
            and undecided(role, await gating.rules(session, owner), copy=copy)):
        await analysis.start(session, owner, role)
        await trail.record(
            session, owner, event="research_requeued", actor="app", link=("opportunity", role.id),
            sentence=f"Sent {role.title} at {role.company} back to the analyst: the work location gate is undecided "
                     "and the posting may say where the work is.",
            detail="The analyst answers the place, or says the posting does not state it. The role keeps its stage "
                   "and its packet; it is drafted once the gates are decided.")
        await session.flush()
        return SENT_BACK
    await session.flush()
    return PASSED
