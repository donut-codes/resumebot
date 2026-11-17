"""The gate check (docs/PRD.md 39.11): one check over what is stored for a posting or a role, used wherever a gate runs.

The gates code can decide are the work location, the lowest pay, a posting's age, an employer the owner never works
for (a gated employer's record, or a name on the Hard pass list), a site the owner avoids, an employer fact the
record holds and a sentence the analyst's classes decide. A sentence rule a reader answers is checked here too, on
the answer a role holds. Each is a line of `results`, cheapest first: the site, the employer, the work location
(All's, then the role's own tab), the lowest pay (the same), then the owner's lines in the order they are listed.

The same check runs when a board is read (`sift.py`), when a title check names a tab (`titles.py`), when a reader's
report is stored (`actions.store`), when a role is settled (`settle.roles`: after research, when its classes are
corrected, when its place or an employer's record changes) and when Hard pass is saved, an employer is gated or
ungated, or a site to avoid is added or removed (`regate.py`). The role page's list of gates is the same lines
(`role_gates.gate_results`), so what it shows and what stops a role cannot differ. `screen_one_role.bpmn` decides the
reader's report in the round with the same comparisons over the same facts (`gates.role_facts`); `store` records that
decision with the structure of this check.

A gate passes until it is proven failed (docs/PRD.md 30.2): a place the posting does not make clear, pay it does not
post, an employer fact nobody has found and a sentence nobody has answered are `unknown` and stop nothing. A place the
posting names with no way of working is a fail only when it fails remote, hybrid and in person alike against the
owner's own place rules (`gates.fails_every_way`, the owner, 2026-10-23): no missing fact could rescue it. A way of
working nobody states is no longer a missing fact once the posting has been read whole (the owner, 2026-11-05): the work
location line judges the subject as in person and says so
(`way_assumed`, docs/PRD.md 30.2). It is judged and never stored, so a stated way is never replaced and a later one
replaces it.

The check itself reads no database: `results` and `check` work over a `Subject` and `Rules`. `rules` reads the
owner's employers, sites and versions once for a whole run, and `Rules.of` builds the same from a profile alone."""

import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import make_transient_to_detached

from app.jobsearch import judged, stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import freshness, scoring
from app.jobsearch.gating import sentence_gates
from app.jobsearch.measures import dials, distance, employer_name
from app.jobsearch.measures.pay_place import (
    ASSUMED_WAY,
    ASSUMED_WAY_WORDS,
    Gates,
    age_in_days,
    as_date,
    fails_every_way,
    hourly_part_time,
    is_excluded,
    location_ok,
    parse_comp_text,
    pay_check,
    posted_pay_verdict,
    says_no_place,
    states_a_way,
    text_states_a_way,
)
from app.jobsearch.models import Employer, Opportunity
from app.jobsearch.postings.record import CODE, HAIKU, LISTING, SONNET
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.gates import compensation_says, gate_says, location_says
from app.jobsearch.profile.schema import Profile
from app.jobsearch.profile.views import engine_gates
from app.jobsearch.row_digest import over, row_text
from app.jobsearch.states import EMPLOYER_LINE, StopGate, StopStep
from app.sites import avoid
from app.timeutil import owner_today

#: The longest id a stop keeps (`js_postings.stop_gate_id`, `js_opportunities.screened_out_gate_id`).
ID_CHARS = 80
#: What a line of the check says of a gate: it holds, it fails, or it cannot be told yet.
PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"
#: A dial's Must whose fact is not stated yet (docs/PRD.md 46.7): the role is held, shown with the fact it waits for,
#: and never stopped or counted as screened out.
WAITS = "waits"
#: The line id of the employer gate (`states.EMPLOYER_LINE`).
EMPLOYER = EMPLOYER_LINE
#: What a role's `gate_facts["state"]` says of where the step that reads a posting for the gate facts is with it
#: (`gate_facts.SETTLED`, `PENDING`, `GATED`): read and settled, a Haiku read still to come, or stopped by a gate.
FACTS_SETTLED, FACTS_PENDING, FACTS_GATED = "settled", "pending", "gated"
#: The mark in `gate_facts` that the step finished a read with no ask for the way of working outstanding (every ask
#: answered or none needed); its value is whether the posting's copy was whole and said nothing of a way then.
FACTS_WAY = "way"
#: The states of a role's research in which the gate-facts step still reads it (`gate_facts.OPEN_STATES`).
STEP_STATES = (None, "waiting", "no_text", "failed")

#: Which gate a line of the owner's (`other`) is, by its kind; a sentence is a rule.
_GATE_OF_KIND = {"employer_fact": StopGate.EMPLOYER, "posting_age": StopGate.AGE, "job_fact": StopGate.RULE}


@dataclass(frozen=True)
class Subject:
    """What the gates read of a posting or a role, all of it stored. A posting has the addresses a site to avoid is
    matched on; a role has the reader's answers and the analyst's research. `employer` is the record the employer's
    own gate and the employer-fact lines read."""

    company: str | None
    location: str | None
    work_mode: str | None
    comp: str | None
    comp_min: int | None
    comp_max: int | None
    posted: date | None
    found: date | None
    target_role: str | None = None
    links: tuple = ()
    employer: Employer | None = None
    answers: list | None = None
    analysis: dict | None = None
    #: What the gate-facts step stored of the posting, fact by fact (`Opportunity.gate_facts["facts"]`): the years of
    #: experience asked, the degree, a clearance (`posting_facts.Extracted.facts`). A job dial reads them.
    stored: dict | None = None
    #: The posting's or role's title, which the Title words dial reads (docs/PRD.md 46.4).
    title: str | None = None
    #: What `posted` or `found` was counted from (`freshness.PostedDay.basis`: the employer's date, the day the app
    #: first saw a posting, the day the role was found), which the sentence that says why a role was stopped on its age
    #: names. None for a subject built by hand, which names nothing.
    age_from: str | None = None
    #: Whether every reader of a way of working has had the posting's whole text and found none (the cleaned copy is
    #: whole and says nothing of remote or hybrid work, and the step that reads a posting for its gate facts is done
    #: with it, its Haiku read included), so a way of working the subject does not state is one the posting does not
    #: state. Only then does the work location gate assume the role is on site (`way_assumed`, the owner, 2026-11-05); a
    #: subject built from a listing alone (a posting, a report) has not been read and assumes nothing, as an unknown
    #: passes.
    way_read: bool = False
    #: Whether the role is already in play: researched (`analysis_state` done) or Ready to Launch. The Main work read
    #: is stored for such a role but never moves it (`job_facts` gives it no `main_work` fact).
    in_play: bool = False


@dataclass(frozen=True)
class Line:
    """One gate for one subject: its id (the page's, or the owner's), kind, sentence, result (`pass`, `fail`,
    `unknown`), the fact it rests on and who checks it. `gate` is the kind of stop it makes when it fails. `short` is
    the sentence a posting it stops is given and `reason` the one a role is, when they are not made from `says` and
    `fact`. `step` is the step that made the stop when it is not the one `checked_by` implies: a sentence the analyst
    answered after research is the research's, though the page lists it with the sentences answered from a posting."""

    id: str
    kind: str
    says: str
    result: str
    fact: str
    checked_by: str
    gate: StopGate
    short: str | None = None
    reason: str | None = None
    step: StopStep | None = None

    def as_dict(self) -> dict:
        """The line as the role page lists it."""
        return {"id": self.id, "kind": self.kind, "says": self.says, "result": self.result, "fact": self.fact,
                "checked_by": self.checked_by}


@dataclass(frozen=True)
class Stop:
    """The first gate that fails, as data (docs/PRD.md 39.12): the step that made the stop, the gate, the id of the
    line and the sentence the owner reads, in the words a posting is given and the words a role is. `says` is the
    gate's own sentence and `fact` what it was judged on."""

    step: StopStep
    gate: StopGate
    gate_id: str
    says: str
    fact: str
    posting_words: str
    role_words: str


def stop_of(line: Line) -> Stop:
    """The stop a failed line makes. A sentence the reader answered is the reader's step, one the analyst answered after
    research is the research's; every other gate is a code gate's (a sentence decided from the analyst's classes is the
    app's)."""
    step = line.step or (StopStep.READER if line.checked_by == "reader" else StopStep.GATE)
    role_words = line.reason or (line.says.rstrip(".") + (f" ({line.fact})." if line.fact else "."))
    return Stop(step=step, gate=line.gate, gate_id=line.id[:ID_CHARS], says=line.says, fact=line.fact,
                posting_words=line.short or line.says, role_words=role_words)


class EmployerIndex:
    """The owner's employer records, found the way `employers.find` finds one: by id, then a name's key, then an
    alias's."""

    def __init__(self, rows: Iterable[Employer]) -> None:
        rows = list(rows)
        self.rows = rows
        self.by_id: dict[uuid.UUID, Employer] = {e.id: e for e in rows}
        self.by_key: dict[str, Employer] = {}
        self.by_alias: dict[str, Employer] = {}
        for e in rows:
            self.by_key.setdefault(e.key, e)
        for e in rows:
            for alias in e.aliases or []:
                if key := employers.key_of(alias):
                    self.by_alias.setdefault(key, e)

    def find(self, name: str | None) -> Employer | None:
        key = employers.key_of(name)
        return (self.by_key.get(key) or self.by_alias.get(key)) if key else None

    def of(self, employer_id: uuid.UUID | None, company: str | None) -> Employer | None:
        """The record a posting or a role is at: the one its link names, else the one its company's name does."""
        return (self.by_id.get(employer_id) if employer_id else None) or self.find(company)

    def gated_keys(self) -> frozenset[str]:
        """The keys of every name and alias of the employers the owner gated, however a posting writes the company."""
        names = ((e.name, e.key, *(e.aliases or [])) for e in self.rows if e.gated)
        return frozenset(key for group in names for n in group if (key := employers.key_of(n)))


@dataclass
class Rules:
    """What the gates are, read once for a run: the owner's profile and its code gates, the day, the employers gated by
    name, the sites avoided, the employer records, and the versions of Hard pass and What to look for in force (what a
    stop records)."""

    profile: Profile
    gates: Gates
    today: date
    gated: frozenset[str] = frozenset()
    avoided: frozenset[str] = frozenset()
    index: EmployerIndex = field(default_factory=lambda: EmployerIndex(()))
    hard_pass: int | None = None
    criteria: int | None = None
    _facts: dict = field(default_factory=dict, repr=False)

    @classmethod
    def of(cls, profile: Profile, *, today: date | None = None, avoided: Iterable[str] = (),
           rows: Iterable[Employer] = (), hard_pass: int | None = None, criteria: int | None = None) -> "Rules":
        """The rules from a profile and what the caller has to hand. With Company switched off no employer stops
        anything, as in `profile_views.engine_gates`."""
        index = EmployerIndex(rows)
        return cls(profile=profile, gates=engine_gates(profile), today=today or owner_today(),
                   gated=index.gated_keys() if profile.gates.checks_employers() else frozenset(),
                   avoided=frozenset(avoided), index=index, hard_pass=hard_pass, criteria=criteria)

    def facts(self, employer: Employer | None) -> dict:
        """The employer record's facts the gates compare (`employers.facts`), worked out once for each employer."""
        if employer is None:
            return {}
        if employer.id not in self._facts:
            self._facts[employer.id] = employers.facts(employer, self.today)
        return self._facts[employer.id]


#: The columns of an employer's record that the gates read, in the rules and everywhere a `Subject` carries the record
#: (`EmployerIndex` its id, key, name and aliases; `Rules.gated` and the gate on a record `gated`, `gated_reason` and
#: `name`; `employers.facts` the facts the employer lines compare). Found by listing every reader of `Rules.index` and
#: of `Subject.employer` (the gate-facts step, the re-gate, `stated_place`, the deck's gate pass, `placement`,
#: `from_posting`, `sift`, `settle`, `linkedin.compare`; `tests/jobsearch/test_rules_kept.py` runs each gate line on
#: rules built from these columns alone and on the whole records, and demands the same lines). The rules kept for the
#: next call hold these and no others: a column outside them raises (`DetachedInstanceError`) where a gate reads it
#: instead of being quietly old, and a change to one of the others (the board's `fetch_*`, `open_roles`, `read_*`, which
#: the night's reads rewrite for every employer) changes nothing here and costs no rebuild.
GATE_EMPLOYER_COLUMNS = (
    "id", "key", "name", "aliases", "gated", "gated_reason", "founded", "employees", "ownership", "ai_native",
    "industry", "sectors", "local", "work_style", "events", "sources")

async def _employers_digest(session: AsyncSession, owner: str) -> tuple[int, int, int]:
    """A digest (`row_digest`) of every employer record's `GATE_EMPLOYER_COLUMNS`, from one statement over the owner's
    rows: how many there are, and the exclusive-or of a 64-bit hash of each row's text under two seeds. It does not
    depend on the order of the rows, a changed, added or removed row changes it, and no row can be told from another by
    its place."""
    columns = [Employer.__table__.c[name] for name in GATE_EMPLOYER_COLUMNS]
    return over((await session.scalars(select(row_text(*columns)).where(Employer.owner == owner))).all())


async def _employer_rows(session: AsyncSession, owner: str) -> list[Employer]:
    """The owner's employer records with `GATE_EMPLOYER_COLUMNS` and no others, detached from every session (built from
    the columns, not loaded as the session's own instances, so nothing here is ever flushed or expunged from a caller's
    session). The rules built from them are shared by the callers that ask for them: nothing writes to them."""
    columns = [Employer.__table__.c[name] for name in GATE_EMPLOYER_COLUMNS]
    out = []
    for values in (await session.execute(select(*columns).where(Employer.owner == owner))).all():
        row = Employer(**dict(zip(GATE_EMPLOYER_COLUMNS, values, strict=True)))
        make_transient_to_detached(row)
        out.append(row)
    return out


@dataclass(frozen=True)
class _Kept:
    """The rules kept for an owner, with the profile they were built from (the one thing a caller gets a copy of)."""

    rules: "Rules"
    profile: Profile


_KEPT: profile_store.OwnerCache[_Kept] = profile_store.OwnerCache()


async def rules(session: AsyncSession, owner: str, profile: Profile | None = None, *, today: date | None = None
                ) -> Rules:
    """The owner's rules now: the profile in force, every employer record, the sites avoided and the two versions.

    With no profile given they are kept for the next call: the rules are a function of the four documents in force, the
    employer records' gate columns (`GATE_EMPLOYER_COLUMNS`), the sites avoided and the day, so each call asks the
    database for those (the rows' ids, a digest, the sites: three statements) and builds nothing when they are the ones
    the kept rules were built from (each call indexed every employer). A profile given is the caller's own, which may be
    an older or a
    changed one, so the rules for it are built fresh from every record as before. What is returned is a copy that
    shares the employer records and the index, which no caller writes to."""
    if profile is not None:
        rows = (await session.scalars(select(Employer).where(Employer.owner == owner))).all()
        return Rules.of(
            profile, today=today, avoided=await avoid.hosts(session, owner), rows=rows,
            hard_pass=await profile_store.newest_version(session, owner, "gates"),
            criteria=await profile_store.newest_version(session, owner, "criteria"))
    held = await profile_store.in_force(session, owner)
    digest = await _employers_digest(session, owner)
    avoided = await avoid.hosts(session, owner)
    day = today or owner_today()
    key = (held.ids, digest, avoided, day)
    kept = _KEPT.get(owner, key)
    if kept is None:
        async with _KEPT.building(owner):
            kept = _KEPT.get(owner, key)
            if kept is None:
                built = await profile_store.profile_of(session, owner, held)
                rows = await _employer_rows(session, owner)
                made = Rules.of(built, today=day, avoided=avoided, rows=rows, hard_pass=held.version_of("gates"),
                                criteria=held.version_of("criteria"))
                kept = _Kept(made, built)
                if await _employers_digest(session, owner) == digest:  # no record changed while they were read
                    _KEPT.put(owner, key, kept)
    return replace(kept.rules, profile=kept.profile.model_copy(deep=True))


# --- the subject: what is stored of a posting or a role ------------------------------------------------------------


def posting_subject(posting, employer: Employer | None, target_role: str | None = None) -> Subject:
    """A posting as the gates read it (also a row of the posting's columns, as the re-gate reads them). Its age is
    counted as a role's is (`freshness.posted_basis`) over the one posting: from its own date, held to the day the app
    first saw it, else from that day. The role's own gates apply to it once a tab's title check has named the role
    (`target_role`, else the one the posting holds)."""
    day = freshness.posted_basis(None, None, [(posting.posted, posting.first_seen)])
    return Subject(
        company=posting.company, location=posting.location, work_mode=posting.work_mode, comp=posting.comp,
        comp_min=posting.comp_min, comp_max=posting.comp_max, posted=day.posted, found=day.found,
        target_role=target_role or posting.target_role, links=(posting.url, posting.apply_url), employer=employer,
        title=posting.title, age_from=day.basis)


#: Who read a fact when it rests on the posting's own words, with the line it was read from (`posting_facts.Found.by`):
#: not a tag the listing carried.
_WORDS = (CODE, HAIKU, SONNET)


def _own_way_stands(role, copy) -> bool:
    """Whether the role's way of working stands over the copy's: the copy's is only the listing's tag (its mark's source
    is `listing`: the cleaner puts the listing's tag over what the text says, `cleanse.cleanse`), and the role's is the
    one the posting's own words state, read in code or by a model with the line it rests on (the role's stored fact
    `work_mode`, still the value the role holds). (2026-11-03): a
    posting that said "hybrid" in its own words was read as remote because its job system's tag said so. A tag against
    a tag, or against no stored reading, is the copy's as before."""
    mark = (getattr(copy, "marks", None) or {}).get("work_mode")
    if not isinstance(mark, dict) or mark.get("source") != LISTING:
        return False
    fact = ((getattr(role, "gate_facts", None) or {}).get("facts") or {}).get("work_mode")
    held = (getattr(role, "move_type", None) or "").strip().lower()
    return (isinstance(fact, dict) and fact.get("by") in _WORDS and bool((fact.get("quote") or "").strip())
            and str(fact.get("value") or "").strip().lower() == held)


def _step_reads(role) -> bool:
    """Whether the gate-facts step still reads this role (`gate_facts.process`): it is in a stage before research and
    its research is not done. The step reads nothing else; a role past it is left to its own gates."""
    return (getattr(role, "status", None) in stages.PRE_READY
            and getattr(role, "analysis_state", None) in STEP_STATES)


def facts_read(role) -> bool:
    """Whether every reader of a way of working is done with a role, as its stored read says: the step that reads its
    posting for the gate facts finished a read, settled or stopped by a gate (`FACTS_SETTLED`, `FACTS_GATED`), with no
    ask for the way outstanding (the mark `FACTS_WAY`, which holds whether the copy was whole and said nothing of a way
    when it read; none for a read still to come, a role stopped before its asks, or one read before the mark existed:
    the step reads those again, `gate_facts._due`). A role the step no longer reads (researched, or in Ready to Launch)
    that holds a read with no mark was read by the analyst, which no Haiku read could improve on: it counts. Found in
    review (2026-11-05): a state of `settled` alone said nothing of the asks it was settled with, and a `gated` one
    stopped before any ask."""
    held = getattr(role, "gate_facts", None) or {}
    if held.get("state") not in (FACTS_SETTLED, FACTS_GATED):
        return False
    if FACTS_WAY in held:
        return held[FACTS_WAY] is True
    return getattr(role, "status", None) in stages.PRE_APPLY and not _step_reads(role)


def whole_copy(copy) -> bool:
    """Whether a cleaned copy is of a whole posting: a partial one (a snippet, a cut-off text) says nothing of what the
    posting does not state (`record.py`: a partial record never fails a gate on an empty field)."""
    return copy is not None and not copy.partial


def silent_copy(copy) -> bool:
    """Whether a cleaned copy is of a whole posting that says nothing of a way of working: no word of hybrid or remote
    work anywhere in it, outside a remote that is denied (`pay_place.text_states_a_way`). Only a posting that does not
    say "remote" or "hybrid" is one that does not say it (the owner, 2026-11-05); a posting that does, in words code
    does not read as this job's way, is left to a later read, and passes meanwhile as an unknown does."""
    return whole_copy(copy) and not text_states_a_way(copy.text_clean)


def way_assumed(s: Subject) -> bool:
    """Whether the work location gate judges this subject as on site for want of a stated way of working: its posting
    was read whole by every reader and said nothing of one (`Subject.way_read`) and neither its way of working nor the
    words of its place state one (`pay_place.states_a_way`). A stated way is never replaced, and a later one replaces
    the assumption, since nothing is stored: the next judgement of the same subject reads the way it holds then."""
    return s.way_read and not states_a_way(s.work_mode, s.location)


def role_subject(role, employer: Employer | None, copy=None, posted: freshness.PostedDay | None = None,
                 read: bool | None = None) -> Subject:
    """A role as the gates read it: its own facts, the reader's answers and the analyst's research. A site to avoid is
    a posting's gate (docs/PRD.md 30.3), so a role carries no links to judge. With `copy`, the role's cleansed copy
    (`cleansed.py`: of the posting the owner's rule picks for a role with several, the employer's own, else the newest,
    `gate_copy.py`), the hard gates on stated facts read what it states (the employer, the places, the way of working
    and the pay band) in place of the role's own; a fact it does not state is the role's (a place that says nothing, a
    count such as "3 Locations", is no place stated), and one neither holds is unknown, which passes. The one exception
    is the way of working: a listing's tag does not lift the one the posting's own words state (`_own_way_stands`).

    The days since it was posted are never the copy's. The copy is of one posting of the role (it was whichever had the
    most text until 2026-11-04, so a role with a dated posting on the employer's system and an undated one of the same
    req on a job site was one age on the night one copy won and another on the night the other did), and the age is the
    role's, not one posting's. They are `posted`, the role's posted day
    (`freshness.posted_basis` over its postings, read by `freshness.posted_by_role`): one number for every gate, the
    score and the role page. A caller that reads only lines that do not depend on the age (the work location) may leave
    it out; the role's own columns then give the day.

    `read` says whether the posting has been read whole by every reader of a way of working and said none
    (`Subject.way_read`: the work location gate then assumes on site where none is stated, `way_assumed`). Left out, it
    is true when the copy is whole and says nothing of remote or hybrid work and the gate-facts step is done with the
    role, no ask for the way outstanding (`silent_copy`, `facts_read`): a role with no copy, a partial one, a copy that
    speaks of a way, or a read still to come is not assumed to be anything. The step itself says it when it has just
    finished a read the role does not hold yet."""
    day = posted or freshness.posted_basis(role.posted, role.found, ())
    subject = Subject(
        company=role.company, location=role.location, work_mode=role.move_type, comp=role.comp, comp_min=None,
        comp_max=None, posted=day.posted, found=day.found, target_role=getattr(role, "target_role", None),
        employer=employer, answers=getattr(role, "gate_answers", None), analysis=getattr(role, "analysis", None) or {},
        stored=(getattr(role, "gate_facts", None) or {}).get("facts"), title=getattr(role, "title", None),
        age_from=day.basis, way_read=(facts_read(role) and silent_copy(copy)) if read is None else read,
        in_play=(getattr(role, "analysis_state", None) == "done"
                 or getattr(role, "status", None) == stages.READY_TO_LAUNCH))
    if copy is None:
        return subject
    # A place that says nothing (a board's count, "3 Locations") states no place, so the role's own stands: the copy of
    # the posting the gates read may hold only the count while the role holds the places themselves.
    places = [place for place in (copy.locations or []) if not says_no_place(place)]
    stated = {"company": copy.employer, "location": "; ".join(places) or None, "work_mode": copy.work_mode}
    if copy.pay_min or copy.pay_max:
        stated.update(comp=None, comp_min=copy.pay_min, comp_max=copy.pay_max)
    if _own_way_stands(role, copy):
        del stated["work_mode"]
    return replace(subject, **{k: v for k, v in stated.items() if v})


def report_subject(raw: dict, employer: Employer | None, target_role: str | None) -> Subject:
    """A role a reader reported (not stored yet), as the gates read it: it has no posting, so its age is counted from
    its own date, else the day it was found (`freshness.posted_basis`)."""
    day = freshness.posted_basis(as_date(raw.get("posted")), as_date(raw.get("found")), ())
    return Subject(
        company=raw.get("company"), location=raw.get("location"), work_mode=raw.get("work_mode"),
        comp=raw.get("comp"), comp_min=raw.get("comp_min"), comp_max=raw.get("comp_max"),
        posted=day.posted, found=day.found, target_role=target_role,
        employer=employer, answers=raw.get("gate_answers"), title=raw.get("title"), age_from=day.basis)


def posted_band(s: Subject) -> tuple[int | None, int | None]:
    """The pay the subject states, as (low, high) a year: its figures, else what its pay line says."""
    if hourly_part_time(s.title, s.comp):
        return None, None  # a fractional or part-time hour is no full-time year, whatever figure was stored (PRD 30.4)
    low, high = s.comp_min, s.comp_max
    if low is None and high is None:
        low, high = parse_comp_text(s.comp)
    return (int(low) if low else None), (int(high) if high else None)


def employer_named(company: str | None, stored: dict) -> tuple[str, str | None]:
    """Whether a posting names its employer, as ("yes", None) or ("no", why): what the posting's own text said when it
    said something the name does not (a stored `employer_named`, `posting_facts.employer_named_from`), else the name
    the listing gives (`employer_name.hidden`)."""
    found = stored.get("employer_named")
    if isinstance(found, dict) and found.get("value") in ("yes", "no"):
        said = found["value"]
        return said, (found.get("quote") or None) if said == "no" else None
    why = employer_name.hidden(company)
    return ("no", why) if why else ("yes", None)


def job_facts(s: Subject, profile: Profile, today: date) -> dict:
    """The facts of a posting or a role that a job dial reads (docs/PRD.md 46.4), each only when it is stated: the years
    of experience asked, the degree required, a clearance, the most percent of the time it travels (all read from the
    posting's text and stored), the top of the posted pay, the way of working, the days since it was posted, and the
    miles from the user's home place to the posting's place (none for a remote posting, an unknown place or no known
    home: `distance.py`), its title, which is always stated when there is one, whether it names its employer, which
    always is (`yes`, or `no` with `employer_named_why`), and whether the job leads business development, only when a
    model's read said `yes` (with `leads_bd_why`, the sentence it rests on) or `no`: a `no` code wrote is left out. The
    role's main work (`main_work` with `main_work_why`) is one of the owner's words a model's read quoted, never
    `none`."""
    stored = s.stored or {}

    def held(name: str):
        found = stored.get(name)
        return found.get("value") if isinstance(found, dict) else found

    out: dict = {}
    if (title := (s.title or "").strip()):
        out["title"] = title
    try:
        if (years := held("experience_years")) not in (None, ""):
            out["experience_years"] = int(years)
    except (TypeError, ValueError):
        pass
    for name in ("degree_required", "clearance"):
        if value := held(name):
            out[name] = str(value).lower()
    try:
        if (travel := held("travel_percent")) not in (None, ""):
            out["travel_percent"] = float(travel)
    except (TypeError, ValueError):
        pass
    out["employer_named"], why = employer_named(s.company, stored)
    if why:
        out["employer_named_why"] = why
    found = stored.get(dials.LEADS_BD)
    by = found.get("by") if isinstance(found, dict) else None
    # Unknown is no fact: nothing stored says it. Nor is a `no` that code wrote: it says only that no sentence matched a
    # word list (`posting_facts.leads_bd_from_text`, a posting about "revenue growth" matches none), not that a reader
    # found the job is not that, so a Must on Yes cannot stop a role on it (docs/PRD.md 46.4). It stays stored.
    if (leads := held(dials.LEADS_BD)) == "yes" or (leads == "no" and by != CODE):
        out[dials.LEADS_BD] = leads
        if leads == "yes" and isinstance(found, dict) and found.get("quote"):
            out[f"{dials.LEADS_BD}_why"] = found["quote"]
    # Main work: only a model's read of one of the owner's words, with its quote. `none`, unknown and code's own
    # none are no fact; a role already in play keeps the fact stored and the gate never sees it.
    said = stored.get(dials.MAIN_WORK)
    if (isinstance(said, dict) and said.get("by") != CODE and not s.in_play
            and said.get("value") not in (None, "", "none", "unknown") and said.get("quote")):
        out[dials.MAIN_WORK] = str(said["value"])
        out[f"{dials.MAIN_WORK}_why"] = str(said["quote"])
    if (high := posted_band(s)[1]):
        out["pay_top"] = high
    mode = (s.work_mode or "").strip().lower().replace(" ", "_")
    mode = "remote" if mode == "restricted_remote" else mode
    if mode in dials.MODES:
        out["work_mode"] = mode
    if (age := age_in_days(s.posted, s.found, today)) is not None:
        out["posted_days"] = age
    if mode != "remote" and (home := dials.home_place(profile.gates)) is not None:
        if (miles := distance.between(home, s.location)) is not None:
            out["distance_miles"] = round(miles, 1)
    return out


# --- the check -----------------------------------------------------------------------------------------------------

#: What an age was counted from, in the words a sentence about it uses (`Subject.age_from`).
_COUNTED_FROM = {freshness.POSTED: "the employer's date", freshness.SEEN: "the day the app first saw it",
                 freshness.FOUND: "the day the role was found"}


def _counted_from(fact: str, s: Subject) -> str:
    """`fact`, a sentence about the subject's age, with what it was counted from when the subject says."""
    words = _COUNTED_FROM.get(s.age_from or "")
    return f"{fact}, counted from {words}" if words else fact


def _scope_lines(s: Subject, scope, mine: Gates, prefix: str, *, place: str, assumed: bool, low, high, worked,
                 want_location: bool = True, want_pay: bool = True) -> Iterator[Line]:
    """The work location and lowest pay of one scope: All's, then (the id carries the role) the role's own on top
    of them (docs/PRD.md 31.9). Each is judged alone, so the page says which one stopped the subject."""
    if want_location and scope.checks_location():
        ok, fact = location_ok(s.location, s.work_mode, mine), place
        if ok is None and fails_every_way(s.location, s.work_mode, mine):
            # No way of working stated, and the place fails remote, hybrid and in person alike: no missing fact can
            # rescue it, so it is no unknown (the owner, 2026-10-23), and the assumption is not what stops it.
            ok, fact = False, (f"{s.location} · way of working not stated; it fails whether remote, hybrid or in "
                               "person")
        elif assumed:
            # A posting read whole that states no way of working is judged as in person (the owner, 2026-11-05); the
            # line says so, so a stop on it names the assumption it rests on.
            ok = location_ok(s.location, ASSUMED_WAY, mine)
            fact = place if ok is None else f"{place} · {ASSUMED_WAY_WORDS}"
        yield Line(f"location{prefix}", "location", location_says(scope.location),
                   {True: PASS, False: FAIL, None: UNKNOWN}[ok], fact, "app", StopGate.LOCATION)
    if want_pay and scope.checks_pay():
        result, figure, source = pay_check(low, high, mine, worked)
        settled = posted_pay_verdict(low, high, mine) if worked is None and result == FAIL else None
        if settled is not None and settled[0] == "fail":  # the posted range alone settled it, before any ask
            fact = settled[1]
        else:
            fact = (f"the {source} ${figure:,}" if figure is not None
                    else "no pay posted and no ask worked out yet")
        yield Line(f"compensation{prefix}", "compensation", compensation_says(scope.compensation), result, fact,
                   "app", StopGate.COMPENSATION)


def location_undecided(s: Subject, rules: Rules) -> bool:
    """Whether any work location line of `results` is not decided (unknown): All's, and the role's own on top of it.
    The same answer as `any(line.kind == "location" and line.result == UNKNOWN for line in results(s, rules))`, worked
    out from the location part alone: no pay, no posted band, no job facts, none of the owner's other lines."""
    g, gates, target = rules.profile.gates, rules.gates, s.target_role
    place = " · ".join(x for x in (s.location, s.work_mode) if x) or "no place stated"
    scope_args = {"place": place, "assumed": way_assumed(s), "low": None, "high": None, "worked": None,
                  "want_pay": False}
    if any(line.result == UNKNOWN for line in _scope_lines(s, g, gates, "", **scope_args)):
        return True
    own = g.role(target)
    return own is not None and target in gates.by_role and any(
        line.result == UNKNOWN for line in _scope_lines(s, own, gates.by_role[target], f":{target}", **scope_args))


def results(s: Subject, rules: Rules) -> Iterator[Line]:
    """Each of the owner's gates for this subject: passed, failed, or not decided (the posting did not say, the reader
    judges it, or nobody has found the fact yet), with the fact it rests on and who checks it. The pay gate is judged
    on the ask the analyst worked out once there is one, and a rule set to follow the analyst's classes on how many
    of the required items are in its class (unknown until the role is researched)."""
    g, gates, target = rules.profile.gates, rules.gates, s.target_role
    check_employers = g.checks_employers()

    if rules.avoided and (host := avoid.matching(s.links, rules.avoided)):
        yield Line(host, "site", "A site you avoid.", FAIL, host, "app", StopGate.SITE, short=avoid.REASON)

    # With Company switched off, no employer stops a subject; every other off section drops its lines the same way.
    on_record = check_employers and s.employer is not None and bool(s.employer.gated)
    by_name = check_employers and bool(rules.gated) and employers.key_of(s.company) in rules.gated
    if is_excluded(s.company, gates) or on_record or by_name:
        gated = on_record or by_name
        why = None
        if gated:
            record = s.employer if on_record else None
            why = f"employer gated: {(record.gated_reason or record.name) if record else s.company}"
        yield Line(EMPLOYER, "employer", "An employer you never work for.", FAIL,
                   f"{s.company}: gated on its page" if gated else (s.company or ""), "app", StopGate.EMPLOYER,
                   short="an employer you never work for", reason=why)

    low, high = posted_band(s)
    worked = ((s.analysis or {}).get("ask") or {}).get("ask")
    place = " · ".join(x for x in (s.location, s.work_mode) if x) or "no place stated"
    assumed = way_assumed(s)

    scope_args = {"place": place, "assumed": assumed, "low": low, "high": high, "worked": worked}
    yield from _scope_lines(s, g, gates, "", **scope_args)
    own = g.role(target)
    if own is not None and target in gates.by_role:
        yield from _scope_lines(s, own, gates.by_role[target], f":{target}", **scope_args)

    age = age_in_days(s.posted, s.found, rules.today)
    owned = g.lines_for(target)
    jobs = job_facts(s, rules.profile, rules.today) if any(o.kind == "job_fact" for o in owned) else {}
    for other in owned:
        result, fact, by = UNKNOWN, "", "app"
        step = None
        gate = _GATE_OF_KIND.get(other.kind, StopGate.RULE)
        if other.kind == "employer_fact" and dials.is_dial(other.fact, other.op):
            result, fact = scoring.dial_gate(other, rules.facts(s.employer))
        elif other.kind == "employer_fact":
            result, fact = scoring.employer_gate(other, rules.facts(s.employer))
        elif other.kind == "job_fact":
            result, fact = scoring.dial_gate(other, jobs)
            if other.fact == "posted_days" and result != WAITS:
                fact = _counted_from(fact, s)
        elif other.kind == "posting_age" and other.days:
            if age is not None:
                result = FAIL if age > other.days else PASS
                fact = _counted_from(f"{age} days old", s)
        elif other.follows_classes():
            # Decided by the app (`by` stays "app") from the analyst's classes of the required items, never from a
            # reader's answer; unknown until the role is researched.
            required = (s.analysis or {}).get("required") or []
            result, fact = scoring.class_gate(other, [r.get("class") for r in required])
        elif other.kind == "rule":
            # The reader's answer from the posting and the line it gave; none yet (a role no reader judged) is unknown.
            by = "reader"
            # The answer to the rule's words as they are now: a rule reworded since is a new question, unanswered (and
            # so passing) until a reader answers it (docs/PRD.md 39.13).
            if given := sentence_gates.answer_of(s.answers, other):
                result, fact = given["answer"], given.get("evidence") or ""
                if given.get("by") == sentence_gates.BY_ANALYST:
                    step = StopStep.RESEARCH
                elif given.get("by") == sentence_gates.BY_FACTS:
                    by, step = "app", StopStep.GATE
        said = None
        if result == FAIL and other.kind == "job_fact" and other.fact == "title" and jobs.get("title"):
            # Title words: name the word that hit, not the whole list the gate's sentence holds.
            if hits := dials.title_hits(str(jobs["title"]), other.value, other.except_words):
                said = dials.title_hit_words(hits)
        yield Line(other.id, other.kind, gate_says(other), result, fact, by, gate, short=said, reason=said, step=step)


def waiting_dials(s: Subject, rules: Rules) -> list[str]:
    """What the owner's Musts on a dial wait for, in words ("the years of experience asked"), in the order the lines
    are owned; empty when none waits. The same answer as the `WAITS` lines of `results`, worked out from the dial lines
    alone: the deck asks it of every role on every turn, and the site, employer, location, pay and age checks of the
    full results can never produce a wait."""
    owned = rules.profile.gates.lines_for(s.target_role)

    def never_waits(other) -> bool:
        # A words dial never waits: a title is always known, and Main work passes when the posting does not show it.
        dial = dials.BY_FACT.get(other.fact or "")
        return dial is not None and dial.kind == "words"

    jobs = (job_facts(s, rules.profile, rules.today)
            if any(o.kind == "job_fact" and not never_waits(o) for o in owned) else {})
    waiting: list[str] = []
    for other in owned:
        if other.kind == "employer_fact" and dials.is_dial(other.fact, other.op):
            result, fact = scoring.dial_gate(other, rules.facts(s.employer))
        elif other.kind == "job_fact" and not never_waits(other):
            result, fact = scoring.dial_gate(other, jobs)
        else:
            continue
        if result == WAITS:
            waiting.append(fact.removeprefix("waiting for "))
    return waiting


#: The order gates are tried in when more than one fails, cheapest first, as `screen_one_role.bpmn` tries them: a site
#: the owner avoids, the employer, the work location (All's and the role's), the lowest pay, the posting's age, then
#: the owner's other lines in the order they are listed. `results` keeps the page's order (each scope's pair).
_PHASE = {"site": 0, "employer": 1, "location": 2, "compensation": 3, "posting_age": 4}


def check(s: Subject, rules: Rules) -> Stop | None:
    """The first gate that fails for this subject (in the order above), as data, or None when it passes every one.
    The same answer wherever it is asked: when a board is read, when a title check names a tab, when a reader's report
    is stored, after research and when Hard pass is saved (docs/PRD.md 39.11)."""
    return first_stop(results(s, rules))


def first_stop(lines: Iterable[Line]) -> Stop | None:
    """The stop of the cheapest failed line (the order above), or None when none failed."""
    failed = [line for line in lines if line.result == FAIL]
    return stop_of(min(failed, key=lambda line: _PHASE.get(line.kind, 5))) if failed else None


# --- recording a stop ----------------------------------------------------------------------------------------------

#: A posting with no stop: what `filtered_by` and the three columns beside it hold.
POSTING_CLEARED = {"filtered_by": None, "stop_gate": None, "stop_gate_id": None, "stop_hard_pass": None}
#: A role with no stop: what the six columns of a stop hold.
ROLE_CLEARED = {"screened_out_reason": None, "screened_out_by": None, "screened_out_step": None,
                "screened_out_gate_id": None, "screened_out_hard_pass": None, "screened_out_criteria": None}


def posting_stop_values(stop: Stop, rules: Rules, *, criteria: int | None = None) -> dict:
    """The columns of a posting a gate stopped: its sentence, the step, the gate, the line, and the versions of What
    to look for (`criteria`, else the one in force) and Hard pass it was judged under. It waits for no one, so it has
    no route."""
    return {"filtered_by": stop.posting_words, "judged_by": judged.GATE,
            "judged_by_version": rules.criteria if criteria is None else criteria,
            "stop_gate": stop.gate.value, "stop_gate_id": stop.gate_id, "stop_hard_pass": rules.hard_pass,
            "route": None, "route_why": None}


def role_stop_values(stop: Stop, rules: Rules) -> dict:
    """The columns of a role a gate stopped, without its status: the sentence, the gate, the step, the line and the
    versions."""
    return {"screened_out_reason": stop.role_words, "screened_out_by": stop.gate.value,
            "screened_out_step": stop.step.value, "screened_out_gate_id": stop.gate_id,
            "screened_out_hard_pass": rules.hard_pass, "screened_out_criteria": rules.criteria}


def stop_role(role: Opportunity, stop: Stop, rules: Rules) -> str:
    """Stop the role at the gate that failed (docs/PRD.md 39.11): Gated out, with that gate, its line and the versions
    it was judged under kept as data (39.12). Returns the reason, the sentence a history line says."""
    role.status = stages.GATED_OUT
    for column, value in role_stop_values(stop, rules).items():
        setattr(role, column, value)
    return stop.role_words
