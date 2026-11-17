"""Pairs of employer records that may be one employer, and what is done about each (docs/PRD.md 36.4 and 36.5).

**A pair is a candidate** (`candidates`) when the two records
- share a website: the same registered domain in their websites (never a job system's host, tenant or not, a job
  board's or a site anyone can put a page on: those say nothing of whose it is); or share a careers site: the same
  registered domain in a careers page, or a careers page on the other's website, or
- post on the same job-system account (below) and their names share a distinctive word, or
- have names that are alike (`names_alike`), counting the other names each goes by.

**Names.** A name is read as `employers.key_of` reads it: lowercase, punctuation and legal endings ("Inc.", "Group",
"Holdings", "Company", "Companies") out, and a few filler words ("of", ".com"), its own acronym beside its long form
("AE (Acme Exchange)" reads as the long form) and its "via" tag ("Acme (via Staffco)" names
another company) left out too. Each word then weighs by how rare it is among the owner's employers (`Rarity`): one
over the number of records whose names hold it, so a word in many names ("Summit", "Capital") counts for little, and a
descriptor (`DESCRIPTORS`: "Insurance", "Bank", "Health", "Hospital", "University" and the like, which say what kind of
employer, not which) never counts for more than a common word. Two names are alike only when they are near-identical:
- the same words in any order ("GRID (Grand River Independent Distributor)" and "Grand River Independent Distributor
  (GRID)"), or
- one is the other plus only descriptors at its end ("Brightwater" and "Brightwater Insurance"), or
- the words they share hold at least `NEAR_IDENTICAL` of the weight of all their words,
and they share at least one distinctive word. "American Express" and "American Express Global Business Travel" are not
alike ("Global Business Travel" is another company's name, not a descriptor); nor are two names whose shared words are
common ("Summit Group" and "Pine Summit"). Names alone never merge; alike names alone are two employers, and any
lesser likeness is no pair at all.

**The same job-system account** is the same customer's own tenant: `resolve.board_key` says so (the system and an
address that is one customer's own: a tenant host, an employer's slug on a shared board, a customer's own host)
and it was not only guessed from the employer's name. Beside it the names need share only one distinctive word. The
same account under names that share none is a board wrongly given to one of them, not a pair. A job board
and a job system's host with no tenant are no evidence at all.

**What is done** (`settle`, run after an import and after the night's read) is decided for each pair by one table
(`employer_match.decide`, first match wins); the researcher's word (`propose_merge`) goes through the same table. The
owner is never asked whether two employers are one (the owner, 2026-11-13): a pair that meets the merge rules is merged
by code, and every other pair is two employers.
- Two records are merged by code (`employer_records.merge`) when their names read the same; or they share a website and
  their names are alike; or they share a website that the owner or the employer researcher confirmed for both
  (`website_confirmed`); or they post on the same job-system account and their names share a distinctive word. A
  website alone is not enough: the per-role analyst sets one while reading a single posting, and two employers can
  share one (a brand and its parent, a chapter of an association and a company).
- Every other pair is two employers: a pair the owner or the researcher said to keep separate; an agency with the
  client it hired for; two records whose websites are on different domains, both confirmed; a brand and its parent; a
  record whose name carries a "via" tag; alike names and nothing else; a website shared but not confirmed; a careers
  site shared. No question is raised and nothing is remembered for them (except the researcher's "different" and an
  agency, which are remembered as separate), so if later evidence meets a merge rule (the researcher confirms both
  websites; a board turns out to be one tenant) the pair is merged then, by code. `candidates` leaves out only what
  the app knows is two employers (decided, agency, contradicted); the rest stay listed, because the researcher's brief
  offers them.
- A merge that fails changes nothing and is logged; the next pass tries again.

A merge task keyed by the pair (`KIND_MERGE`) is now only what was raised before this rule, or the researcher's
"different" kept as a finished task, which is how a pair is remembered as separate. `settle` closes every merge
question still open with a note (`WITHDRAWN`): it is not the owner's answer, so the pair is not taken as decided. An
answer the owner gives to one before it is closed is still carried out by `apply_answer`. Merges already made are never
undone. Services flush; the route or the night's step commits.

**A split** (`propose_split`) is the researcher's word that some roles at a record are another company's; it is the
only thing that starts one, since code finds no split on its own. Code moves a role only when the role's own posting
address shows the other employer (`split_evidence`): where applying happens (`Opportunity.apply_url` and its postings'),
else where it is listed, is on the other employer's job-system account (its own tenant, carrying its name), or on its
confirmed website, or, where no record of it exists, on an account or a site named for the new company. The
researcher's word and a role's company text are never evidence. Each role is judged alone and a group moves in part;
a role that fails the test stays, nothing is asked and nothing is remembered, so the same claim moves the role if the
researcher sends it again once the facts are there. An answer the owner gave to an older split question (`KIND_SPLIT`)
is still remembered, and `settle_splits` closes the questions an earlier release left open, moving the roles whose
postings show it.
"""

import hashlib
import logging
import re
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.inbox import service as inbox
from app.jobsearch import ats
from app.jobsearch.actors import BY_APP, BY_OWNER
from app.jobsearch.employers import core as employers
from app.jobsearch.employers import follow as employer_posting
from app.jobsearch.employers import match as employer_match
from app.jobsearch.employers import records as employer_records
from app.jobsearch.employers import resolve
from app.jobsearch.employers.core import EmployerError, NoSuchEmployer
from app.jobsearch.employers.match import Decision, Ties
from app.jobsearch.employers.records import LINK
from app.jobsearch.models import Employer, JobPosting, Opportunity
from app.jobsearch.postings.coop import Cooperate
from app.models.work import TASK_DONE, TASK_OPEN, TASK_SKIPPED, Task
from app.sites import avoid, registry

log = logging.getLogger(__name__)

#: The kinds of Inbox task (`Task.kind`) and the answers they take (`Task.options[].id`).
KIND_MERGE, KIND_SPLIT = "employer_merge", "employer_split"
KINDS = (KIND_MERGE, KIND_SPLIT)
MERGE, SEPARATE = "merge", "keep_separate"
SPLIT, TOGETHER = "split", "keep_together"
#: What the researcher says of a pair (`Evidence.verdict`).
SAME, UNSURE, DIFFERENT = "same", "unsure", "different"
#: What `propose_merge` and `propose_split` came to.
MERGED, KEPT_SEPARATE, KEPT_TOGETHER, SPLIT_DONE = "merged", "kept_separate", "kept_together", "split"

#: The most passes `settle` makes at merging: a merge can make a record share a website or a board with a third.
MAX_ROUNDS = 5
#: How much of a name and of one of the researcher's sentences a remembered answer repeats; how many sentences.
NAME_CHARS, REASON_CHARS, REASONS = 60, 240, 5
#: A word in the names of this many employers or more tells none of them from another ("Capital", "Summit").
COMMON_WORD = 5
#: Words that say what kind of employer a name is, not which one: a name that is another's plus only these at its end
#: is the same name ("Brightwater Insurance", "Riverton Health System"), and one never counts for more than a common
#: word. A short, generic list (a published convention, not anyone's setting); a word that is another company's name
#: ("Global Business Travel") is not on it.
DESCRIPTORS = frozenset({
    "insurance", "bank", "bancorp", "financial", "health", "healthcare", "system", "systems", "hospital", "hospitals",
    "medical", "dental", "center", "centre", "clinic", "university", "college", "school", "international", "global",
    "worldwide", "america", "americas", "services", "solutions", "technologies", "technology", "consulting", "partners",
    "enterprises", "industries", "labs", "laboratories", "foundation", "credit", "union"})
#: Two names are near-identical (`names_alike`) when the words they share carry at least this share of the weight of
#: all their words (`Rarity.weight`): what they do not share weighs no more than a quarter of what they do (a
#: descriptor or two, or a word many names hold). "Contoso Bank Riverton" and "Contoso Riverton" are 2 of 2.2;
#: "American Express" and "American Express Global Business Travel" about 1 of 3.2.
NEAR_IDENTICAL = 0.8
#: The answer stored on an open merge question closed by `settle` (not the owner's answer, so the pair is not
#: decided), and the note it carries.
WITHDRAWN = "withdrawn"
WITHDRAWN_NOTE = ("Closed by the app: it merges two employer records only when its matching rules show they are one "
                  "employer, and otherwise keeps them as two. Nothing was merged.")
#: The note on an open split question `settle_splits` closed: roles moved, or none (not the owner's answer).
SPLIT_MOVED_NOTE = ("Closed by the app: it moves roles to another employer only when their own postings show it. "
                    "{moved} moved, the rest stayed.")
SPLIT_KEPT_NOTE = ("Closed by the app: it moves roles to another employer only when their own postings show it, and "
                   "these do not. Nothing was moved.")
#: What two records have in common (`_signals`), in the order a card tells it.
REASONS_IN_ORDER = ("website", "careers", "board", "names", "word")
#: Words that tell one employer from another no better than "Inc." does, besides the legal endings (`employers._NOISE`).
_FILLER = frozenset({"a", "an", "of", "and", "for", "com", "net", "org"})
#: The tag a staffing firm's name follows: "Rivera Pharma (via Staffco)". (Not a name that begins with "Via".)
_VIA = re.compile(r"(?<=\S)[\s(]+via\b.*$", re.I)
#: The staffing firm a tag names: in brackets ("Globex (via Staffco)") or to the end of the text ("Globex via Staffco").
_VIA_IN_BRACKETS = re.compile(r"\(\s*via\s+([^()]+?)\s*\)", re.I)
_VIA_TO_THE_END = re.compile(r"(?<=\S)\s+via\s+(.+?)\s*$", re.I)
_BRACKETS = re.compile(r"\(([^()]*)\)")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: Hosts anyone can have a page on: a website on one says nothing of whose it is.
_OPEN_HOSTS = (
    "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "github.com", "github.io", "medium.com",
    "wikipedia.org", "crunchbase.com", "bloomberg.com", "wixsite.com", "squarespace.com", "weebly.com", "notion.site",
    "linktr.ee", "bit.ly",
    # A careers platform that hosts many employers' pages under the vendor's own domain, and has no runbook yet.
    "jobs.net")


def _shared_hosts() -> frozenset[str]:
    """Hosts anyone can have a page on, or that list other companies: a website on one says nothing of whose it is. The
    job boards' and the careers platforms' that host many employers' pages under the vendor's own domain, besides the
    job systems (`System.domains`), are the runbooks' (`registry`); a page there is the vendor's host, not the
    employer's own site."""
    return frozenset({*registry.board_hosts(), *registry.shared_hosts(), *_OPEN_HOSTS})


@dataclass(frozen=True)
class Evidence:
    """What the researcher found about a pair: its verdict (`SAME`, `UNSURE` or `DIFFERENT`) and, in plain sentences,
    what it found and where (the same headquarters on its own site; a parent and a subsidiary). The owner reads the
    sentences."""

    verdict: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Proposal:
    """What a proposal came to: `outcome` is `MERGED` (`employer` is the record left), `SPLIT_DONE` (roles moved;
    `employer` is the record they went to), `KEPT_SEPARATE` or `KEPT_TOGETHER` (the answer is remembered, or was
    already the owner's, or nothing was changed: `task` is the remembered answer, if any). Nobody is asked."""

    outcome: str
    task: Task | None = None
    employer: Employer | None = None


@dataclass
class Candidate:
    """A pair that may be one employer. `keep` is the record that would stay (the one with more roles), `decision` what
    the table says to do with the pair (`employer_match.decide`: whether code may merge it, the rule that decided and
    the words that say why) and `reasons` why the two look the same."""

    keep: Employer
    away: Employer
    decision: Decision
    reasons: list[str] = field(default_factory=list)


# --- names, websites, boards -----------------------------------------------------------------------------------------


def _without_acronym(words: list[str]) -> list[str]:
    """The words of a name without its own acronym: a first or last word of two to eight letters that is the initials of
    all the others, the filler words counted or not ("AE (Acme Exchange)", "Acme
    Exchange - AE" and "Bank of Riverton (BOR)" read as the long form). An acronym on a record of its
    own, with no long form beside it, is left as the name it is."""
    for at in (0, -1):
        if len(words) < 3:
            break
        word, others = words[at], (words[1:] if at == 0 else words[:-1])
        if 2 <= len(word) <= 8 and word.isalpha() and any(
                word == "".join(w[0] for w in group) for group in (others, [w for w in others if w not in _FILLER])):
            return others
    return words


def name_parts(name: str | None) -> tuple[str, ...]:
    """The words of a name that tell it from another, in order: lowercase, its "via" tag, its legal endings, its filler
    and its own acronym (`_without_acronym`) out."""
    text = employers._NOISE.sub(" ", _VIA.sub(" ", (name or "").lower()))
    return tuple(w for w in _without_acronym(re.sub(r"[^a-z0-9]+", " ", text).split()) if w not in _FILLER)


def has_via(name: str | None) -> bool:
    """Whether the name is tagged with the staffing firm it is hired through."""
    return bool(_VIA.search(name or ""))


def via_names(text: str | None) -> list[str]:
    """The staffing firms a name, or the company text of a role, says the role is hired through ("Globex (via Staffco)"
    and "Globex via Staffco" name Staffco)."""
    value = text or ""
    named = _VIA_IN_BRACKETS.findall(value)
    if not named and (rest := _VIA_TO_THE_END.search(value)):
        named = [rest.group(1)]
    return [n for n in named if employers.key_of(n)]


def domain_of(url: str | None) -> str | None:
    """The registered domain of an address that is an employer's own page: not a job system's host, whether or not a
    tenant is in it (a careers page kept from a board is on the system's, which every employer of that system shares;
    the root of a vendor's own domain names no one), not a job board's, not a site anyone can have a page on.
    None for any of those."""
    host = avoid.host_of(url)
    if (not host or ats.parse(url) is not None
            or any(avoid.covers(shared, host) for shared in _shared_hosts())):
        return None
    return employer_posting._base_domain(host)


def website_confirmed(employer: Employer) -> bool:
    """Whether the employer's website is its own as far as the app can tell: the owner set it (`owner_fields`), or the
    employer researcher did (`sources`, set under its name, `employer_research.BY`). The per-role analyst's word is not
    confirmation: it sets a website while reading one posting, and the page it read can be the posting's own site, so a
    page on that host says where it looked, not whose site it is."""
    from app.jobsearch.research import (
        employer as employer_research,  # it calls this module: imported here so the two never loop
    )

    if not employer.website:
        return False
    if "website" in (employer.owner_fields or []):
        return True
    said = (employer.sources or {}).get("website")
    return isinstance(said, dict) and said.get("by") == employer_research.BY


def _board(employer: Employer) -> tuple | None:
    """The job-system board an employer posts on, as something to compare: the system and its address when that is one
    customer's own (`resolve.board_key`, the one definition of the same board); None when it has none, only part of
    one, or one only guessed from the employer's name."""
    if not employer.ats or (employer.board or {}).get("confirmed", True) is False:
        return None
    return _own_board(employer.ats, employer.board or {})


def _own_board(system: str, parts: dict) -> tuple | None:
    """A job-system address as `_board` compares it: the system and the parts of one customer's own address, the values
    lowercased; None when it is not one customer's own (a vendor root, a host only)."""
    key = resolve.board_key(system, parts)
    return None if key is None else (key[0], tuple((k, str(v).lower()) for k, v in key[1]))


@dataclass(frozen=True)
class _Facts:
    """What a record is compared by, worked out once."""

    employer: Employer
    website: str | None  # the registered domain of its website
    careers: str | None  # the registered domain of its careers page
    board: tuple | None
    names: tuple[tuple[str, tuple[str, ...]], ...]  # each name it goes by (its own first), with its words in order
    keys: frozenset[str]  # the key of each name it goes by, and the key it is stored under
    via: frozenset[str]  # the keys of the staffing firms its names and its roles' company texts say it is hired through
    holds: tuple[tuple[str, str], ...]  # (a name it goes by, the key of a name in brackets in it), for each bracket

    @property
    def domains(self) -> set[str]:
        return {d for d in (self.website, self.careers) if d}


def _facts(employer: Employer, company_texts: Iterable[str] = ()) -> _Facts:
    """What the record is compared by. `company_texts` are the company texts of its roles that carry a "via" tag: a
    role's tag is kept there ("Globex (via Staffco)" is stored under Globex), not in the record's name."""
    called = (employer.name, *(employer.aliases or []))
    names = tuple((n, w) for n in called if (w := name_parts(n)))
    keys = frozenset(k for k in (employer.key, *(employers.key_of(n) for n in called)) if k)
    via = frozenset(
        k for text in (*called, *company_texts) for firm in via_names(text) if (k := employers.key_of(firm)))
    holds = tuple((n, k) for n in called for held in _BRACKETS.findall(n or "") if (k := employers.key_of(held)))
    return _Facts(employer, domain_of(employer.website), domain_of(employer.careers_url), _board(employer), names, keys,
                  via, holds)


@dataclass(frozen=True)
class Rarity:
    """How rare each word is among the owner's employers: `held` is how many records' names hold it (the name and the
    other names each goes by, a word counted once a record). Empty, every word not a descriptor weighs the same."""

    held: Mapping[str, int] = field(default_factory=dict)

    def weight(self, word: str) -> float:
        """One over the number of records whose names hold the word; a descriptor weighs no more than a common word."""
        weight = 1 / max(self.held.get(word, 1), 1)
        return min(weight, 1 / COMMON_WORD) if word in DESCRIPTORS else weight

    def distinctive(self, word: str) -> bool:
        """Whether the word tells one employer from another: held by fewer than `COMMON_WORD` records, and not a
        descriptor."""
        return word not in DESCRIPTORS and self.held.get(word, 1) < COMMON_WORD


#: No count of the owner's names: every word that is not a descriptor weighs the same, and is distinctive.
NO_RARITY = Rarity()


def _likeness(ones: tuple[str, ...], others: tuple[str, ...], rarity: Rarity) -> float:
    """The weight of the words two names share over the weight of all their words (`Rarity.weight`), 0 to 1."""
    every = set(ones) | set(others)
    total = sum(rarity.weight(w) for w in every)
    return sum(rarity.weight(w) for w in set(ones) & set(others)) / total if total else 0.0


def names_alike(first: tuple[str, ...], second: tuple[str, ...], rarity: Rarity = NO_RARITY) -> str | None:
    """Why two names (their words in order, `name_parts`) are near-identical, in words, or None. They share a
    distinctive word, and they use the same words, or one is the other plus only descriptors at its end, or the words
    they share hold `NEAR_IDENTICAL` of the weight of all of them."""
    (shorts, longs) = sorted((first, second), key=lambda w: (len(w), w))
    if not shorts or not any(rarity.distinctive(w) for w in set(shorts) & set(longs)):
        return None
    if set(shorts) == set(longs):
        return "the same words"
    if longs[:len(shorts)] == shorts and all(w in DESCRIPTORS for w in longs[len(shorts):]):
        return "begins"
    if _likeness(shorts, longs, rarity) >= NEAR_IDENTICAL:
        return "nearly"
    return None


def _name_link(first: _Facts, second: _Facts, rarity: Rarity) -> str | None:
    """Why the names of two records are alike (`names_alike`), in words, or None, counting every name each goes by."""
    for one, ones in first.names:
        for other, others in second.names:
            how = names_alike(ones, others, rarity)
            if how is None:
                continue
            (short, _s), (long, _l) = sorted(((one, ones), (other, others)), key=lambda n: (len(n[1]), n[0]))
            if how == "the same words":
                return f'"{short}" and "{long}" use the same words'
            if how == "begins":
                return f'"{long}" begins with "{short}"'
            return f'"{short}" and "{long}" are nearly the same name'
    return None


def _shared_word(first: _Facts, second: _Facts, rarity: Rarity) -> str | None:
    """A distinctive word (`Rarity.distinctive`) a name of each record holds, as the first record's name writes it, or
    None."""
    theirs = {w for _n, ws in second.names for w in ws}
    for name, words in first.names:
        for word in words:
            if word in theirs and rarity.distinctive(word):
                written = re.search(rf"(?i)(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", name or "")
                return written.group(0) if written else word
    return None


def _signals(first: _Facts, second: _Facts, rarity: Rarity = NO_RARITY) -> dict[str, str]:
    """What two records have in common, by kind (`website`, `careers`, `board`, `names`, `word`), each as a clause that
    finishes "they look the same because …": they share the website acme.test; they post on the same job
    board; "Brightwater Insurance" begins with "Brightwater"; both names hold "Acme". `rarity` says how rare a word is
    among the owner's employers (the pass over every record, `candidates`, knows; left out, no word is rare)."""
    out: dict[str, str] = {}
    if first.website is not None and first.website == second.website:
        out["website"] = f"they share the website {first.website}"
    else:
        careers = sorted(d for d in {first.careers, first.website} & {second.careers, second.website} if d)
        if careers:
            out["careers"] = f"they share the careers site {careers[0]}"
    if first.board is not None and first.board == second.board:
        out["board"] = f"they post on the same {ats.label(first.board[0])} job board"
    linked = _name_link(first, second, rarity)
    if linked:
        out["names"] = linked
    elif (word := _shared_word(first, second, rarity)) is not None:
        out["word"] = f'both names hold "{word}"'
    return out


def _contradiction(first: _Facts, second: _Facts) -> str | None:
    """The words that say two records are different employers by what was confirmed of them, or None: their websites
    are on different registered domains, and the owner or the researcher confirmed each (`website_confirmed`)."""
    if (first.website and second.website and first.website != second.website
            and website_confirmed(first.employer) and website_confirmed(second.employer)):
        low, high = sorted((first.website, second.website))
        return f"their websites are different, {low} and {high}, and you or the researcher confirmed both"
    return None


def _brand(first: _Facts, second: _Facts) -> tuple[str, str] | None:
    """The name of a brand and the name of its parent when a name of one holds the other's in brackets ("Fabrikam Dental
    (Initech)" is a brand of "Initech"), else None. The caller has seen the two names are not alike."""
    for brand, parent in ((first, second), (second, first)):
        for name, held in brand.holds:
            if held in parent.keys:
                return name, parent.employer.name
    return None


def _ties(first: _Facts, second: _Facts, kinds: dict[str, str], *, decided: bool) -> Ties:
    """What the table needs to know of two records besides what they share (`employer_match.Ties`)."""
    one, other = first.employer.name, second.employer.name
    return Ties(
        decided=decided,
        agency=bool(first.via & second.keys or second.via & first.keys),
        contradiction=_contradiction(first, second),
        brand=None if "names" in kinds else _brand(first, second),
        tagged=has_via(one) or has_via(other),
        same_name=bool(employers.key_of(one)) and employers.key_of(one) == employers.key_of(other),
        confirmed=website_confirmed(first.employer) and website_confirmed(second.employer))


async def _role_tags(session: AsyncSession, owner: str,
                     employer_ids: Iterable[uuid.UUID] | None = None) -> dict[uuid.UUID, list[str]]:
    """The company texts of the owner's roles that carry a "via" tag, by the record the role is at (all the records, or
    just `employer_ids`)."""
    query = select(Opportunity.employer_id, Opportunity.company).where(
        Opportunity.owner == owner, Opportunity.employer_id.is_not(None), Opportunity.company.ilike("%via%")).distinct()
    if employer_ids is not None:
        query = query.where(Opportunity.employer_id.in_(list(employer_ids)))
    out: dict[uuid.UUID, list[str]] = {}
    for employer_id, company in (await session.execute(query)).all():
        if employer_id is not None and has_via(company):
            out.setdefault(employer_id, []).append(company)
    return out


def _rarity_of(rows: Iterable[Employer]) -> Rarity:
    """How many of these records' names hold each word (`Rarity`), each word counted once a record."""
    return Rarity(Counter(w for e in rows for w in {w for n in (e.name, *(e.aliases or [])) for w in name_parts(n)}))


async def _rarity(session: AsyncSession, owner: str) -> Rarity:
    """How rare each word is among the names of all the owner's records."""
    return _rarity_of((await session.scalars(select(Employer).where(Employer.owner == owner))).all())


# --- finding the pairs ------------------------------------------------------------------------------------------------


def pair_key(first: uuid.UUID, second: uuid.UUID) -> str:
    """The key of the Inbox decision about a pair of records: the same whichever is named first."""
    low, high = sorted((str(first), str(second)))
    return f"employer-pair:{low}:{high}"


def _ids_in(key: str | None) -> list[uuid.UUID]:
    return [uuid.UUID(m) for m in _UUID.findall(key or "")]


async def decided(session: AsyncSession, owner: str) -> set[frozenset[uuid.UUID]]:
    """The pairs of records already decided about: a question about the pair that was answered or set aside, or a
    verdict of the researcher's. Whatever the answer was, and if it was "keep separate", the pair is not merged by
    code. A question the app closed without an answer (`WITHDRAWN`) is no decision."""
    keys = (await session.scalars(select(Task.key).where(
        Task.owner == owner, Task.kind == KIND_MERGE, Task.state != TASK_OPEN,
        or_(Task.answer.is_(None), Task.answer != WITHDRAWN)))).all()
    return {frozenset(ids) for ids in (_ids_in(key) for key in keys) if len(ids) == 2}


def _which(first: Employer, second: Employer, roles: dict[uuid.UUID, int]) -> tuple[Employer, Employer]:
    """The record that stays and the one merged into it: the one with more roles, else the older."""
    def rank(e: Employer):
        return (-roles.get(e.id, 0), e.created_at or datetime.max.replace(tzinfo=UTC), str(e.id))
    one, other = sorted((first, second), key=rank)
    return one, other


async def candidates(session: AsyncSession, owner: str) -> list[Candidate]:
    """Every pair of the owner's records that may be one employer, with why they look the same and what the table says
    to do with it (`employer_match.decide`: merge it, or it is two employers). Not a pair decided about already, an
    agency and the client it hired for, nor two records whose confirmed websites differ: the app knows those are two
    employers. Not a pair with nothing but a shared word, either. Reads only."""
    rows = list((await session.scalars(select(Employer).where(
        Employer.owner == owner, Employer.key != employers.PLACEHOLDER_KEY).order_by(  # "Added link" is no employer
        Employer.name, Employer.id))).all())
    if len(rows) < 2:
        return []
    roles: dict[uuid.UUID, int] = {e: n for e, n in (await session.execute(
        select(Opportunity.employer_id, func.count()).where(
            Opportunity.owner == owner, Opportunity.employer_id.is_not(None))
        .group_by(Opportunity.employer_id))).all() if e is not None}
    tags = await _role_tags(session, owner)
    facts = {e.id: _facts(e, tags.get(e.id, ())) for e in rows}
    coop = Cooperate()
    index: dict[tuple, list[uuid.UUID]] = {}  # what each record shares with whom: the only pairs worth comparing
    for f in facts.values():
        keys: set[tuple] = {("site", d) for d in f.domains} | {("word", w) for _n, ws in f.names for w in ws}
        if f.board is not None:
            keys.add(("board", f.board))
        for k in keys:
            index.setdefault(k, []).append(f.employer.id)
        await coop()
    rarity = _rarity_of(rows)
    near: dict[uuid.UUID, set[uuid.UUID]] = {i: set() for i in facts}
    for ids in index.values():
        await coop()
        for i in ids:
            near[i].update(ids)
    told = await decided(session, owner)
    out: list[Candidate] = []
    for a_id, others in near.items():
        await coop()
        for b_id in sorted(others, key=str):
            if str(a_id) >= str(b_id):
                continue
            a, b = facts[a_id], facts[b_id]
            kinds = _signals(a, b, rarity)
            if not (kinds.keys() & {"website", "careers", "names"} or kinds.keys() >= {"board", "word"}):
                continue  # a shared word alone is silence; one account under names with no word in common, a mistake
            decision = employer_match.decide(kinds, _ties(a, b, kinds, decided=frozenset((a_id, b_id)) in told))
            if decision.rule in (employer_match.DECIDED, employer_match.AGENCY, employer_match.CONTRADICTED):
                continue  # the app knows these are two employers; the rest stay listed for the researcher
            keep, away = _which(a.employer, b.employer, roles)
            out.append(Candidate(keep, away, decision, [kinds[k] for k in REASONS_IN_ORDER if k in kinds]))
    out.sort(key=lambda c: (c.keep.name.lower(), c.away.name.lower(), str(c.keep.id)))
    return out


# --- merging by code -----------------------------------------------------------------------


def _cut(text: str, most: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= most else text[:most - 1].rstrip() + "…"


async def settle(session: AsyncSession, owner: str) -> dict:
    """Merge the pairs the table says to merge (`employer_match.decide`), then close every merge question still open
    (`withdraw_stale`): the owner is never asked whether two employers are one, so any pair the table does not merge is
    two employers. Then close every split question an earlier release left open (`settle_splits`), moving the roles
    whose own postings show another employer. Safe to run again: a merged pair is gone, a closed question is not
    opened again. A merge that fails changes nothing and is logged; the next pass tries again (the pair is still a
    merge candidate). Returns {merged}."""
    merged = 0
    failed: set[frozenset[uuid.UUID]] = set()
    for _ in range(MAX_ROUNDS):
        todo = [(c.keep.id, c.away.id, c.decision.words) for c in await candidates(session, owner)
                if c.decision.action == employer_match.MERGE]
        done, touched = 0, set()
        for keep_id, away_id, why in todo:
            pair = frozenset((keep_id, away_id))
            if pair in failed or keep_id in touched or away_id in touched:  # a merge now changes what the next one sees
                continue
            touched.update(pair)
            try:
                done += (await employer_records.merge(session, owner, keep_id, away_id, why=why, by=BY_APP)).merged
            except Exception:  # a pair that cannot be merged is logged, and the night goes on
                log.exception("merging two employer records failed")
                failed.add(pair)
        merged += done
        if not done:
            break
    closed = await withdraw_stale(session, owner)
    if closed:
        log.info("closed %d employer merge question(s): the app does not ask whether two employers are one", closed)
    closed = await settle_splits(session, owner)  # after the merges: the record a split moves to is final
    if closed:
        log.info("closed %d employer split question(s): the app moves roles only when their postings show it", closed)
    return {"merged": merged}


async def nothing_to_settle(session: AsyncSession, owner: str) -> bool:
    """Whether `settle` would write nothing: no pair to merge and no merge or split question still open. Reads only, so
    it may
    be made on a reader's session; a merge is never decided from these rows (`settle` reads everything again under the
    turn)."""
    if any(c.decision.action == employer_match.MERGE for c in await candidates(session, owner)):
        return False
    return (await session.scalar(select(Task.id).where(
        Task.owner == owner, Task.kind.in_(KINDS), Task.state == TASK_OPEN).limit(1))) is None


def _withdrawn(task: Task | None) -> bool:
    """Whether a question was closed by the app, not answered (not a decision of anyone's)."""
    return task is not None and task.state != TASK_OPEN and task.answer == WITHDRAWN


async def withdraw_stale(session: AsyncSession, owner: str) -> int:
    """Close every open merge question, with a note that says why: the app merges two records only when its matching
    rules show they are one employer, and otherwise keeps them as two. Closed for the app, not the owner, and not
    taken as a decision (`decided`); no merge is undone. Returns how many."""
    rows = (await session.scalars(select(Task).where(
        Task.owner == owner, Task.kind == KIND_MERGE, Task.state == TASK_OPEN))).all()
    for task in rows:
        task.state, task.answer, task.done_at = TASK_SKIPPED, WITHDRAWN, datetime.now(UTC)
        task.assignee, task.note = BY_APP, WITHDRAWN_NOTE
    await session.flush()
    return len(rows)


# --- the answers ------------------------------------------------------------------------------------------------------


def _plan(task: Task, option: str) -> dict:
    """What the option of a decision carries out (`options[].plan`)."""
    for chosen in task.options or []:
        if chosen.get("id") == option:
            return chosen.get("plan") or {}
    return {}


async def apply_answer(session: AsyncSession, owner: str, task: Task, option: str) -> None:
    """Carry out the owner's answer to a decision about employers (the task is already marked done): "Merge" merges the
    pair, "Split them out" splits the roles out; the other answers change nothing, and the finished task is how they are
    remembered. A record that is gone is nothing to merge or split. Flushes; a refusal is an `EmployerError`."""
    plan = _plan(task, option)
    if not plan:
        return
    try:
        if task.kind == KIND_MERGE and option == MERGE:
            await employer_records.merge(session, owner, uuid.UUID(plan["keep"]), uuid.UUID(plan["away"]),
                                         why="you chose Merge in the Inbox", by=BY_OWNER)
        elif task.kind == KIND_SPLIT and option == SPLIT:
            await employer_records.split(
                session, owner, uuid.UUID(plan["from"]), role_ids=plan["roles"], names=plan.get("names") or (),
                new_name=plan["new_name"], why="you chose to split them out in the Inbox", by=BY_OWNER)
    except NoSuchEmployer:
        log.info("the employers in decision %s are gone; nothing to carry out", task.key)


async def forget(session: AsyncSession, owner: str, keep_id: uuid.UUID, away_id: uuid.UUID) -> None:
    """Settle the questions about a record that was merged away (`employer_records.merge` calls it): one still open
    about the record is withdrawn (nothing is asked again: the table decides the record that is left); one answered
    stays remembered, for the record that is left, and the answer to the question about the pair itself stays as it
    is."""
    rows = (await session.scalars(select(Task).where(
        Task.owner == owner, Task.kind.in_(KINDS), Task.key.like(f"%{away_id}%")))).all()
    for task in rows:
        others = [i for i in _ids_in(task.key) if i != away_id]
        if task.state == TASK_OPEN or _withdrawn(task) or (task.kind == KIND_MERGE and not others):
            await session.delete(task)
            continue
        if keep_id in others:  # the question about the pair itself, answered: it stays, the record of what was decided
            continue
        key = (pair_key(keep_id, others[0]) if task.kind == KIND_MERGE
               else (task.key or "").replace(str(away_id), str(keep_id)))
        if await inbox.get_task_by_key(session, owner, key) is not None:
            await session.delete(task)
        else:
            task.key = key
    await session.flush()


# --- what the researcher calls ----------------------------------------------------------------------------------------


def _said(evidence: Evidence) -> tuple[str, ...]:
    return tuple(_cut(r, REASON_CHARS) for r in evidence.reasons if str(r).strip())[:REASONS]


async def _remember_separate(session: AsyncSession, owner: str, c: Candidate, by: str, said: tuple[str, ...]) -> Task:
    """The pair is not one employer: a task already finished under the pair's key, for the researcher, so the pair is
    never asked about (and never merged by code). The researcher said it, or the app knows it (an agency and its
    client). A question about it still open is withdrawn."""
    key = pair_key(c.keep.id, c.away.id)
    open_one = await inbox.get_task_by_key(session, owner, key)
    if open_one is not None and (open_one.state == TASK_OPEN or _withdrawn(open_one)):
        await session.delete(open_one)
        await session.flush()
    task = await inbox.raise_task(
        session, owner, key=key, kind=KIND_MERGE, assignee=by, link=(LINK, c.keep.id),
        title=f'Not the same employer: "{_cut(c.keep.name, NAME_CHARS)}" and "{_cut(c.away.name, NAME_CHARS)}"')
    task.state, task.answer, task.done_at = TASK_DONE, SEPARATE, datetime.now(UTC)
    task.note = " ".join(said)[:1000] or None
    await session.flush()
    return task


async def propose_merge(session: AsyncSession, owner: str, first_id: uuid.UUID, second_id: uuid.UUID,
                        evidence: Evidence, *, by: str) -> Proposal:
    """The researcher's word that two records are, or are not, one employer (docs/PRD.md 36.5). The pair goes through
    the table the night's pass reads (`employer_match.decide`). `DIFFERENT`: remembered, and the pair is never a
    candidate again. `SAME` or `UNSURE` and the table says merge (names that read the same, a website beside alike
    names, a website confirmed for both, the same job-system account beside a shared distinctive word): merged. `SAME`
    or `UNSURE` otherwise: the pair is two employers (`KEPT_SEPARATE`), with nothing remembered and no question; a
    later pass merges it if new facts make the table say merge. An agency and the client it hired for are remembered
    as separate; two records whose confirmed websites differ are kept separate with nothing remembered; a pair the
    owner already decided about is left as they decided.
    Code checks the claim itself: the researcher's word alone never merges. `by` names the researcher in Activity and
    on what it remembers."""
    if evidence.verdict not in (SAME, UNSURE, DIFFERENT):
        raise EmployerError(f"a verdict is one of {SAME}, {UNSURE}, {DIFFERENT}")
    if first_id == second_id:
        raise EmployerError("that is the same employer")
    first = await employer_records.get(session, owner, first_id)
    second = await employer_records.get(session, owner, second_id)
    roles = {e.id: (await employer_records.tally(session, owner, e.id))["roles"] for e in (first, second)}
    keep, away = _which(first, second, roles)
    key, said = pair_key(first.id, second.id), _said(evidence)
    earlier = await inbox.get_task_by_key(session, owner, key)
    tags = await _role_tags(session, owner, (first.id, second.id))
    one, other = _facts(first, tags.get(first.id, ())), _facts(second, tags.get(second.id, ()))
    kinds = _signals(one, other, await _rarity(session, owner))
    told = earlier is not None and earlier.state != TASK_OPEN and not _withdrawn(earlier)
    decision = employer_match.decide(kinds, _ties(one, other, kinds, decided=told))
    if decision.rule == employer_match.DECIDED and earlier is not None:
        return Proposal(KEPT_SEPARATE, earlier)
    c = Candidate(keep, away, decision, [kinds[k] for k in REASONS_IN_ORDER if k in kinds])
    if evidence.verdict == DIFFERENT or decision.rule == employer_match.AGENCY:
        return Proposal(KEPT_SEPARATE, await _remember_separate(session, owner, c, by, said))
    if decision.rule == employer_match.CONTRADICTED:  # nothing remembered: a website corrected later can change it
        return Proposal(KEPT_SEPARATE)
    if evidence.verdict in (SAME, UNSURE) and decision.action == employer_match.MERGE:
        # The criteria hold, and `settle` would merge the pair anyway: the researcher's word only adds to the words.
        why = f"{decision.words}, and research found them the same" if evidence.verdict == SAME else decision.words
        return Proposal(MERGED, None, (await employer_records.merge(
            session, owner, keep.id, away.id, why=why, by=by)).employer)
    return Proposal(KEPT_SEPARATE)  # two employers: nothing remembered (later evidence can change it), nobody asked


def split_key(from_id: uuid.UUID, role_ids, new_name: str) -> str:
    """The key of the decision to split these roles out of a record under this name: the same roles and name, the same
    question."""
    mark = "|".join([*sorted(str(r) for r in employer_records.ids_of(role_ids, "a role")), employers.key_of(new_name)])
    return f"employer-split:{from_id}:{hashlib.sha1(mark.encode()).hexdigest()[:12]}"


@dataclass(frozen=True)
class Moves:
    """The roles of a split group whose own postings show another employer, and where they go: `into_id` is the record
    that exists (else a new one is made under `new_name`), `names` the alias of the old record that goes with them, and
    `why` the clause Activity gives after "Split N out of X into Y:"."""

    role_ids: list[uuid.UUID]
    into_id: uuid.UUID | None
    new_name: str
    names: list[str]
    why: str


@dataclass(frozen=True)
class _Address:
    """What one address of a role says of whose it is, and nothing else: its own job-system account (`_own_board`), its
    own website's registered domain (`domain_of`) and the names the two carry, run together (`resolve.spoken`)."""

    board: ats.Board | None
    tenant: tuple | None
    domain: str | None
    board_names: tuple[str, ...]
    site_name: str | None


def _read_address(url: str) -> _Address:
    found = ats.parse(url)
    board = found.board if found else None
    tenant = _own_board(board.system, board.parts) if board else None
    domain = domain_of(url)
    names = tuple(n for n in (resolve.spoken(label) for label in ats.board_labels(board.system, board.parts))
                  if len(n) >= 3) if board and tenant is not None else ()
    label = resolve.spoken(domain.split(".")[0]) if domain else ""
    return _Address(board, tenant, domain, names, label if len(label) >= 3 else None)


def _run_together(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", key.lower())


async def split_evidence(session: AsyncSession, owner: str, source: Employer, role_ids, new_name: str) -> Moves | None:
    """Which of these roles at `source` move to the employer `new_name`, by what their own postings show (docs/PRD.md
    36.6), or None when none does. Reads only. The researcher's word and a role's company text are never evidence.

    A role's addresses are where applying happens (`Opportunity.apply_url` and the `apply_url` of its postings at
    `source`), else, when it has none, where it is listed (their `url`). It STAYS when one of them points at `source`
    (its own job-system account or the registered domain of its website or careers page), or when its company text, the
    record's name or the new name carries a "via" tag (an agency: never split by code). Otherwise it MOVES when
    - S1: the employer `new_name` exists and an address is on its own job-system account, a board that carries its name
      (`resolve.owns`) or one named for it (S1b: a name on the address, equal to its key or an alias's); or
    - S2: the employer exists, its website is confirmed (`website_confirmed`) and an address is on that domain; or
    - S3: no such employer exists (a new record would be made) and a name on the address, or the first label of its
      domain, equals the new name read as a key reads it, at least three characters (equal, never a part of it).
    Nothing moves when the new name is no name, or when a role left behind is found under it or the old record's own
    key holds it: the merge table would put the records together again the same night."""
    key = employers.key_of(new_name)
    if not key or key == employers.PLACEHOLDER_KEY or has_via(source.name) or has_via(new_name):
        return None
    found = await employers.find(session, owner, new_name)
    target = found if found is not None and found.id != source.id else None
    ids = employer_records.ids_of(role_ids, "a role")
    roles = list((await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.id.in_(ids), Opportunity.employer_id == source.id))).all())
    posted: dict[uuid.UUID, list[JobPosting]] = {}
    for posting in (await session.scalars(select(JobPosting).where(
            JobPosting.owner == owner, JobPosting.employer_id == source.id,
            JobPosting.opportunity_id.in_([r.id for r in roles] or [None])))).all():
        if posting.opportunity_id is not None:
            posted.setdefault(posting.opportunity_id, []).append(posting)
    here = _facts(source)
    there = _facts(target) if target is not None else None
    called: set[str] = set()
    if target is not None:
        called = {n for n in (_run_together(employers.key_of(a)) for a in target.aliases or []) if len(n) >= 3}
        if len(_run_together(target.key)) >= 3:
            called.add(_run_together(target.key))
    wanted = _run_together(key)
    moving: list[uuid.UUID] = []
    why: list[str] = []
    for role in roles:
        if has_via(role.company):
            continue
        links = posted.get(role.id, [])
        urls = [u for u in (role.apply_url, *(p.apply_url for p in links)) if u] or [
            u for u in (role.url, *(p.url for p in links)) if u]
        reads = [_read_address(u) for u in urls]
        if any((a.tenant is not None and a.tenant == here.board) or (a.domain is not None and a.domain in here.domains)
               for a in reads):
            continue
        reason = None
        for a in reads:
            if target is not None and there is not None:
                if a.board is not None and a.tenant is not None and a.tenant == there.board \
                        and resolve.owns(target, a.board.system, a.board.parts):
                    reason = f"their own postings are on {target.name}'s job-system account"
                elif a.board_names and called.intersection(a.board_names):
                    reason = f"their own postings are on a job-system account named for {target.name}"
                elif a.domain is not None and website_confirmed(target) and a.domain == domain_of(target.website):
                    reason = f"their own postings are on {target.name}'s confirmed site"
            elif len(wanted) >= 3:
                if wanted in a.board_names:
                    reason = f"their own postings are on a job-system account named for {new_name}"
                elif a.site_name == wanted:
                    reason = f"their own postings are on a site named for {new_name}"
            if reason:
                break
        if reason:
            moving.append(role.id)
            why.append(reason)
    if not moving:
        return None
    staying = {employers.key_of(c) for c in (await session.scalars(select(Opportunity.company).where(
        Opportunity.owner == owner, Opportunity.employer_id == source.id, Opportunity.id.not_in(moving)))).all()}
    if key in staying or key == source.key:
        return None
    names = [a for a in source.aliases or [] if employers.key_of(a) == key]  # no role left behind goes by it
    return Moves(moving, target.id if target is not None else None, new_name, names, " and ".join(dict.fromkeys(why)))


def _close_split(task: Task, moved: int) -> None:
    """Close a split question the app settled itself: not the owner's answer, so not a decision (`_withdrawn`)."""
    task.state, task.answer, task.done_at = TASK_SKIPPED, WITHDRAWN, datetime.now(UTC)
    task.assignee = BY_APP
    task.note = SPLIT_MOVED_NOTE.format(moved=moved) if moved else SPLIT_KEPT_NOTE


async def _split_if_shown(session: AsyncSession, owner: str, from_id: uuid.UUID, role_ids, new_name: str,
                          *, by: str) -> employer_records.Split | None:
    """Split out the roles of the group whose own postings show the other employer (`split_evidence`); None when none
    moved."""
    source = await employer_records.get(session, owner, from_id)
    moves = await split_evidence(session, owner, source, role_ids, new_name)
    if moves is None:
        return None
    done = await employer_records.split(
        session, owner, from_id, role_ids=moves.role_ids, names=moves.names, into_id=moves.into_id,
        new_name=None if moves.into_id else moves.new_name, why=moves.why, by=by)
    return done if done.roles else None


async def propose_split(session: AsyncSession, owner: str, from_id: uuid.UUID, *, role_ids, new_name: str,
                        evidence: Evidence, by: str) -> Proposal:
    """The researcher's word that some roles at a record are another company's (an alias that folded a different
    company in; roles whose postings sit on two unrelated domains). Code moves the roles whose own postings show the
    other employer (`split_evidence`) and nothing else: `SPLIT_DONE` when at least one moved, else `KEPT_TOGETHER`
    with nothing asked and nothing remembered, so the same word moves them once the facts are there. The researcher's
    sentences (`evidence`) are not evidence and are not used. An older answer of the owner's to this question ("Keep
    together", or "Split them out") is remembered, and a question still open about it is closed. Refused
    (`EmployerError`) when the roles are not at the record or no name is given. `by` names the researcher."""
    del evidence  # the researcher's words are never evidence
    await employer_records.preview_split(session, owner, from_id, role_ids=role_ids, new_name=new_name)
    earlier = await inbox.get_task_by_key(session, owner, split_key(from_id, role_ids, new_name))
    if earlier is not None and earlier.state != TASK_OPEN and not _withdrawn(earlier):
        return Proposal(KEPT_TOGETHER, earlier)
    done = await _split_if_shown(session, owner, from_id, role_ids, new_name, by=by)
    if earlier is not None and earlier.state == TASK_OPEN:
        _close_split(earlier, done.roles if done else 0)
        await session.flush()
    if done is None:
        return Proposal(KEPT_TOGETHER)
    log.info("%s proposed a split of %d role(s) out of %s", by, done.roles, from_id)
    return Proposal(SPLIT_DONE, employer=done.employer)


async def settle_splits(session: AsyncSession, owner: str) -> int:
    """Close every split question still open (an earlier release asked them): the roles whose own postings show the
    other employer are moved (`split_evidence`), the rest stay, and the question is closed with a note that says which
    (`WITHDRAWN`: the app's, not the owner's answer, so it is no decision). One that cannot be carried out (the record
    or the roles are gone) is closed too; one failure never stops the rest. Returns how many were closed."""
    rows = (await session.scalars(select(Task).where(
        Task.owner == owner, Task.kind == KIND_SPLIT, Task.state == TASK_OPEN)
        .order_by(Task.created_at, Task.id))).all()
    todo = [(task, _plan(task, SPLIT)) for task in rows]  # read before any savepoint expires the rows
    for task, plan in todo:
        moved = 0
        try:
            async with session.begin_nested():
                done = await _split_if_shown(session, owner, uuid.UUID(plan["from"]), plan["roles"],
                                             plan["new_name"], by=BY_APP)
            moved = done.roles if done else 0
        except Exception:  # a question that cannot be carried out is closed, and the rest go on
            log.exception("settling a split question failed")
        _close_split(task, moved)
    await session.flush()
    return len(todo)
