"""Gates first (the owner, 2026-10-19): the cheap gates are decided on the
posting's own facts before anyone pays to analyse the role or to open it in a browser.

Place, pay and travel are facts a posting states in its header and condition lines, so a role should not be gated out
only after a full analysis. The order of work is:

1. **Read the facts** (`process`), as soon as a role has posting text and again whenever its text, place, way of
   working, pay or employer changes (`fingerprint`): in code first (`posting_facts.extract`: the schema.org
   JobPosting a page embeds, the label lines and the sentences of the posting), each fact kept with the line it rests
   on. A role read under an older `READER` has only the facts the newer one added read once more, from its text.
2. **Decide** with the gate code that always decided (`gating.check`): a fail moves the role to Gated out with the
   reason and the evidence on its history; an unknown passes (the owner's rule). A place that fails in every way of
   working a posting could have (a town abroad, way of working not stated, remote only in the owner's country and in
   person only at their towns) is a fail, since no missing fact can rescue it: the one gate check decides it
   (`gates.fails_every_way`, switched on by the owner on 2026-10-23), so the re-gate and every other check agree.
3. **A short Haiku read** (`item`, `take_answers`) for what code cannot settle: one small session (a mode of the title
   session, `jobs.titles_next`) is handed just the posting's header and condition lines and answers the few facts
   with a quote each. A quote that is not in the posting is no evidence, so a fact without one is dropped.
4. **The order**: this runs before analysis is offered (the step "Read the gate facts" of `analyse_role.bpmn`, and a
   role whose Haiku read is still to come is not offered for `HOLD`, `analysis._eligible`), for a role in Fact
   checking on what its listing shows before a browser item is queued for it (`browser_queue._wanted`), and again
   after code sourcing brings more (`sourcing.sweep`, `posting_text.take`). A role gated out this way never reaches
   the analyst or the browser.
5. **Inside the analysis** the gates that need research (an employer fact) are checked again as soon as their facts
   exist: when a session is about to take the role, and when it asks for the master resume (`stop_if_failed`). The
   gates that need the analyst's classes run when its findings are taken, as before (`analysis.action_settle`).

6. **Leads business development** (the owner, 2026-11-05, docs/PRD.md 46.4) is read the same way, for the roles whose
   profile holds a line on that dial and for no one else (`bd_wanted`): code gives `no` to a posting with no sentence
   that speaks of selling, and a posting that has some waits for the short Haiku read, which is handed just the title
   and those sentences (`posting_facts.bd_lines`, ask `ASK_BD`) and whose `yes` stands on a quote of the posting or is
   dropped; `no` and `unknown` gate nothing a Must wants `no` of. Code's `no` is kept, so nothing is asked again, but it
   says only that a word list found no sentence, which is no read: no gate reads it (`check.job_facts`), so a Must on
   Yes stops a role only on a model's `no`. A role read before the dial was set carries no `bd` mark, so it is read once
   more from the text it holds when the dial is on (`_due`, the re-read branch of `process`): code's `no`, or the ask
   (a role still waiting for a read is asked it with what it was asked; a settled one only it), nothing else read
   again. A role researched or past Ready is left to its own gates.

7. **A way of working nobody states is on site** (the owner, 2026-11-05, docs/PRD.md 30.2). Once the posting is read
   whole, says nothing of remote or hybrid work in any words, and no read of it is to come, the work location gate
   judges a role that states no way of working as in person (`gating.way_assumed`), so a place that waited for the way
   is decided and a stop on it says "on-site assumed". The step asks the Haiku read for the way on the place as the
   posting states it, and whenever the assumed verdict would differ from it (`_way_asks`), and its own decisions say
   the read is done only when no ask is left (`process`) or the answer has come (`take_answers`). It stores that as the
   mark `way` (`gating.FACTS_WAY`: the copy was whole and said nothing of a way), which every other path reads
   (`gating.facts_read`). A role settled or stopped with no mark (read before it existed, or stopped before its asks) is
   read again for the way (`_way_due`), asked first, then judged. Nothing is stored as the way itself.

Priority 1 is the best data, so nothing here guesses: a fact stands on a quote of the posting, a role the owner moved by
hand is never moved, a role already researched is left to its own gates, and `ENABLED` is the switch (the tests turn it
off to see what the analyst does alone). Services flush; the route, tool or process step commits."""

import asyncio
import functools
import hashlib
import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import and_, func, or_, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlalchemy.sql.elements import ColumnElement

from app import db, jsonsql
from app.jobsearch import as_read, stages
from app.jobsearch.employers import core as employers
from app.jobsearch.fit import freshness
from app.jobsearch.gating import check as gating
from app.jobsearch.gating import posting_facts, stated_place
from app.jobsearch.gating.posting_facts import HAIKU, Extracted, Found, quote_in
from app.jobsearch.measures import dials
from app.jobsearch.measures.pay_place import is_part_time, names_a_town, parse_comp_text, posted_pay_verdict
from app.jobsearch.models import CleansedPosting, JobPosting, Opportunity
from app.jobsearch.pipeline import placement
from app.jobsearch.postings import cleansed, whole
from app.jobsearch.postings import text as posting_text
from app.jobsearch.profile.scoring import Group
from app.jobsearch.research import line as research_line
from app.jobsearch.work import hand_outs
from app.trail import service as trail

log = logging.getLogger(__name__)

#: The switch. Off, no role is read or stopped by this step and the order of work is the one before it.
ENABLED = True
#: The short Haiku read of a posting's header and condition lines (`item`, `take_answers`). ON (the owner, 2026-10-19,
#: after the Haiku comparison: about 1/30 of a cent a role in batches of 25, and it added correct place gates). A gate
#: decision from the read is made only when the posting states the way of working and the place together (`fill`): a
#: place with no stated way of working never fails a role ("an unknown passes"). Everything it does stands on a quote.
HAIKU_READS = True

#: What a role's `gate_facts` says of where it is: read and settled, a Haiku read still to come, or stopped by a gate.
SETTLED, PENDING, GATED = gating.FACTS_SETTLED, gating.FACTS_PENDING, gating.FACTS_GATED
#: What `process` answers.
CLEAR, WAITS, STOPPED, LEFT = "clear", "waits", "stopped", "left"
#: How long a role whose Haiku read is to come is held back from the analyst. The read is a small session in the
#: pool's next turn; past this the analyst takes the role as it always did (nothing waits on a model, and the gates run
#: after the research as before).
HOLD = timedelta(hours=3)
#: Roles one Haiku session is handed, how many times a role is handed before it is left, and how long a hand-out is
#: leased to its session.
BATCH = 25
HANDED_AT_MOST = 2
LEASE = timedelta(minutes=10)
#: The ids a Haiku session answers with: the start of the role's id.
ID_CHARS = 8
#: The kind of item and answer (`jobs.titles_next`, `jobs.titles_report`).
KIND = "facts"
#: What a Haiku read may be asked: the place, the way of working, the pay, and whether the job leads business
#: development (only for a role whose profile holds a line on that dial: `bd_wanted`).
ASK_PLACE, ASK_MODE, ASK_PAY, ASK_BD = "place", "work_mode", "pay", dials.LEADS_BD
#: And, when the owner's Main work list holds a word (`mw_words`), whether one of those words is the role's main work.
ASK_MW = dials.MAIN_WORK
#: The mark of a role in play that is owed its Main work read (`_read_in_play`), kept apart from `state` so the role's
#: own read is left as it is.
MWQ = "asked"
#: The version of what the step reads of a posting. A role read under an older one (its gate facts hold no `reader`: 1)
#: has the facts the newer ones added read once more from the text it holds, nothing else read again and its fingerprint
#: unchanged, so a fact a release adds reaches the roles already read; the fingerprint has no version of its own, and a
#: role is otherwise read again only when what it was read from changes. 2 adds `ADDED`.
READER = 2
#: The facts reader 2 added: the percent of travel a posting states and whether it names its employer (PRD 46.4).
ADDED = ("travel_percent", "employer_named")
#: The stages the step reads: a role before Ready whose research is not done. Past that, or researched, the role's own
#: gates (`action_settle`, `regate`) judge it.
OPEN_STATES = gating.STEP_STATES

HOW = (
    "An item of kind `facts` is a role whose posting's header and condition lines you read for the few facts the "
    "owner's gates stop a role on, before anyone pays to analyse it. You are given `lines` (the posting's own header "
    "and condition lines, never the whole posting), `held` (what the app holds already), `asks` (which facts to find) "
    "Answer with jobs.titles_report: {id, kind: \"facts\", "
    "facts: {work_mode: {value: remote|hybrid|onsite, quote}, location: {value, quote}, pay: {value, quote}, "
    "leads_bd: {value: yes|no|unknown, quote}}}. "
    "Each fact carries `quote`: the words of `lines`, copied exactly. Send a fact only when the lines state it of "
    "this job; leave it out otherwise. Never guess from the employer or the title, except for leads_bd. "
    "When `asks` holds leads_bd you are given `bd_lines` (the job's title, then the posting's sentences that speak of "
    "selling; `lines` is empty when this is all that is asked), and the question is whether leading business "
    "development is a main responsibility of this job: owning a sales or new-business pipeline, a revenue, bookings "
    "or quota target, winning new clients or accounts, closing deals. For this ask the title is a line of evidence, "
    "like the sentences. Answer yes only when `bd_lines` say so of this job, with the words that say it, the title or "
    "a sentence, as `quote` (copied exactly); a yes with no quote is thrown away. Supporting work does not count: "
    "helping with proposals, keeping existing client relationships, working beside a sales team, or a sales team "
    "named only as the employer's. Answer no when the lines show the job is not that, unknown when they do not tell. "
    "When `asks` holds main_work you are given `mw_lines` (the job's title, the posting's opening, then sentences that "
    "hold one of the owner's words) and `mw_words` (the owner's words), and answer main_work: {value, quote}. The "
    "value is one of `mw_words`, exactly as listed, only when the posting says it is the role's main work, with the "
    "words that say it as `quote` (copied exactly; a word with no quote is thrown away); the value is none when the "
    "word names who the role works with, a topic it touches or one duty among several, or when the lines do not say."
)


@dataclass(frozen=True)
class Outcome:
    """What `process` did with a role."""

    state: str
    stop: gating.Stop | None = None
    changes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Prepared:
    """What `read_batch` reads for a role while it holds no lock, which `process` uses in place of reading it itself:
    the facts the posting's text states, the copy the place and pay gates read, the day the role was posted, and where
    the owner has put the role (`placement`). Each stands on role columns and rows `read_batch` checks again once the
    role is locked (`GATE_STANDS`, the copy's id and the posted day)."""

    found: Extracted
    copy: CleansedPosting | None
    posted: freshness.PostedDay
    standing: placement.Standing
    #: The role's fingerprint on the text it was read from, hashed once with nothing locked. `process` compares it and
    #: stores it only when `fill` changed nothing (it hashes the columns `fill` writes).
    fp: str | None = None


#: How much CPU one of this step's loops runs before it gives the event loop back: its work is code over hundreds of
#: roles, and a stretch longer than this delays the statements of every other task.
YIELD_AFTER = 0.005


class _Pace:
    """Gives the event loop back (`await asyncio.sleep(0)`) once a loop has run `YIELD_AFTER` seconds without it."""

    def __init__(self) -> None:
        self._since = time.perf_counter()

    async def tick(self) -> None:
        if time.perf_counter() - self._since >= YIELD_AFTER:
            await asyncio.sleep(0)
            self._since = time.perf_counter()


def fingerprint(role: Opportunity, text: str | None) -> str:
    """What the step read of a role, as one hash: its posting's text, place, way of working, pay and employer. When it
    is the same as the one kept, nothing has changed and nothing is read again."""
    digest = hashlib.sha1()  # the parts one after another: the bytes of the joined string, unbuilt
    for n, part in enumerate((text or "", role.location or "", role.move_type or "", role.comp or "",
                              role.company or "")):
        if n:
            digest.update(b"\x1f")
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()


def short_id(role: Opportunity) -> str:
    return str(role.id)[:ID_CHARS]


def _now() -> datetime:
    return datetime.now(UTC)


def _stored(role: Opportunity) -> dict:
    return dict(role.gate_facts or {})


def pending() -> ColumnElement[bool]:
    """The roles whose Haiku read is still to come (SQL): their `gate_facts` say `PENDING`. Written as the partial index
    holds it (`ix_js_opportunities_owner_facts_pending`, `jsonsql`), so the read finds the few roles that wait."""
    return jsonsql.field(Opportunity.gate_facts, "state") == PENDING


async def held_back_ids(session: AsyncSession, owner: str, now: datetime | None = None) -> set[uuid.UUID]:
    """The roles with the analyst whose Haiku read is still to come and `HOLD` has not run out: the line leaves them
    out."""
    rows = (await session.execute(select(Opportunity.id, Opportunity.gate_facts).where(
        Opportunity.owner == owner, Opportunity.analysis_state == "waiting", pending()))).all()
    moment = now or _now()
    out = set()
    for role_id, facts in rows:
        try:
            at = datetime.fromisoformat((facts or {}).get("at") or "")
        except ValueError:
            continue
        if moment - at < HOLD:
            out.add(role_id)
    return out


# --- deciding ------------------------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=256)
def _copy_bands(text: str) -> tuple[tuple[int, int, str], ...]:
    """The pay ranges a cleansed copy's text states (`posting_facts.extract`'s `bands`: they come from the text alone,
    the company and title it is also given read other facts). A pure function of the text, kept for the last few
    hundred copies: `_bands` asks for the same copy's ranges twice a role (`decide`, `pay_mark`: a whole extract of the
    copy's text each, 10 to 15 ms of code), and `read_batch` asks first, while it holds no lock."""
    return tuple(posting_facts.extract(text).bands)


def _bands(role: Opportunity, found: Extracted | None, copy=None) -> list[tuple[int, int, str]]:
    """The ranges the posting the gates judge states, which the most generous of is the pay a gate may stop on: those in
    the text of the role's cleansed copy `copy` (the posting the owner's rule picks, `gate_copy`: a role listed in
    several pay bands is judged on the one posting's, never the most generous of every posting's); when that text
    states none (or the role has no copy), those in the text the step read (`found`), as for any fact a posting does
    not state."""
    if found is None:
        return []
    own = list(_copy_bands(copy.text_clean)) if copy is not None and copy.text_clean else []
    return own or found.bands


def _subject(role: Opportunity, rules: gating.Rules, found: Extracted | None, copy=None,
             posted: freshness.PostedDay | None = None, read: bool | None = None) -> gating.Subject:
    """The role as the gates read it before analysis: as the re-gate and the settle read it (`gating.role_subject`, on
    the role's cleansed copy `copy` when it has one: the copy of the posting the owner's rule picks, `gate_copy`; and
    the day it was posted, `posted`), so one rule decides it on one subject wherever it is asked. Pay is the most
    generous range that posting states when it states several (`posting_facts.generous`). `read`: see
    `gating.role_subject`."""
    employer = rules.index.of(role.employer_id, role.company)
    subject = gating.role_subject(role, employer, copy, posted, read)
    if found is not None:  # the facts just read count before they are stored (a job dial reads them)
        subject = replace(subject, stored={**(subject.stored or {}), **found.facts()})
    bands = _bands(role, found, copy)
    best = posting_facts.generous(bands) if len(bands) > 1 else None
    if best is not None:
        subject = replace(subject, comp=None, comp_min=best[0], comp_max=best[1])
    elif role.comp_source == "research":
        subject = replace(subject, comp=None)  # the analyst's own band is no posted range
    return subject


def _pay_scope(line: gating.Line, rules: gating.Rules):
    """The pay gate's numbers a compensation line was judged on: All's, or the target role's own."""
    return rules.gates if line.id == "compensation" else rules.gates.by_role.get(line.id.split(":", 1)[1])


def pay_shortcut(subject: gating.Subject, rules: gating.Rules) -> list[tuple[gating.Line, str, str]]:
    """The pay gate's lines the posted range alone settles (`gates.posted_pay_verdict`): (line, "fail" | "pass", the
    evidence). Only the posted range counts, never the analyst's ask."""
    low, high = gating.posted_band(subject)
    out = []
    for line in gating.results(subject, rules):
        if line.kind != "compensation" or (scope := _pay_scope(line, rules)) is None:
            continue
        verdict, evidence = posted_pay_verdict(low, high, scope)
        if verdict != "unknown":
            out.append((line, verdict, evidence))
    return out


def pay_mark(subject: gating.Subject, rules: gating.Rules) -> dict | None:
    """What a role's `gate_facts` keeps when the posted range settles every pay gate as a pass: the result and its
    evidence."""
    settled = pay_shortcut(subject, rules)
    if settled and all(verdict == "pass" for _, verdict, _ in settled):
        return {"result": "pass", "evidence": settled[0][2]}
    return None


def decide(role: Opportunity, rules: gating.Rules, found: Extracted | None = None, copy=None,
           posted: freshness.PostedDay | None = None, read: bool | None = None) -> gating.Stop | None:
    """The first gate that fails on what the role holds (and the employer the posting names), as the one gate check
    reads it (`gating.check`, the re-gate's and the settle's too: which fails a place that fails whatever the way of
    working, and an estimated-ask pay gate only on the posted range's certain bound, `pay_check`). Pay is judged
    on the most generous range the posting states when it states several (`posting_facts.generous`). `posted` is the day
    the role was posted (`freshness.posted_of`), counted the same way for every gate. `read`: whether every reader of a
    way of working is done with the posting, so a way it does not state is assumed on site (`gating.role_subject`,
    `gating.way_assumed`); the step says it when it has just finished the read the role's stored state does not show
    yet, and leaves it out when the stored state says it (`gating.facts_read`)."""
    subject = _subject(role, rules, found, copy, posted, read)
    stop = gating.check(subject, rules)
    if stop is not None:
        return stop
    named = found.employer.value if found is not None and found.employer is not None else None
    if named and employers.key_of(named) != employers.key_of(role.company):
        # The employer the posting names is judged by the employer gates too (a recruiter's posting for a client the
        # owner never works for).
        other = replace(subject, company=named, employer=rules.index.find(named))
        for line in gating.results(other, rules):
            if line.kind == "employer" and line.result == gating.FAIL:
                return gating.stop_of(line)
    return None


# --- the facts on the role -----------------------------------------------------------------------------------------


def fill(role: Opportunity, found: Extracted) -> list[tuple[str, Found]]:
    """Put the facts the posting states on the role, only where the role holds none: the place when the role's names no
    town (never over a town it holds, never on a remote role: `stated_place.fills`), the way of working when it holds
    none, the pay when it holds none. Returns what was put, with the line each rests on."""
    done: list[tuple[str, Found]] = []
    held_mode = (role.move_type or "").strip().lower()
    mode_known = found.mode is not None or held_mode in (*posting_facts.MODES, "restricted_remote")
    # A place read with no way of working stated beside it decides nothing: it is left off the role, so no gate can fail
    # on it (the owner's rule, an unknown passes).
    if (found.place is not None and mode_known
            and stated_place.fills(role.location, role.move_type, found.place.value)):
        role.location = found.place.value
        done.append(("location", found.place))
    if found.mode is not None and (role.move_type or "").strip().lower() in ("", "unknown"):
        role.move_type = found.mode.value
        done.append(("work_mode", found.mode))
    if found.pay is not None and not (role.comp or "").strip():
        role.comp, role.comp_source = found.pay.value, "posting"
        done.append(("pay", found.pay))
    return done


def _held_facts(role: Opportunity) -> dict:
    """What the role holds of the facts, as the listing gave them (a fact the posting adds is kept beside, with its
    line)."""
    return {k: {"value": v, "quote": "", "by": posting_facts.LISTING}
            for k, v in (("location", role.location), ("work_mode", role.move_type), ("pay", role.comp)) if v}


def bd_wanted(rules: gating.Rules, role: Opportunity) -> bool:
    """Whether the owner's profile holds a line on the Leads business development dial for this role: a Must in Hard
    pass or a Prefer in Prioritization that counts for the role's target role (`GateSet.lines_for`,
    `Profile.items_for`: the lines the gate check and the score read). Without one nothing is asked and nothing is
    spent (docs/PRD.md 46.4)."""
    profile = rules.profile
    s = profile.scoring
    if not (any(g.fact == dials.LEADS_BD for g in profile.gates.other)
            or any(a.fact == dials.LEADS_BD for a in [*s.asks, *(m for g in s.groups for m in g.members)])):
        return False  # no line anywhere: the common case, settled without a list for each role
    if any(g.kind == "job_fact" and g.fact == dials.LEADS_BD for g in profile.gates.lines_for(role.target_role)):
        return True
    return any(a.fact == dials.LEADS_BD for item in profile.items_for(role.target_role)
               for a in (item.members if isinstance(item, Group) else [item]))


def mw_words(rules: gating.Rules, role: Opportunity) -> list[str]:
    """The owner's Main work words that count for this role: the values of the Hard pass lines on that dial that
    `GateSet.lines_for` gives its target role, lowercased, in the order listed. Empty when there is none (the common
    case: nothing is asked and nothing is spent)."""
    gates = rules.profile.gates
    if not any(g.fact == dials.MAIN_WORK for g in gates.other):
        return []
    words: list[str] = []
    for g in gates.lines_for(role.target_role):
        if g.kind == "job_fact" and g.fact == dials.MAIN_WORK:
            words += [w for w in dials.values_of(g.value) if w not in words]
    return words


def _extract(role: Opportunity, text: str) -> Extracted:
    """What the posting's text states of the cheap facts (`posting_facts.extract`), for this role. A text too short to
    be a posting (a snippet, a title and a few header lines: `whole.thin`) says nothing of what the job is, so code does
    not read `no` from it: the role passes, and is read again when the whole text arrives."""
    found = posting_facts.extract(text, role.company, role.title)
    if whole.thin(text):
        found.leads_bd = None
    return found


def _with_main_work(found: Extracted, role: Opportunity, text: str, words: Iterable[str]) -> Extracted:
    """`found` with the Main work fact code can write (`none`, `posting_facts.main_work_from_text`) when the
    owner's list holds words. The posting's other facts do not depend on the list."""
    found.main_work = posting_facts.main_work_from_text(text, role.title, list(words))
    return found


def _way_asks(role: Opportunity, rules: gating.Rules, text: str, copy=None) -> list[str]:
    """What a Haiku read is asked of the way of working: the way, and the place when the role's names no town, when the
    work location gate is undecided on the posting as it states it or would say something else judged as in person
    (`stated_place.asks_the_way`). Asked on the posting as it states it, never on the assumed on-site: the Haiku read is
    one of the readers a way of working is looked for with before it is assumed (the owner, 2026-11-05). Nothing when
    the role has no text to read."""
    if not text.strip() or not HAIKU_READS or not stated_place.asks_the_way(role, rules, copy):
        return []
    named = not names_a_town(role.location) and role.move_type != "remote"
    return [ASK_PLACE, ASK_MODE] if named else [ASK_MODE]


def _asks(role: Opportunity, rules: gating.Rules, found: Extracted, text: str, copy=None) -> list[str]:
    """What code could not settle that a Haiku read may: the place or way of working when the work location gate is
    still undecided, the pay when a line talks of pay and code read no range, and whether the job leads business
    development when the profile holds that dial and the posting has a sentence that speaks of selling. Nothing when the
    role has no text to read."""
    if not text.strip() or not HAIKU_READS:
        return []
    asks = _way_asks(role, rules, text, copy)
    g = rules.profile.gates
    own = g.role(role.target_role)
    if (g.checks_pay() or (own is not None and own.checks_pay())) and not found.bands:
        subject = gating.role_subject(role, None)
        if gating.posted_band(subject) == (None, None) and posting_facts.has_pay_words(text):
            asks.append(ASK_PAY)
    if _bd_asked(role, rules, found, text):
        asks.append(ASK_BD)
    if _mw_asked(role, rules, found, text):
        asks.append(ASK_MW)
    return asks


def _mw_asked(role: Opportunity, rules: gating.Rules, found: Extracted, text: str) -> bool:
    """Whether a Haiku read is to say what the role's main work is: the owner's list holds a word for it, code has not
    answered it (`found.main_work`), the posting is whole, and its title or a sentence holds one of the words."""
    words = mw_words(rules, role)
    return (bool(words) and found.main_work is None and not whole.thin(text)
            and (bool(dials.title_hits(role.title, ",".join(words)))
                 or bool(posting_facts.main_work_candidates(text, role.title, words))))


def _haiku(asks: list[str], rules: gating.Rules, role: Opportunity) -> dict:
    """The `haiku` mark of a role waiting for a read: what it is asked, none handed yet, and with the Main work ask the
    owner's words as they stood (the read is checked against what it was asked)."""
    out: dict = {"asks": asks, "handed": 0}
    if ASK_MW in asks:
        out["mw"] = mw_words(rules, role)
    return out


def _bd_asked(role: Opportunity, rules: gating.Rules, found: Extracted, text: str) -> bool:
    """Whether a Haiku read is to say if the job leads business development: the dial is on for the role, code has not
    answered it (`found.leads_bd`), and the posting has a sentence or a title that speaks of selling."""
    return (found.leads_bd is None and bd_wanted(rules, role)
            and bool(posting_facts.bd_candidates(text, role.title)))


#: What a fact is called in the line that says what a role was stopped on, where its name is not its words.
_FACT_NAMES = {ASK_BD: "leads business development", ASK_MW: "main work"}
#: What a stop's history line says the Haiku read was given: the header and condition lines, or for the business
#: development dial the title and the sentences that speak of selling (`posting_facts.bd_lines`).
_READ_HEADER = "Read by a short Haiku read of the posting's header and condition lines"
_READ_BD = "Read by a short Haiku read of the posting's title and the sentences that speak of selling"
_READ_MW = ("Read by a short Haiku read of the posting's title, its opening and the sentences that hold the "
            "owner's words")


def _read_how(stop: gating.Stop, rules: gating.Rules) -> str:
    """How a stop on a Haiku read says it was read: by what the read was given, which the failed gate decides."""
    on = {g.fact for g in rules.profile.gates.other if g.id == stop.gate_id}
    return _READ_BD if dials.LEADS_BD in on else _READ_MW if dials.MAIN_WORK in on else _READ_HEADER


def _evidence(found: dict, changes: list[tuple[str, Found]]) -> str:
    said = []
    for name, fact in changes:
        label = _FACT_NAMES.get(name) or name.replace("_", " ")
        said.append(f'{label}: {fact.value} (from the posting: "{fact.quote}")' if fact.quote
                    else f"{label}: {fact.value}")
    return "; ".join(said)


def _bd_catch_up(role: Opportunity, rules: gating.Rules, held: dict, found: Extracted, text: str,
                 facts: dict) -> dict:
    """What a role read while the Leads business development dial was off (or before it existed) gains once the dial
    is on: the mark `bd`; code's `no`, kept beside the facts the role holds, when the posting has no sentence that
    speaks of selling; else, for a role whose read is settled or still to come, the ask. A role waiting for a read
    keeps what it is asked and is asked this too; a settled role was answered what it asked, so it is asked only this
    (its hand-outs counted again, its hold running from now): its header lines are not sent again and nothing it was
    asked is asked again. Nothing else is read or changed. `facts` are the facts the role holds."""
    out: dict = {"bd": True}
    if "leads_bd" in facts:
        return out
    if found.leads_bd is not None:
        out["facts"] = {**facts, "leads_bd": found.leads_bd.as_dict()}
    elif _bd_asked(role, rules, found, text) and HAIKU_READS and held.get("state") in (SETTLED, PENDING):
        if held.get("state") == PENDING:
            haiku = dict(held.get("haiku") or {})
            asks = list(haiku.get("asks") or [])
            if ASK_BD not in asks:
                haiku["asks"] = [*asks, ASK_BD]
        else:
            haiku = {"asks": [ASK_BD], "handed": 0}
            out.update(state=PENDING, at=_now().isoformat())
        out["haiku"] = haiku
    return out


def _mw_due(held: dict, words: Iterable[str]) -> bool:
    """Whether the owner's Main work list holds a word the role was not read for: its mark `mw` lacks it. A removed
    word, or a save that changed no word, is no reason to read again."""
    return bool(set(words) - set(held.get("mw") or []))


def _mw_catch_up(role: Opportunity, rules: gating.Rules, held: dict, found: Extracted, text: str, facts: dict) -> dict:
    """What a role read before a word was on the owner's Main work list gains: the mark `mw` (the words it was read for,
    joined to the words it was marked with); code's `none`, kept beside the facts the role holds, when the posting
    holds no listed word; else, for a role whose read is settled or still to come, the ask, as the business development
    catch-up does (`_bd_catch_up`): a pending role keeps what it is asked and is asked this too, a settled one is
    asked only this. A role a gate stopped gets the mark and no ask. `held` is what the role holds with the other
    catch-up's changes; `facts` the facts it holds."""
    words = mw_words(rules, role)
    out: dict = {"mw": sorted({*words, *(held.get("mw") or [])})}
    if held.get("state") == GATED:
        return out
    if found.main_work is not None:
        out["facts"] = {**facts, "main_work": found.main_work.as_dict()}
    elif _mw_asked(role, rules, found, text) and HAIKU_READS and held.get("state") in (SETTLED, PENDING):
        if held.get("state") == PENDING:
            haiku = dict(held.get("haiku") or {})
            asks = list(haiku.get("asks") or [])
            if ASK_MW not in asks:
                haiku["asks"] = [*asks, ASK_MW]
            haiku["mw"] = words
        else:
            haiku = {"asks": [ASK_MW], "handed": 0, "mw": words}
            out.update(state=PENDING, at=_now().isoformat())
        out["haiku"] = haiku
    return out


async def gate_out(session: AsyncSession, role: Opportunity, stop: gating.Stop, rules: gating.Rules, *,
                   evidence: str = "", how: str = "", inside: bool = False) -> None:
    """Stop the role at the gate that failed on the posting's facts, with that gate, its line and the versions it was
    judged under kept as data (`gating.role_stop_values`), and a line in its history that says what it was stopped on
    and that nothing was spent on its analysis. Services flush."""
    gating.stop_role(role, stop, rules)
    await research_line.leave(session, role, research_line.GATE_FAILED, inside=inside)
    detail = (f"{how}. " if how else "") + (f"What the posting says: {evidence}. " if evidence else "")
    await trail.record(
        session, role.owner, event="gate_facts_stopped", actor="app", who=trail.APP_WHO, link=("opportunity", role.id),
        sentence=f"Gated out before analysis: {stop.role_words}",
        detail=(detail + "It was stopped on the posting's own facts, so it was not analysed and the browser did not "
                         "open it.").strip())
    await session.flush()


def _reads_text(role: Opportunity) -> bool:
    """Whether the step reads a text for the role at all: a role that is not Sourced and holds no description is judged
    on its listing alone."""
    return role.sourced_at is not None or bool(role.description)


def judged(role: Opportunity, postings: Iterable[JobPosting]) -> str:
    """The text `process` judges a role on, from the role and its postings already read: what the role holds
    (`posting_text.held_from`: its own description when whole, else the fullest whole text of a posting, else its
    snippet), nothing for a role the step reads no text for. The one answer the sweep (`stale`) and `process` share, so
    the fingerprint the sweep compares is the one `process` stored."""
    return posting_text.held_from(role, postings).text if _reads_text(role) else ""


async def process(session: AsyncSession, role: Opportunity, *, rules: gating.Rules | None = None,
                  text: str | None = None, inside: bool = False, prepared: Prepared | None = None) -> Outcome:
    """Read the role's posting for the cheap facts and decide the cheap gates on them. A role before Ready whose
    research is not done, that the owner did not move by hand, is read when its fingerprint is not the one kept; a role
    the step has read already (nothing changed) costs no more than the comparison. Returns what happened: `STOPPED`
    (Gated out, with the reason and the evidence on its history), `WAITS` (a Haiku read of the header and condition
    lines is still to come) or `CLEAR`; `LEFT` for a role the step does not read. Services flush; the caller commits.
    `inside`: called from the analysis process's own step. `prepared`: what the caller read for this role with no lock
    (`read_batch`), used in place of the reads of the owner's moves, the copy, the posted day and the facts the text
    states."""
    if not ENABLED or role.status not in stages.PRE_READY or role.analysis_state not in OPEN_STATES:
        return Outcome(LEFT)
    if role.analysis_state == "waiting" and role.analysis_claimed_at is not None and not inside:
        return Outcome(LEFT)  # a session has the role: its own research is under way
    held = _stored(role)
    if text is None:
        text = (await posting_text.held(session, role)).text if _reads_text(role) else ""
    rules = rules or await gating.rules(session, role.owner)
    wanted = bd_wanted(rules, role)
    words = mw_words(rules, role)
    if held.get("fp") and held["fp"] == (prepared.fp if prepared and prepared.fp else fingerprint(role, text)):
        floor_same = held.get("pay_floor", 0) == rules.gates.comp_floor
        older = held.get("reader", 1) < READER
        bd_due = wanted and not held.get("bd")
        mw_due = _mw_due(held, words)
        way_due = _way_due(held)
        if floor_same and not older and not bd_due and not mw_due and not way_due:
            return Outcome(WAITS if held.get("state") == PENDING else CLEAR)
        # Only the pay floor changed, or the step reads more than it did when it read this role (a newer reader, or the
        # dial on whether the job leads business development, set after it was read, or the way of working never
        # looked for): the gates are decided again on what was read, and the facts a newer reader added are read from
        # the same text and kept beside it (`ADDED`; a fact the role holds is never read over). Nothing else is read
        # again.
        if (prepared.standing if prepared else await placement.one(
                session, role.owner, role, placement.Purpose.GATES)).held:
            return Outcome(LEFT)
        found = prepared.found if prepared else _with_main_work(_extract(role, text), role, text, words)
        copy = prepared.copy if prepared else await cleansed.gate_copy_of(session, role.owner, role.id)
        posted = prepared.posted if prepared else await freshness.posted_of(session, role)
        # A read with no mark that the way was looked for (made before the mark, or stopped before its asks) asks the
        # Haiku read for it now, unless it was asked and answered once already; the role is judged on the assumed
        # on-site only when none is left to ask.
        asks: list[str] = []
        read = None
        if way_due:
            haiku = held.get("haiku") or {}
            answered = bool(haiku.get("answered")) and ASK_MODE in (haiku.get("asks") or [])
            asks = [] if answered else _way_asks(role, rules, text, copy)
            read = not asks and gating.silent_copy(copy)
        stop = decide(role, rules, found, copy, posted, read)
        now = {"pay_floor": rules.gates.comp_floor, "reader": READER,
               "pay_gate": pay_mark(_subject(role, rules, found, copy, posted, read), rules)}
        if way_due and not asks:
            now[gating.FACTS_WAY] = bool(read)
        kept = held.get("facts") or {}
        if older:
            now["facts"] = {**kept, **{k: v for k, v in found.facts().items() if k in ADDED and k not in kept}}
        if bd_due:
            now.update(_bd_catch_up(role, rules, held, found, text, now.get("facts") or kept))
        if mw_due:
            now.update(_mw_catch_up(role, rules, {**held, **now}, found, text, now.get("facts") or kept))
        if stop is None:
            if asks:  # the way is asked first: the role waits for a read of this alone, as for the dial on business
                now.update(state=PENDING, at=_now().isoformat(), haiku=_haiku(asks, rules, role))
            role.gate_facts = {**held, **now}
            await session.flush()
            return Outcome(WAITS if role.gate_facts.get("state") == PENDING else CLEAR)
        role.gate_facts = {**held, **now, "state": GATED, "stop": stop.gate_id}
        # A stop here is never on the Leads business development dial: code's `no` is no fact a gate reads, so the dial
        # being set is no reason to give for it.
        how = ("Read in code, the pay floor having changed" if not floor_same
               else "Read in code, the percent of travel and the employer named read again from the posting" if older
               else "Read in code, the way of working looked for before it was assumed" if way_due
               else "Read in code, the gates decided again on what the role holds")
        await gate_out(session, role, stop, rules, evidence=_evidence(held.get("facts") or {}, []), how=how,
                       inside=inside)
        return Outcome(STOPPED, stop)
    if (prepared.standing if prepared else await placement.one(
            session, role.owner, role, placement.Purpose.GATES)).held:
        return Outcome(LEFT)
    found = prepared.found if prepared else _with_main_work(_extract(role, text), role, text, words)
    changes = fill(role, found)
    fp = prepared.fp if prepared and prepared.fp and not changes else fingerprint(role, text)  # fill moves its columns
    copy = prepared.copy if prepared else await cleansed.gate_copy_of(session, role.owner, role.id)
    posted = prepared.posted if prepared else await freshness.posted_of(session, role)
    # What a Haiku read is still to answer is asked before the gates decide, so a way of working the posting does not
    # state is assumed (on site, `gating.way_assumed`) only once every reader is done: not while a read is to come.
    asks = _asks(role, rules, found, text, copy)
    reading_done = not asks and gating.silent_copy(copy)
    stop = decide(role, rules, found, copy, posted, reading_done)
    facts = {**_held_facts(role), **found.facts()}
    names = tuple(name for name, _ in changes)
    # `bd`: the role was read with the Leads business development dial on, so a role read with it off is read once more
    # when it is set (`_due`), as `pay_floor` does for the pay gate.
    # `way`: no ask is left (every one answered or none needed), and whether the copy was whole and said nothing of a
    # way: the mark `gating.facts_read` goes by, absent while an ask is still to be answered.
    # `mw`: the words of the owner's Main work list the role was read with, sorted (absent when the list is empty).
    floor_now = {"pay_floor": rules.gates.comp_floor, "bd": wanted, **({"mw": sorted(words)} if words else {}),
                 "pay_gate": pay_mark(_subject(role, rules, found, copy, posted, reading_done), rules),
                 **({} if asks else {gating.FACTS_WAY: reading_done})}
    if stop is not None:
        role.gate_facts = {"fp": fp, "at": _now().isoformat(), "state": GATED, "facts": facts,
                           "stop": stop.gate_id, "reader": READER, **floor_now}
        await gate_out(session, role, stop, rules, evidence=_evidence(facts, changes), how="Read in code",
                       inside=inside)
        return Outcome(STOPPED, stop, names)
    role.gate_facts = {"fp": fp, "at": _now().isoformat(),
                       "state": PENDING if asks else SETTLED, "facts": facts, "haiku": _haiku(asks, rules, role),
                       "reader": READER, **floor_now}
    if changes:
        await trail.record(
            session, role.owner, event="gate_facts_read", actor="app", who=trail.APP_WHO,
            link=("opportunity", role.id), sentence="Read the gate facts from the posting.",
            detail=_evidence(facts, changes))
    await session.flush()
    return Outcome(WAITS if asks else CLEAR, None, names)


async def after_text(session: AsyncSession, role: Opportunity) -> None:
    """Called when a role's text is saved (`posting_text.take`): read it for the gate facts at once, so a role whose
    posting states a place or pay that fails a gate never waits for the analyst. A role in the analyst's line is read by
    the process's own step (`action_gate_facts`), and one a session is researching is left alone. A failure here never
    stops the save."""
    if role.analysis_state == "waiting":
        return
    try:
        async with session.begin_nested():
            await process(session, role)
    except Exception:
        log.warning("reading the gate facts of role %s failed", role.id, exc_info=True)


#: The columns of a role the sweep never reads: its research and the breakdowns of its score. `stale` loads the roles
#: without them (`placement.load` reads none of them either).
_LIGHT = (defer(Opportunity.analysis), defer(Opportunity.fit_breakdown), defer(Opportunity.gate_answers),
          defer(Opportunity.analysis_made_under))
#: How many roles' postings one statement asks for.
_POSTINGS_AT_ONCE = 500
#: How many roles `stale` loads, and judges before it gives the event loop back, at a time (a role's row is about 5 KB
#: with its text: the load of every role before Ready was one statement and one long stretch), and how
#: many `sweep` hands `read_batch` at a time (which reads the texts, copies and posted days of all it is given first).
STALE_SLICE = 250
#: What `read_batch` read of each role while it held no lock, as the role must still be when it is locked
#: (`as_read.lock`'s `stands`): a role that holds anything else then was changed meanwhile, and is left for the next
#: turn's `stale`. Every role column that the facts read from the posting, the copy the gates read, the day posted and
#: the owner's moves are made from: the text it is judged on (`description`, where it came from, `sourced_at`), the
#: company and title the facts are read for, the address and kind of its source (the copy is chosen by them), the dates
#: the posted day counts from, and the stored read (`gate_facts`) and the place, way of working and pay the fingerprint
#: is of. What `process` reads of the locked row itself (the research, the answers, the analyst's claim on it) is read
#: after the lock and needs no stand. A posting's text is no stand: it is inside the fingerprint, so a changed text is
#: read again next turn.
GATE_STANDS = (
    Opportunity.status, Opportunity.analysis_state, Opportunity.analysis_claimed_at, Opportunity.gate_facts,
    Opportunity.location, Opportunity.move_type, Opportunity.comp, Opportunity.comp_source, Opportunity.company,
    Opportunity.title, Opportunity.employer_id, Opportunity.target_role, Opportunity.sourced_at,
    Opportunity.description, Opportunity.description_from, Opportunity.source_kind, Opportunity.source_url,
    Opportunity.posted, Opportunity.found, Opportunity.board_url)


def _found_where(owner: str) -> tuple:
    """What a role must be for the step to read it: before Ready, its research not done (`OPEN_STATES`)."""
    return (Opportunity.owner == owner, Opportunity.status.in_(stages.PRE_READY),
            Opportunity.analysis_state.in_([s for s in OPEN_STATES if s is not None])
            | Opportunity.analysis_state.is_(None))


def in_play(role: Opportunity) -> bool:
    """Whether a role is already in play: researched (`analysis_state` done) or in Ready to Launch. The Main work read
    is stored for such a role and never moves it (`check.Subject.in_play`)."""
    return role.analysis_state == "done" or role.status == stages.READY_TO_LAUNCH


def _in_play_where(owner: str) -> tuple:
    """What a role must be for the Main work read to store a fact on it without moving it (`in_play`)."""
    return (Opportunity.owner == owner, Opportunity.status.in_(stages.PRE_APPLY),
            (Opportunity.analysis_state == "done") | (Opportunity.status == stages.READY_TO_LAUNCH))


def _mw_listed(rules: gating.Rules) -> bool:
    """Whether the owner has any Main work word at all (for any target role): with none, no in-play role is read."""
    return any(g.kind == "job_fact" and g.fact == dials.MAIN_WORK and dials.values_of(g.value)
               for g in rules.profile.gates.other)


def _read_where(owner: str, in_play_too: bool = True):
    """The roles `stale` and `read_batch` look at: those before Ready the step reads, and those in play it only reads
    for the Main work (`_read_in_play`), unless `in_play_too` is off (the owner's Main work list is empty)."""
    if not in_play_too:
        return and_(*_found_where(owner))
    return or_(and_(*_found_where(owner)), and_(*_in_play_where(owner)))


def _found(owner: str):
    """The roles the step reads (`_found_where`)."""
    return select(Opportunity).where(*_found_where(owner))


async def _judged_texts(session: AsyncSession, owner: str, roles: list[Opportunity]) -> dict[uuid.UUID, str]:
    """The text each role is judged on (`judged`). The postings are read once for the roles that need them (their own
    description is empty or a snippet) and only those that could be the fullest whole text: one under
    `cleansed.MIN_TEXT` characters has under that many bytes, which `octet_length` says without unpacking the text."""
    need = [r.id for r in roles if _reads_text(r) and posting_text.needs_postings(r)]
    postings: dict[uuid.UUID, list[JobPosting]] = {}
    for start in range(0, len(need), _POSTINGS_AT_ONCE):
        for posting in await session.scalars(select(JobPosting).where(
                JobPosting.owner == owner, JobPosting.opportunity_id.in_(need[start:start + _POSTINGS_AT_ONCE]),
                func.octet_length(JobPosting.text) >= cleansed.MIN_TEXT)):
            if posting.opportunity_id is not None:
                postings.setdefault(posting.opportunity_id, []).append(posting)
    await db.release_if_free(session)  # the loop below is CPU work: it runs without the writer turn
    pace, texts = _Pace(), {}
    for role in roles:
        texts[role.id] = judged(role, postings.get(role.id, []))
        await pace.tick()
    return texts


def _way_due(held: dict) -> bool:
    """Whether a finished read (settled, or stopped by a gate) was made with no look for the way of working: it holds no
    `way` mark, being read before the mark existed or stopped before its asks, so the way is looked for now (the owner,
    2026-11-05: the readers go first). A read still to come has no mark and is not due for it: its answer ends it."""
    return held.get("state") in (SETTLED, GATED) and gating.FACTS_WAY not in held


def _due(role: Opportunity, text: str, rules: gating.Rules, fp: str | None = None) -> bool:
    """Whether a role is to be read: no session holds it (`process` leaves it), and what the step read of it is not
    what it is now (its fingerprint, on `text`, or the pay floor it was decided under has changed, or it was read under
    an older `READER`, or the Leads business development dial is on for it and it was read with the dial off: no `bd`
    mark, or the way of working was never looked for: no `way` mark, `_way_due`)."""
    if role.analysis_state == "waiting" and role.analysis_claimed_at is not None:
        return False
    held = _stored(role)
    if in_play(role):  # the step reads nothing else of it
        return _mw_due(held, mw_words(rules, role))
    return not (held.get("fp") and held["fp"] == (fp or fingerprint(role, text))
                and held.get("pay_floor", 0) == rules.gates.comp_floor and held.get("reader", 1) >= READER
                and (held.get("bd") or not bd_wanted(rules, role)) and not _mw_due(held, mw_words(rules, role))
                and not _way_due(held))


async def stale(session: AsyncSession, owner: str, *, limit: int, rules: gating.Rules | None = None
                ) -> list[uuid.UUID]:
    """The roles before Ready that have not been read in their present state, oldest first, at most `limit`: the roles
    whose text, place, way of working, pay or employer changed since (`fingerprint`), or whose pay gate was decided
    under another floor, or that were read under an older `READER`, or that were read with the Leads business
    development dial off and it is on for them now (no `bd` mark: `_due`), that no session holds and the owner did not
    move by hand. Reads only, and takes no lock: the ids come first, oldest first, then the roles `STALE_SLICE` at a
    time without their research, a posting's text is read only for a role that has none of its own to judge it by, and
    where the owner's moves are asked (`placement.load`) is only for the roles of a slice whose fingerprint differs.
    The event loop is given back between slices and every `YIELD_AFTER` seconds of judging, and the reading stops at
    `limit` roles (the oldest `limit` are the same ones whether or not the rest are read). Nothing is changed here;
    `read_batch` locks the ones to read and reads them again."""
    if not ENABLED:
        return []
    rules = rules or await gating.rules(session, owner)
    where = _read_where(owner, _mw_listed(rules))  # no Main work word: the in-play roles are not loaded or read at all
    ids = list((await session.scalars(
        select(Opportunity.id).where(where).order_by(Opportunity.created_at, Opportunity.id))).all())
    pace = _Pace()
    out: list[uuid.UUID] = []
    for start in range(0, len(ids), STALE_SLICE):
        if len(out) >= limit:
            break
        roles = list((await session.scalars(
            select(Opportunity).where(Opportunity.id.in_(ids[start:start + STALE_SLICE]), where)
            .options(*_LIGHT))).all())
        roles.sort(key=lambda r: (r.created_at, r.id))  # the ids came oldest first; the statement gives no order
        texts = await _judged_texts(session, owner, roles)
        due = []
        for role in roles:
            if _due(role, texts[role.id], rules):
                due.append(role)
            await pace.tick()
        if due:
            places = await placement.load(session, owner, due, placement.Purpose.GATES)
            due = [r for r in due if not places[r.id].held]
        out.extend(r.id for r in due[:limit - len(out)])
        await asyncio.sleep(0)  # a slice is a statement or two and its judging: the others get a turn
    return out


async def _prepare(session: AsyncSession, owner: str, due: list[Opportunity], texts: dict[uuid.UUID, str],
                   places: dict[uuid.UUID, placement.Standing], fps: dict[uuid.UUID, str] | None = None,
                   rules: gating.Rules | None = None) -> dict[uuid.UUID, Prepared]:
    """What each of these roles' read needs, read with nothing locked (`Prepared`): the day posted and the copies for
    all of them in a statement each, then the facts the text states and the pay ranges the copy states, each role in
    turn with the event loop given back between them. A role this fails on is left out (and so is every role when the
    statements fail): it is read as it is locked, as it always was, where a failure is its own (`read_batch`'s
    savepoint), not the batch's. The statements run in a savepoint of their own, so a database error in one (a statement
    timeout, which in Postgres aborts the whole transaction) costs the read ahead and not the lock that follows it."""
    ids = [r.id for r in due]
    try:
        async with session.begin_nested():
            held = await freshness.held_postings(session, owner, ids)  # by the roles' ids, however many there are
            days = await freshness.posted_by_role(session, owner, due, held)
            copies = await cleansed.gate_copies_for(session, owner, ids)
    except Exception:
        log.warning("the posted day and the copies of %d roles could not be read ahead: each is read as it is locked",
                    len(due), exc_info=True)
        return {}
    await db.release_if_free(session)  # the loop below is CPU work: it runs without the writer turn
    pace = _Pace()
    out: dict[uuid.UUID, Prepared] = {}
    for role in due:
        try:
            found = _extract(role, texts[role.id])
            if rules is not None:
                _with_main_work(found, role, texts[role.id], mw_words(rules, role))
            await pace.tick()
            copy = copies.get(role.id)
            if copy is not None and copy.text_clean:
                _copy_bands(copy.text_clean)  # the gates ask for its pay ranges under the lock: worked out now
            out[role.id] = Prepared(found, copy, days[role.id], places[role.id], (fps or {}).get(role.id))
        except Exception:
            log.warning("the gate facts of role %s could not be read ahead", role.id, exc_info=True)
        await pace.tick()
    return out


async def _moved_since(session: AsyncSession, owner: str, roles: list[Opportunity],
                       prepared: dict[uuid.UUID, Prepared]) -> set[uuid.UUID]:
    """The ids of these roles, locked, whose copy or posted day is not the one `_prepare` read for them. A copy is made
    new by every cleaning and never changed, so another id is another copy; the posted day counts from the postings'
    dates, which are no role column and so no stand of the lock (a role's own `posted`, `found` and `board_url` are, and
    are read from the locked row). Three statements for all of them (the copies, the sites that say which postings are a
    board's, the postings' dates), never one for each role."""
    ids = [r.id for r in roles]
    chosen = await cleansed.gate_copy_ids_for(session, owner, ids)
    held = await freshness.held_postings(session, owner, ids)
    days = await freshness.posted_by_role(session, owner, roles, held)
    moved: set[uuid.UUID] = set()
    for role in roles:
        ready = prepared[role.id]
        if (chosen.get(role.id) != (ready.copy.id if ready.copy is not None else None)
                or days[role.id] != ready.posted):
            moved.add(role.id)
    return moved


async def _read_in_play(session: AsyncSession, owner: str, roles: list[Opportunity], texts: dict[uuid.UUID, str],
                        rules: gating.Rules) -> int:
    """The Main work read of roles already in play (`in_play`): the words they were not read for are looked for in
    their text. Code's `none` is stored where the posting holds no listed word; else the role is asked (`mwq`), and its
    answer is stored without moving it (`take_answers`). Nothing else about the role is read, decided or changed, and no
    gate runs: the role keeps its stage. Returns how many were read."""
    held = await as_read.lock(session, Opportunity, roles, stands=GATE_STANDS, where=_in_play_where(owner))
    read = 0
    for role in roles:
        if role not in held:
            continue  # taken meanwhile: the next turn's `stale` names it again
        stored = _stored(role)
        words = mw_words(rules, role)
        if not _mw_due(stored, words):
            continue
        text = texts[role.id]
        found = _with_main_work(Extracted(), role, text, words)
        now: dict = {"mw": sorted({*words, *(stored.get("mw") or [])})}
        if found.main_work is not None:
            now["facts"] = {**(stored.get("facts") or {}), "main_work": found.main_work.as_dict()}
        elif _mw_asked(role, rules, found, text) and HAIKU_READS:
            now.update(haiku={"asks": [ASK_MW], "handed": 0, "mw": words}, mwq=MWQ)
        role.gate_facts = {**stored, **now}
        read += 1
    await session.flush()
    return read


async def main_work_hits_unmoved(session: AsyncSession, owner: str, rules: gating.Rules | None = None) -> list[dict]:
    """The roles in play (`in_play`) whose stored Main work is one of the owner's current words with its quote, which
    the dial would have stopped had they not been in play: `{id, title, company, word, quote}` for each, oldest first.
    Reads only; no screen or control uses it yet (docs/PRD.md 39.31)."""
    rules = rules or await gating.rules(session, owner)
    rows = (await session.execute(
        select(Opportunity.id, Opportunity.title, Opportunity.company, Opportunity.target_role, Opportunity.gate_facts)
        .where(*_in_play_where(owner)).order_by(Opportunity.created_at, Opportunity.id))).all()
    out = []
    for role_id, title, company, target, facts in rows:
        said = ((facts or {}).get("facts") or {}).get(dials.MAIN_WORK)
        if not (isinstance(said, dict) and said.get("by") != posting_facts.CODE and said.get("quote")):
            continue
        listed = ",".join(mw_words(rules, SimpleNamespace(target_role=target)))  # type: ignore[arg-type]
        if listed and dials.title_hits(str(said.get("value") or ""), listed) and dials.title_hits(
                str(said["quote"]), listed):
            out.append({"id": role_id, "title": title, "company": company, "word": said["value"],
                        "quote": said["quote"]})
    return out


async def read_batch(session: AsyncSession, owner: str, ids: list[uuid.UUID],
                     rules: gating.Rules | None = None) -> dict:
    """Read these roles (`stale` named them) for the gate facts (`process`), each in a savepoint. Everything a role's
    read needs from the database or from code alone (its text, where the owner has put it, the copy the gates read, the
    day it was posted, the facts the text states) is read first for all the roles, in a few statements, with nothing
    locked and the event loop given back between roles (`_prepare`); then the roles that are due are locked in one
    statement (a role another transaction holds, a session taking it, is left for the next turn) and each is looked at
    as the lock gives it: a role whose columns (`GATE_STANDS`), copy or posted day are not what was read is left as it
    is, as is one a session holds since, one the owner moved or one read meanwhile, for the next turn's `stale`. Under
    the lock there are three statements for all the roles (`_moved_since`) and, for each role, its savepoint and its
    writes. Returns `{looked, stopped, waits}`. Services flush; the caller commits."""
    out = {"looked": 0, "stopped": 0, "waits": 0}
    if not ENABLED or not ids:
        return out
    rules = rules or await gating.rules(session, owner)
    roles = sorted((await session.scalars(
        select(Opportunity).where(Opportunity.id.in_(ids), _read_where(owner)).options(*_LIGHT))).all(),
        key=lambda r: (r.created_at, r.id))  # read oldest first
    texts = await _judged_texts(session, owner, roles)
    fps: dict[uuid.UUID, str] = {}
    due = []
    for r in roles:  # one hash of each role's text and columns, used by the due look, the comparison and the store
        fps[r.id] = fingerprint(r, texts[r.id])
        if _due(r, texts[r.id], rules, fps[r.id]):
            due.append(r)
    if not due:
        return out
    places = await placement.load(session, owner, due, placement.Purpose.GATES)
    due = [r for r in due if not places[r.id].held]  # the owner's: never read
    if not due:
        return out
    play = [r for r in due if in_play(r)]
    if play:
        out["looked"] += await _read_in_play(session, owner, play, texts, rules)
        due = [r for r in due if r not in play]
        if not due:
            return out
    prepared = await _prepare(session, owner, due, texts, places, fps, rules)
    held = await as_read.lock(session, Opportunity, due, stands=GATE_STANDS, where=_found_where(owner))
    locked = [r for r in due if r in held]  # locked in id order, as read: read oldest first
    ahead = [r for r in locked if r.id in prepared]
    moved = await _moved_since(session, owner, ahead, prepared) if ahead else set()
    for role in locked:
        ready = prepared.get(role.id)
        if role.id in moved:
            continue  # its posting has a newer copy or another date since: read again next turn
        try:
            async with session.begin_nested():
                outcome = await process(session, role, rules=rules, text=texts[role.id], prepared=ready)
        except Exception:
            log.warning("reading the gate facts of role %s failed", role.id, exc_info=True)
            continue
        if outcome.state == LEFT:
            continue
        out["looked"] += 1
        out["stopped"] += outcome.state == STOPPED
        out["waits"] += outcome.state == WAITS
    await session.flush()
    return out


async def sweep(session: AsyncSession, owner: str, *, limit: int = 400) -> dict:
    """Read every role before Ready that has not been read in its present state (`stale`, then `read_batch`), in one
    session: the roles in Fact checking on what their listing shows (before a browser item is queued for them), and the
    roles whose text, place, way of working or pay changed since. Cheap when nothing changed: a role's own columns are
    compared, and a posting's text is read only for a role that holds none of its own. The pipeline's turn does the same
    in a transaction for each batch (`schedule.read_gate_facts`). Returns `{looked, stopped, waits}`."""
    if not ENABLED:
        return {"looked": 0, "stopped": 0, "waits": 0}
    rules = await gating.rules(session, owner)
    ids = await stale(session, owner, limit=limit, rules=rules)
    out = {"looked": 0, "stopped": 0, "waits": 0}
    for start in range(0, len(ids), STALE_SLICE):  # `read_batch` reads all it is given first: a slice at a time
        for name, count in (await read_batch(session, owner, ids[start:start + STALE_SLICE], rules)).items():
            out[name] += count
    return out


async def sweep_in_batches(owner: str, *, limit: int = 400) -> dict:
    """`sweep` for a caller with no transaction open: which roles are stale is read once, then each `BATCH` of them is
    read in a transaction of its own (`read_batch`), committed before the next begins, so the writer is held for one
    batch and not for the whole sweep. A failure is logged and ends the sweep there: the roles it did not reach are
    stale still and the next sweep reads them."""
    out = {"looked": 0, "stopped": 0, "waits": 0}
    if not ENABLED:
        return out
    try:
        async with db.read_session_factory()() as session:
            rules = await gating.rules(session, owner)
            ids = await stale(session, owner, limit=limit, rules=rules)
            await session.commit()
        for start in range(0, len(ids), BATCH):
            async with db.session_factory()() as session:
                for name, count in (await read_batch(session, owner, ids[start:start + BATCH], rules)).items():
                    out[name] += count
                await session.commit()
    except Exception:
        log.warning("reading the gate facts failed", exc_info=True)
    return out


async def sweep_safely(session: AsyncSession, owner: str) -> dict:
    """`sweep`, in a savepoint: a failure is logged and leaves everything as it was, so the work that asked for the
    sweep (the browser's queue, the night's sourcing) goes on."""
    try:
        async with session.begin_nested():
            return await sweep(session, owner)
    except Exception:
        log.warning("reading the gate facts failed", exc_info=True)
        return {"looked": 0, "stopped": 0, "waits": 0}


async def stop_if_failed(session: AsyncSession, role: Opportunity, point: str, *, rules: gating.Rules | None = None
                         ) -> bool:
    """The gates again on what the role holds now, at a point inside the analysis where more facts may exist (the
    employer's record, researched since): a session is about to take the role, or asks for the master resume. A role
    that fails is Gated out at once and the analysis ends there. Returns whether it was stopped. A role the owner moved
    by hand, or that is not before Ready, is left."""
    if not ENABLED or role.status not in stages.PRE_READY:
        return False
    if (await placement.one(session, role.owner, role, placement.Purpose.GATES)).held:
        return False
    rules = rules or await gating.rules(session, role.owner)
    stop = decide(role, rules, copy=await cleansed.gate_copy_of(session, role.owner, role.id),
                  posted=await freshness.posted_of(session, role))
    if stop is None:
        return False
    held = _stored(role)
    role.gate_facts = {**held, "state": GATED, "stop": stop.gate_id}
    await gate_out(session, role, stop, rules, how=f"Checked again {point}")
    return True


# --- the Haiku read --------------------------------------------------------------------------------------------------


async def waiting(session: AsyncSession, owner: str, *, skip_leased: bool = False, now: datetime | None = None,
                  light: bool = False) -> list[Opportunity]:
    """The roles a Haiku session may be handed now: before Ready, their research not done, their read still to come, not
    handed `HANDED_AT_MOST` times, and (with `skip_leased`) not on another session's lease. The oldest first. `light`:
    without the research, the breakdown of the score and the posting's text, which the caller who only picks and
    leases the roles never reads (`item` reads the text of the ones it shows); `analysis`, `description` and the others
    are then not loaded, and a caller that reads them loads them itself (a role's `session.refresh`)."""
    if not ENABLED or not HAIKU_READS:
        return []
    moment = now or _now()
    query = select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.status.in_(stages.PRE_READY), pending())
    if light:
        query = query.options(*_LIGHT, defer(Opportunity.description))
    rows = list((await session.scalars(query.order_by(
        Opportunity.status != stages.SECOND_IMPRESSION, Opportunity.created_at, Opportunity.id))).all())
    # The roles in play still owed their Main work read (`mwq`, `_read_in_play`), after the others.
    play = select(Opportunity).where(*_in_play_where(owner), jsonsql.field(Opportunity.gate_facts, "mwq") == MWQ)
    if light:
        play = play.options(*_LIGHT, defer(Opportunity.description))
    owed = [r for r in (await session.scalars(play.order_by(Opportunity.created_at, Opportunity.id))).all()
            if r not in rows]
    claimed = await hand_outs.held(session, owner, hand_outs.TITLES) if skip_leased else set()
    out = []
    for role in [*rows, *owed]:
        if (role.analysis_state not in OPEN_STATES and role not in owed) or str(role.id) in claimed:
            continue
        haiku = _stored(role).get("haiku") or {}
        if int(haiku.get("handed") or 0) >= HANDED_AT_MOST:
            continue
        if skip_leased and haiku.get("lease") and datetime.fromisoformat(haiku["lease"]) > moment:
            continue
        out.append(role)
    return out


async def session_holds(session: AsyncSession, owner: str, session_id: str | None,
                        now: datetime | None = None) -> bool:
    """Whether this session was handed roles to read and has not answered them all: one batch a session, as the title
    pass's is."""
    if not session_id:
        return False
    moment = now or _now()
    for role in await waiting(session, owner, light=True):
        haiku = _stored(role).get("haiku") or {}
        leased = haiku.get("lease") and datetime.fromisoformat(haiku["lease"]) > moment
        if haiku.get("to") == session_id[:80] and leased:
            return True
    return False


async def item(session: AsyncSession, role: Opportunity) -> dict:
    """One role as a Haiku session is handed it: what the app holds, what to find, and the posting's header and
    condition lines (never the whole posting). A role asked whether it leads business development carries the title and
    the sentences that speak of selling as `bd_lines`, and the header and condition lines only when something else is
    asked too."""
    if "description" in sa_inspect(role).unloaded:  # a role `waiting(light=True)` gave: its text is read now
        await session.refresh(role, ["description"])
    text = (await posting_text.held(session, role)).text
    asks = list((_stored(role).get("haiku") or {}).get("asks") or [])
    out = {"id": short_id(role), "kind": KIND, "title": role.title, "company": role.company,
           "held": {k: v for k, v in (("location", role.location), ("work_mode", role.move_type),
                                      ("pay", role.comp)) if v},
           "asks": asks, "lines": posting_facts.lines_for(text) if any(a not in (ASK_BD, ASK_MW) for a in asks) else ""}
    if ASK_BD in asks:
        out["bd_lines"] = posting_facts.bd_lines(text, role.title)
    if ASK_MW in asks:
        words = list((_stored(role).get("haiku") or {}).get("mw") or [])
        out["mw_lines"] = posting_facts.main_work_lines(text, role.title, words)
        out["mw_words"] = words
    return out


def hand_out(roles: list[Opportunity], session_id: str | None, now: datetime | None = None) -> None:
    """Note on each role that it was handed to a session (so it is not handed out again and again) and lease it."""
    moment = now or _now()
    for role in roles:
        held = _stored(role)
        haiku = dict(held.get("haiku") or {})
        haiku["handed"] = int(haiku.get("handed") or 0) + 1
        haiku["lease"] = (moment + LEASE).isoformat()
        haiku["to"] = (session_id or "")[:80]
        role.gate_facts = {**held, "haiku": haiku}


def _fact(raw, text: str, problems: list[str], name: str) -> Found | None:
    """One fact a session sent, if it stands: it has a value and a quote, and the quote is in the posting. A fact that
    does not is named in `problems` and dropped; the rest of the answer is kept."""
    if not isinstance(raw, dict):
        return None
    value = " ".join(str(raw.get("value") or raw.get("line") or "").split())
    quote = " ".join(str(raw.get("quote") or "").split())
    if not value:
        return None
    if not quote_in(text, quote):
        problems.append(f"{name}: the quote is not in the posting's lines, so the fact was not kept")
        return None
    return Found(value[:posting_facts.QUOTE_CHARS], quote[:posting_facts.QUOTE_CHARS], HAIKU)


def _bd_fact(raw, text: str, problems: list[str], title: str | None) -> Found | None:
    """The answer to "does the job lead business development" a session sent, if it stands. `yes` stands only on a quote
    that is in the posting (or is its title) and is dropped, and named in `problems`, when it has none or the quote is
    not there. `no` is kept (with the quote when it is in the posting): it never gates a Must that wants `no`, and a
    Must that wants `yes` is stopped on it. `unknown`, and anything else, is no fact."""
    if not isinstance(raw, dict):
        return None
    value = " ".join(str(raw.get("value") or "").split()).lower().strip(" .")
    quote = " ".join(str(raw.get("quote") or "").split())
    there = quote_in(f"{text}\n{title or ''}", quote)
    if value == "yes":
        if not there:
            problems.append(f"{ASK_BD}: the quote is not in the posting's lines, so the fact was not kept")
            return None
        return Found("yes", quote[:posting_facts.QUOTE_CHARS], HAIKU)
    if value == "no":
        return Found("no", quote[:posting_facts.QUOTE_CHARS] if there else "", HAIKU)
    return None


def _main_work_fact(raw, text: str, title: str | None, words: list[str], problems: list[str]) -> Found | None:
    """The answer to "what is the role's main work" a session sent, if it stands. The value is one of the owner's words
    (the one the list holds, whatever its case or hyphens) or `none`; anything else is no fact. A word stands only on a
    non-empty quote that is in the posting or its title, and is dropped, and named in `problems`, when it has none or
    the quote is not there. `none` keeps its quote only when the quote is there."""
    if not isinstance(raw, dict):
        return None
    value = " ".join(str(raw.get("value") or "").replace("-", " ").lower().split()).strip(" .")
    quote = " ".join(str(raw.get("quote") or "").split())
    there = quote_in(f"{text}\n{title or ''}", quote)
    if value == "none":
        return Found("none", quote[:posting_facts.QUOTE_CHARS] if there else "", HAIKU)
    listed = {" ".join(w.replace("-", " ").split()): w for w in words}
    word = listed.get(value)
    if word is None:
        if value and value != "unknown":
            problems.append(f"{ASK_MW}: the value is not one of the words asked, so the fact was not kept")
        return None
    if not there:
        problems.append(f"{ASK_MW}: the quote is not in the posting's lines, so the fact was not kept")
        return None
    return Found(word, quote[:posting_facts.QUOTE_CHARS], HAIKU)


def read_answer(text: str, raw: dict, asked: list[str], problems: list[str], title: str | None = None,
                mw: list[str] | None = None) -> Extracted:
    """The facts a Haiku answer states that stand on a quote of the posting, read as code reads them: the way of working
    is one of remote, hybrid, onsite; the pay a line with a range code can read; the place a name; whether the job leads
    business development, yes or no (`_bd_fact`; `title` is the role's, which a quote may be of); which of the owner's
    words `mw` is the role's main work, or none (`_main_work_fact`)."""
    found = Extracted()
    said = raw.get("facts")
    facts: dict = said if isinstance(said, dict) else {}
    if ASK_MODE in asked and (mode := _fact(facts.get("work_mode"), text, problems, "work_mode")):
        value = mode.value.strip().lower().replace("-", "").replace(" ", "")
        value = {"onsite": "onsite", "inperson": "onsite", "inoffice": "onsite"}.get(value, value)
        if value in posting_facts.MODES:
            found.mode = Found(value, mode.quote, HAIKU)
        else:
            problems.append("work_mode: one of remote, hybrid or onsite")
    wants_place = ASK_PLACE in asked or ASK_MODE in asked
    if wants_place and (place := _fact(facts.get("location"), text, problems, "location")):
        if names_a_town(place.value):
            found.place = place
    if ASK_PAY in asked and (pay := _fact(facts.get("pay"), text, problems, "pay")):
        low, high = parse_comp_text(pay.quote, part_time=is_part_time(title, pay.quote))
        if low and high and posting_facts.PAY_FLOOR <= high <= posting_facts.PAY_CEILING:
            found.pay = Found(f"${low:,}-${high:,}" if low != high else f"${low:,}", pay.quote, HAIKU)
            found.bands = [(low, high, pay.quote)]
        else:
            problems.append("pay: the quote has no range the app can read as a year's pay")
    if ASK_BD in asked:
        found.leads_bd = _bd_fact(facts.get(ASK_BD), text, problems, title)
    if ASK_MW in asked:
        found.main_work = _main_work_fact(facts.get(ASK_MW), text, title, list(mw or []), problems)
    return found


async def take_answers(session: AsyncSession, owner: str, answers: list[dict], session_id: str | None = None) -> dict:
    """Take a Haiku session's answers about the roles it was handed (`item`). A role must have been handed and still be
    waiting for its read. The facts that stand on a quote are put on the role where it holds none (`fill`), and the
    gates are decided again: a fail moves the role to Gated out with the quote on its history. The ones that cannot
    be taken are named and the rest kept. Returns `{taken, stopped, problems}`. Services flush; the caller commits."""
    rules = await gating.rules(session, owner)
    pending = {short_id(r): r for r in await waiting(session, owner)
               if int((_stored(r).get("haiku") or {}).get("handed") or 0) > 0}
    taken = stopped = 0
    problems: list[str] = []
    seen: set[str] = set()
    for raw in answers:
        key = str(raw.get("id") or "").strip().lower()[:ID_CHARS] if isinstance(raw, dict) else ""
        role = pending.get(key)
        if role is None or key in seen:
            problems.append(f"{key or '?'}: not a role you were handed, or already answered")
            continue
        seen.add(key)
        text = (await posting_text.held(session, role)).text
        asked = list((_stored(role).get("haiku") or {}).get("asks") or [])
        local: list[str] = []
        asked_words = list((_stored(role).get("haiku") or {}).get("mw") or [])
        found = read_answer(text, raw, asked, local, role.title, asked_words)
        problems += [f"{key}: {p}" for p in local]
        if in_play(role):  # stored, never decided: no gate runs and the role keeps its stage
            held = _stored(role)
            kept = {k: v for k, v in held.items() if k != "mwq"}
            role.gate_facts = {**kept, "facts": {**(held.get("facts") or {}), **found.facts()},
                               "haiku": {**(held.get("haiku") or {}), "answered": _now().isoformat()}}
            taken += 1
            if found.main_work is not None and found.main_work.value != "none":
                await trail.record(
                    session, role.owner, event="gate_facts_read", actor="app", who=trail.APP_WHO,
                    link=("opportunity", role.id), sentence="Read the gate facts from the posting's header lines.",
                    detail=_evidence({}, [(ASK_MW, found.main_work)]))
            continue
        changes = fill(role, found)
        said_word = found.main_work is not None and found.main_work.value != "none"
        shown = [*changes, *([(ASK_BD, found.leads_bd)] if found.leads_bd is not None else []),
                 *([(ASK_MW, found.main_work)] if found.main_work is not None and said_word else [])]
        copy = await cleansed.gate_copy_of(session, role.owner, role.id)
        posted = await freshness.posted_of(session, role)
        # The Haiku read was the last reader: a way of working the posting still does not state is assumed on site.
        read = gating.silent_copy(copy)
        stop = decide(role, rules, found, copy, posted, read)
        facts = {**(_stored(role).get("facts") or {}), **found.facts()}
        done = {**_stored(role), "fp": fingerprint(role, text), "at": _now().isoformat(), "state": SETTLED,
                "facts": facts, "haiku": {**(_stored(role).get("haiku") or {}), "answered": _now().isoformat()},
                "pay_floor": rules.gates.comp_floor, gating.FACTS_WAY: read,
                "pay_gate": pay_mark(_subject(role, rules, found, copy, posted, read), rules)}
        taken += 1
        evidence = _evidence(facts, shown)
        if stop is not None:
            role.gate_facts = {**done, "state": GATED, "stop": stop.gate_id}
            await gate_out(session, role, stop, rules, evidence=evidence.lstrip("; "), how=_read_how(stop, rules))
            stopped += 1
            continue
        role.gate_facts = done
        if changes or said_word or (found.leads_bd is not None and found.leads_bd.value == "yes"):
            await trail.record(
                session, role.owner, event="gate_facts_read", actor="app", who=trail.APP_WHO,
                link=("opportunity", role.id), sentence="Read the gate facts from the posting's header lines.",
                detail=evidence.lstrip("; "))
    await session.flush()
    return {"taken": taken, "stopped": stopped, "problems": problems[:20]}


# --- the step of the analysis process --------------------------------------------------------------------------------


async def action_gate_facts(session, instance, data):
    """The step "Read the gate facts" of `analyse_role.bpmn`: after the posting's text is held and before the analyst is
    sent the role, the cheap gates are decided on the posting's facts. A role stopped here is Gated out and the process
    ends; a role whose Haiku read is still to come stays at the analyst's step, held back from the line for `HOLD`
    (`held_back_ids`); every other role goes on."""
    role = await session.get(Opportunity, uuid.UUID(data["role"]))
    if role is None:
        return {"stage": stages.SECOND_IMPRESSION}
    try:
        async with session.begin_nested():
            outcome = await process(session, role, inside=True)
    except Exception:
        log.warning("reading the gate facts of role %s failed", role.id, exc_info=True)
        return {"stage": stages.SECOND_IMPRESSION}
    # Only a role this step stopped ends the process here: one that was Gated out already (the owner asked for its
    # research) goes on to the analyst as it always did.
    # Never the role's own status: a role already Gated out (asked for by the owner) must go on to the analyst.
    return {"stage": stages.GATED_OUT if outcome.state == STOPPED else stages.SECOND_IMPRESSION,
            "state": outcome.state}
