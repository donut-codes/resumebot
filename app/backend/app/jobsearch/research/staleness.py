"""Which results are out of date, and why (the owner, 2026-10-19).

A research keeps the versions of the inputs it was made from, when it is made (`stamp_analysis`:
`Opportunity.analysis_made_under`). `analysis_reasons` compares those with what is in force now and says, in words,
which input changed and touches this result. Nothing re-does anything here: `requeue.reanalyse_stale` reads the answer.
A packet keeps no such versions.

The rules (each input is compared only as far as the result depends on it):
- **The master resume, by line.** A result cites master lines by id. It is out of date only when a line it cites was
  dropped or its words changed since the version it was made under (read from that stored version). An edit to a line
  it does not cite changes nothing.
- **What to look for (criteria).** Out of date when the company text or the target role the result was made for changed.
  A change to another tab, or to the positioning, does not.
- **Hard pass (gates).** Out of date when a sentence rule the analyst answers for the role was added or reworded. A pay
  floor, place or any other gate is decided in code from the stored facts (`regate.py`) and needs no new research.
- **Prioritization (scoring).** Out of date when an ask a reader answers for the role was added or reworded. A change
  to points or to what the app decides itself is worked out again in code (`prioritize.after_save`).
- **The instruction file.** Compared whole, by version number: the part is too mixed to say cheaply what a change
  touches. An instruction file counts only from the owner's first save (version 1 is the shipped file; a release that
  ships other words is not a version).
- **No stamp, or no stamp for an input: unknown, never stale by itself.** Old rows stay as they are; the first time one
  is done again it is stamped. A stamped version that is no longer stored is unknown too.

Services flush; the caller commits."""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.models import Opportunity, ProfileVersion
from app.jobsearch.profile import documents, schema
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import instruction_files, role_match

#: The instruction file the analyst is sent.
ANALYST_FILE = "jobsearch.analyst"

#: The master's sections an analysis cites lines from.
ANALYSIS_SECTIONS = ("required", "preferred", "responsibilities")


#: The edition of the instruction file as shipped, 1 until a release rewords what a result was made under. A file with
#: no saved version is "version 1" however its words change, so a release that must send the roles back to be read again
#: raises this number: it is added to the stamp (`_instructions`), so research made under the earlier edition is out of
#: date by the version stamp, like any other change. 2: the analyst's Proven rule (a soft trait or a years ask the cited
#: lines meet is Proven), 2026-10-21.
SHIPPED_EDITION = {ANALYST_FILE: 2}


async def _instructions(session: AsyncSession, owner: str, ref: str) -> int:
    version = (await instruction_files.resolve(session, owner, "instructions", ref)).version
    return version + SHIPPED_EDITION.get(ref, 1) - 1


async def stamp_analysis(session: AsyncSession, owner: str) -> dict[str, int]:
    """The versions in force now of everything a research is made from, to be kept on the role when it is taken."""
    return {
        "resume": await profile_store.newest_version(session, owner, "resume"),
        "criteria": await profile_store.newest_version(session, owner, "criteria"),
        "gates": await profile_store.newest_version(session, owner, "gates"),
        "scoring": await profile_store.newest_version(session, owner, "scoring"),
        "instructions": await _instructions(session, owner, ANALYST_FILE),
    }


@dataclass
class Now:
    """What is in force now, and the stored versions looked at so far (a pass over many results reads each once)."""

    session: AsyncSession
    owner: str
    stamps: dict[str, int]
    profile: schema.Profile
    docs: documents.Documents
    kept: dict[tuple[str, int], Any] = field(default_factory=dict)

    @classmethod
    async def load(cls, session: AsyncSession, owner: str) -> "Now":
        return cls(session, owner, await stamp_analysis(session, owner), await profile_store.current(session, owner),
                   await profile_store.owner_documents(session, owner))

    async def at(self, kind: str, version: int) -> Any:
        """The document `kind` as it was at `version`, or None when that version is not stored."""
        key = (kind, version)
        if key not in self.kept:
            row = await self.session.scalar(select(ProfileVersion).where(
                ProfileVersion.owner == self.owner, ProfileVersion.kind == kind, ProfileVersion.version == version))
            self.kept[key] = profile_store.parse(kind, row.data) if row is not None else None
        return self.kept[key]


def _changed(stamped, now: int) -> bool:
    """Whether a stamped version is a number and not the one in force. No stamp is unknown, not changed."""
    return isinstance(stamped, int) and stamped != now


async def _lines(now: Now, stamped: int | None, cited: list[str]) -> list[str]:
    """What happened to the master lines a result cites, since the master version it was made under."""
    if not cited or not isinstance(stamped, int) or not _changed(stamped, now.stamps["resume"]):
        return []
    was = await now.at("resume", stamped)
    if was is None:
        return []
    before, after = documents.master_texts(was), documents.master_texts(now.docs.resume)
    out = []
    for line in dict.fromkeys(cited):
        if line not in before:
            continue  # it was never in that version: nothing to compare
        if line not in after:
            out.append(f"the master resume line {line} it cites was dropped")
        elif " ".join(before[line].split()) != " ".join(after[line].split()):
            out.append(f"the master resume line {line} it cites was changed")
    return out


def cited_by_analysis(role: Opportunity) -> list[str]:
    """The master line ids a research cites: the `from` of each item it classed Proven or Transferable."""
    found = role.analysis or {}
    return [i for section in ANALYSIS_SECTIONS for item in found.get(section) or [] if isinstance(item, dict)
            for i in item.get("from") or [] if isinstance(i, str)]


def _target(criteria: schema.Criteria, target_role: str | None) -> dict | None:
    role = criteria.role(target_role) if target_role else None
    return role.model_dump(mode="json") if role is not None else None


async def analysis_reasons(session: AsyncSession, role: Opportunity, now: Now | None = None) -> list[str]:
    """Why a role's research is out of date, one sentence for each input that changed and touches it; none when it is
    fresh, or when its versions are unknown (kept before they were recorded)."""
    made = role.analysis_made_under
    kept = role.analysis_state == "done" or (role.analysis_state is None and role.analysed_at is not None)
    if not isinstance(made, dict) or not kept:  # a role researched, then taken out of the line, kept its research
        return []
    return await _reasons(session, role, made, cited_by_analysis(role), now)


async def _reasons(session: AsyncSession, role: Opportunity, made: dict, cited: list[str],
                   now: Now | None) -> list[str]:
    now = now or await Now.load(session, role.owner)
    out = await _lines(now, made.get("resume"), cited)
    target = role.target_role
    if _changed(made.get("criteria"), now.stamps["criteria"]) and (
            was := await now.at("criteria", made["criteria"])) is not None:
        if was.company != now.profile.criteria.company:
            out.append("What to look for: the company text changed")
        # A target role the code or Haiku pass gave is not the analyst's reading, so a change to it is no reason to read
        # the role again; the analyst's or the owner's choice of it still is.
        if (target and role.matched_by not in (role_match.CODE, role_match.HAIKU)
                and _target(was, target) != _target(now.profile.criteria, target)):
            out.append(f"What to look for: its target role {target} changed")
    if _changed(made.get("instructions"), now.stamps["instructions"]):
        out.append(f"the analyst's instructions changed (version {made['instructions']} to "
                   f"{now.stamps['instructions']})")
    return out
