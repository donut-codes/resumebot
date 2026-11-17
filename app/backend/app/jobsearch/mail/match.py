"""Matching a message to a role (docs/PRD.md 14.5), on go_mail's matcher (`go_mail.match`: the employer by its whole
name in the message or the sender's address, or by an alias of one word with the role beside it, and the title in
the message's words).

A message is matched against the roles still in play (any stage before the end, and Gone Quiet, whose reply moves it
out: 14.6). `go_mail.match` gives each role `SURE` (its employer and its whole title), `UNSURE` (its employer, with
part of the title or none) or nothing. Then, as 14.5 reads:

- the message holds one role's whole title (of two titles where one holds the other, the longer): that role;
- it names no title, and the kind of mail moves or ends a role (a confirmation, a rejection): only a role that kind may
  act on is meant (`can_take`), and when exactly one is, it is the role. A confirmation may move to Applied only a
  role the owner applied to or Launch opened, or one in Ready to Launch; a rejection may end only a role in Applied or
  Active Discussion, or one Gone Quiet that the owner applied to (the owner, 2026-11-04: a role found by a search and
  never applied to was moved to Applied by an update that named no job). With none such, or several, the candidates
  are returned and the owner decides;
- any other kind that names no title: one role at the employer is the role, whatever the title (nothing else could be
  meant), and several are candidates;
- otherwise nothing is guessed: the candidates are returned and the owner decides.

**Who sent it counts first** (docs/PRD.md 14.5). A message names an employer's role only when it comes from that
employer's own domain (the registered domain of its website, its careers page or a site of its own) or from a known job
system's mail domain (`kinds.JOB_SYSTEMS`); a job board's (`kinds.JOB_BOARDS`) only when its subject speaks of an
application, as its alerts and digests name employers too (`kinds.may_name_a_role`). A newsletter, a job alert or a
forward that merely names the employer, or mail from anyone else, is not about the role: the roles it names are
returned as candidates, marked `unverified`, and the owner decides (the same Inbox decision); nothing moves or ends on
it.

Read-only; the database is read for roles, employers and their sites and nothing is written."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import stages
from app.jobsearch.mail import kinds as mail_kinds
from app.jobsearch.mail import reader as go_mail
from app.jobsearch.models import Employer, Opportunity, StageMove
from app.mailmine.message import Seen
from app.sites.models import Site

#: The roles a message can be about: in play, or Gone Quiet (a reply from the employer moves it out).
IN_PLAY = (*stages.OPEN, stages.GONE_QUIET)
#: The most candidates a decision lists.
MOST_CANDIDATES = 6


@dataclass
class Candidate:
    """A role, the names its company and employer record go by, and the registered domains of the employer's own sites
    (`own_domains`: where its mail comes from)."""

    role: Opportunity
    names: go_mail.Names
    domains: frozenset[str] = frozenset()
    #: Whether the role was Applied at some time (a move into that stage, or out of it, is on record), so a role Gone
    #: Quiet, or moved back by hand, is still one the owner applied to.
    applied_before: bool = False


@dataclass
class Match:
    """What matching found: `role` when exactly one role is meant, else `candidates` (several that fit, nothing
    guessed, or the roles a message names though it did not come from their employer or a job system: `unverified`) or
    neither (no role of an employer the message names)."""

    role: Opportunity | None = None
    candidates: list[Opportunity] = field(default_factory=list)
    unverified: bool = False


def own_domains(employer: Employer | None, site_urls: Iterable[str | None] = ()) -> frozenset[str]:
    """The registered domains an employer's own mail comes from: its website, its careers page and the sites of its own
    (`site_urls`). Not a job system's host, a job board's or a vendor's, which every employer of theirs shares
    (`pairs.domain_of`)."""
    from app.jobsearch.employers import pairs  # employers reads much of the job search

    urls = [*site_urls, *((employer.website, employer.careers_url) if employer is not None else ())]
    return frozenset(d for url in urls if (d := pairs.domain_of(url)))


async def candidates_of(session: AsyncSession, owner: str) -> list[Candidate]:
    """Every role in play with the names its employer goes by (the company, and its employer record's name and
    aliases) and the domains its employer's own sites are on."""
    roles = (await session.scalars(select(Opportunity).where(
        Opportunity.owner == owner, Opportunity.status.in_(IN_PLAY)))).all()
    ids = {r.employer_id for r in roles if r.employer_id}
    employers = {e.id: e for e in (await session.scalars(select(Employer).where(
        Employer.owner == owner, Employer.id.in_(ids)))).all()}
    sites: dict[uuid.UUID, list[str]] = {}
    for employer_id, url in (await session.execute(select(Site.employer_id, Site.url).where(
            Site.owner == owner, Site.employer_id.in_(ids)))).all():
        if employer_id is not None:
            sites.setdefault(employer_id, []).append(url)
    # Every role the owner ever applied to: a move into Applied, or out of it (a role from before the app recorded its
    # coming in has only that). A handful, read by owner and stage and not by a list of the roles in play: such a list
    # binds a value for each role, and asyncpg refuses a statement of more than 32767.
    applied = set((await session.scalars(select(StageMove.role_id).where(
        StageMove.owner == owner,
        or_(StageMove.to_status == stages.APPLIED, StageMove.from_status == stages.APPLIED)).distinct())).all())
    out = []
    for role in roles:
        employer = employers.get(role.employer_id) if role.employer_id else None
        own: list[str | None] = [role.company, employer.name] if employer is not None else [role.company]
        aliases = [str(a) for a in (employer.aliases or [])] if employer is not None else []
        domains = own_domains(employer, sites.get(role.employer_id, ()) if role.employer_id else ())
        out.append(Candidate(role, go_mail.employer_names(own, aliases), domains, applied_before=role.id in applied))
    return out


def all_keys(candidates: list[Candidate]) -> list[str]:
    """Every employer key of the roles in play (the header filter's: mail from an employer's own address is read)."""
    return sorted({k for c in candidates for k in (*c.names.keys, *c.names.loose)})


def from_the_employer(seen: Seen, candidate: Candidate) -> bool:
    """Whether the message came from the role's employer: the sender's domain is one of its own sites' (or a subdomain
    of one). A sender at a mailbox provider (a person's own address, the owner's forward) is no employer's."""
    sender = seen.sender_key.lower()
    return "@" not in sender and any(sender == d or sender.endswith("." + d) for d in candidate.domains)


#: The stages a mail naming no title may be about, by kind: a confirmation, a role the owner has applied to (or is in
#: Ready to Launch, or Launch opened); a rejection, a role that is Applied or in Active Discussion, or Gone Quiet after
#: the owner applied to it (docs/PRD.md 14.6: a rejection takes it to Rejected).
_APPLIED_TO = (stages.READY_TO_LAUNCH, stages.APPLIED, stages.ACTIVE_DISCUSSION)
_STILL_ALIVE = (stages.APPLIED, stages.ACTIVE_DISCUSSION)
#: The kinds whose move on a guess is a wrong one: they act only on a role they can be about (`can_take`).
NO_GUESS_KINDS = (mail_kinds.CONFIRMATION, mail_kinds.REJECTION)


def can_take(kind: str | None, candidate: Candidate) -> bool:
    """Whether a mail of this kind that names no job title may be about this role. A confirmation: the owner applied to
    it (it is Applied or in Active Discussion, or a move into or out of Applied is on record) or Launch opened it
    (`go_opened_at`), or it is in Ready to Launch. A rejection: it is Applied or in Active Discussion, or Gone Quiet
    with a move into or out of Applied on record (a role whose posting closed before the owner applied is not). Any
    other kind: any role."""
    role = candidate.role
    if kind == mail_kinds.CONFIRMATION:
        return role.status in _APPLIED_TO or role.go_opened_at is not None or candidate.applied_before
    if kind == mail_kinds.REJECTION:
        return role.status in _STILL_ALIVE or (role.status == stages.GONE_QUIET and candidate.applied_before)
    return True


def match(seen: Seen, candidates: list[Candidate], kind: str | None = None) -> Match:
    """Which role in play a message of this kind is about (see the module's docstring). `kind` is what the message was
    classed as; None or a kind that moves nothing on a guess keeps the one-role-at-the-employer rule."""
    named_by = [(c, said) for c in candidates if (said := go_mail.match(seen, c.names, c.role.title)) is not None]
    if not named_by:
        return Match()
    if mail_kinds.may_name_a_role(seen):
        named = named_by
    elif theirs := [(c, said) for c, said in named_by if from_the_employer(seen, c)]:
        named = theirs  # from an employer's own address: it is about that employer's roles, not another it names
    else:
        return Match(candidates=[c.role for c, _ in named_by][:MOST_CANDIDATES], unverified=True)
    sure = [c.role for c, said in named if said == go_mail.SURE]
    # "Product Manager" is in "Senior Product Manager": a message with the longer title is about the longer, not both.
    whole = [r for r in sure if not any(r.id != o.id and _held(r.title, o.title) for o in sure)]
    if len(whole) == 1:
        return Match(role=whole[0])
    if not sure and kind in NO_GUESS_KINDS:
        # The message names no job: only a role this kind may act on is meant, and it must be the only one.
        fits = [c for c, _ in named if can_take(kind, c)]
        if len(fits) == 1:
            return Match(role=fits[0].role)
        return Match(candidates=[c.role for c in (fits if len(fits) > 1 else [c for c, _ in named])][:MOST_CANDIDATES])
    if len(named) == 1:
        return Match(role=named[0][0].role)
    return Match(candidates=[c.role for c, _ in named][:MOST_CANDIDATES])


def _held(short: str, long: str) -> bool:
    """Whether the short title is inside the longer one (and is not the same)."""
    a, b = go_mail._fold(short).strip(), go_mail._fold(long).strip()  # pyright: ignore[reportPrivateUsage]
    return a != b and f" {a} " in f" {b} "
