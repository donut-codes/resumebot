"""The cleansed master (the owner, 2026-10-21): from each saved master version, a list of concise claims in place
of the full text, such as "Experience with program management (example: risk working group, quarterly review cycle)".
The master stays the source and is never edited; a claim is derived from it and versioned by it.

What a claim is (the owner's rules, 2026-10-21): one capability, one claim. Several jobs or lines that show the same
thing are one claim, their examples combined and every line id cited. It states no years at all. Its examples use the
master's own words, and nothing is added beyond the cited lines: a line that says work was pending is not stated as
done. A generic "e.g." template list in the master is not an example of work done. One Sonnet job of the kind `claims`
(a parse job, like the master's structured record) is queued when a master version is saved and writes them:
`{claim, examples, lines}`.

What code does: refuses a report whose cited id is not a line of this master (every problem named at once, so the
session can send it again), refuses "N years of ..." (a result that carries a time span is allowed), an "e.g."
list, and an example whose words are not in the lines the claim cites. Nothing the master marks "never in
writing" is sent to the session or cited, so no claim carries
it. Nothing here names a person, an employer or a skill: every claim is the owner's own data, read from their master.

Who reads them: the Haiku ranker's short profile (`rank.render_profile`: the claims alone, no ids) and the Second
Impression's master input (`second_impression_input`: the claims with their line ids, and the full text of the cited
lines, the whole master still to hand). Services flush; the route commits.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.models import ParseJob, StructuredRecord
from app.jobsearch.postings.record import SONNET, Field, Record, check_quotes
from app.jobsearch.profile import documents, integrity
from app.jobsearch.profile import store as profile_store
from app.jobsearch.resume.structured_profile import (
    ProfileError,
    job_ref,
    numbered_text,
    version_of,
    version_resume,
)
from app.jobsearch.states import ParseKind, ParseState

KIND = "claims"
#: A claim is a category, not a paragraph; an example is a few words.
MAX_CLAIM_CHARS, MAX_EXAMPLE_CHARS, MAX_EXAMPLES, MOST_CLAIMS = 100, 80, 8, 400
RULES_VERSION = 2
#: How many jobs one master version may have: a lost or failed one is made again, never without end.
MOST_TRIES = 3
_YEARS_WRITTEN = re.compile(r"\b\d+(?:\.\d+)?\+?\s*(?:years?|yrs?)\b", re.IGNORECASE)
_TEMPLATE_LIST = re.compile(r"\be\.?g\.?\b|\bfor example\b|\bsuch as\b", re.IGNORECASE)
_LEAD = re.compile(r"^experience (?:with|in|of)\s+", re.IGNORECASE)
_WORDS = re.compile(r"[^\W_]+(?:'[^\W_]+)*")

HOW = (
    "This is the owner's master resume, one line per id: `[id] text`. Condense it into claims and send them with "
    "jobs.parse_report: {id, fields} in the shape sent with it. One claim for each capability: `claim` is the "
    "category in a few words, the words that follow \"Experience with\" (\"data analysis\", \"customer "
    "onboarding\"), never a sentence and never a number of years. When several jobs or lines show the same "
    "capability, write one claim, combine their examples and cite every line. Two different capabilities are two "
    "claims. `examples` is a few brief examples, each in the master's own words, copied from the cited lines; add no "
    "example, fact, tool or figure a cited line does not state, and never state as done what a line says is pending "
    "or planned. A generic \"e.g.\" list in the master is a template, not work done: leave it out. `lines` is the ids "
    "of the lines the claim rests on, at least one. Use only ids that are in the text; a claim with none is refused. "
    "Then call jobs.parse_next again."
)


class ProblemsError(ValueError):
    """A report that breaks the rules: every problem, in one message."""


# --- what the master marks never in writing ----------------------------------------------------------------------


def hidden_ids(resume: documents.Resume) -> frozenset[str]:
    """The ids the session is not shown and a claim never cites: every line marked never in writing, and a marked
    role with the lines under it."""
    lines, others = integrity.marked_lines(resume)
    hidden = set(lines) | set(others)
    for role in resume.roles:
        if role.id in hidden:
            hidden |= {x.id for x in (*role.bullets, *role.evidence)}
    return frozenset(hidden)


def _all_texts(resume: documents.Resume) -> dict[str, str]:
    """Every line's text by id, and where one id is on several lines (the master is never changed) all their texts, one
    under another, so an example may quote either copy and the id cites both."""
    copies: dict[str, list[str]] = {}
    for ident, text in _each_line(resume):
        if text not in copies.setdefault(ident, []):
            copies[ident].append(text)
    return {k: "\n".join(v) for k, v in copies.items()}


def _each_line(resume: documents.Resume):
    """(id, text) for every citable line, as `documents.master_texts` and `structured_profile.line_texts` have them but
    without a later line of the same id replacing an earlier one."""
    for role in resume.roles:
        yield role.id, f"{role.title}, {role.employer}"
        for b in role.bullets:
            yield b.id, b.text
        for e in role.evidence:
            yield e.id, f"{e.title}: {e.text}"
    for x in (*resume.achievements, *resume.projects, *resume.awards, *resume.summary_variants):
        yield x.id, x.text
    for e in resume.education:
        yield e.id, f"{e.degree}, {e.school}"
    for c in resume.competencies:
        yield c.id, ": ".join(x for x in (c.area, ", ".join(c.items)) if x)


def visible_texts(resume: documents.Resume) -> dict[str, str]:
    """Every line a claim may cite, by id."""
    hidden = hidden_ids(resume)
    return {k: v for k, v in _all_texts(resume).items() if k not in hidden}


def claims_text(resume: documents.Resume) -> str:
    """The master as the claims job is handed it (no summary: it has no id to cite; no line marked never in writing)."""
    return numbered_text(resume, hidden_ids(resume), summary=False)


# --- the check of a report ---------------------------------------------------------------------------------------


def _plain(text: str) -> str:
    """Words only, lower case, one space between: how an example is looked for in the lines."""
    return " ".join(_WORDS.findall(text.replace("’", "'").lower()))


def _words(raw: object, where: str, what: str, most: int, problems: list[str]) -> str:
    if not isinstance(raw, str):
        problems.append(f"{where}: {what} must be text")
        return ""
    text = " ".join(raw.split()).strip(" ,;:-")
    if not text:
        problems.append(f"{where}: {what} is empty")
    elif len(text) > most:
        problems.append(f"{where}: {what} is {len(text)} characters; be brief (at most {most})")
        return ""
    elif _YEARS_WRITTEN.search(text):
        problems.append(f"{where}: {what} states a number of years; a claim states none")
        return ""
    elif _TEMPLATE_LIST.search(text):
        problems.append(f"{where}: {what} is an \"e.g.\" list; a generic list is not work done")
        return ""
    return text


def check(raw: object, resume: documents.Resume) -> tuple[list[dict], list[str]]:
    """A session's report tidied, and every problem with it: `{claim, examples, lines}` for each claim, repeats merged
    (their examples and lines joined), each tied to ids that are lines of this master and not marked never in writing,
    each example in the words of the lines cited. A claim that breaks a rule is left out and named in the problems, so
    a caller that refuses on any problem names them all."""
    if not isinstance(raw, dict) or not isinstance(raw.get("claims"), list):
        return [], ["fields must be an object: {claims: [{claim, examples, lines}]}"]
    problems: list[str] = []
    if extra := [str(k) for k in raw if k != "claims"]:
        problems.append(f"{', '.join(extra)} not a field; the field is claims")
    known = visible_texts(resume)
    items = raw["claims"]
    if len(items) > MOST_CLAIMS:
        problems.append(f"claims has {len(items)}; at most {MOST_CLAIMS}")
    merged: dict[str, dict] = {}
    for i, item in enumerate(items[:MOST_CLAIMS]):
        where = f"claims[{i}]"
        if not isinstance(item, dict):
            problems.append(f"{where}: must be an object")
            continue
        claim = _LEAD.sub("", _words(item.get("claim"), where, "the claim", MAX_CLAIM_CHARS, problems))
        examples_raw = item.get("examples") or []
        if not isinstance(examples_raw, list) or not all(isinstance(x, str) for x in examples_raw):
            problems.append(f"{where}: examples must be a list of short texts")
            continue
        if len(examples_raw) > MAX_EXAMPLES:
            problems.append(f"{where}: {len(examples_raw)} examples; at most {MAX_EXAMPLES}")
        examples = [t for x in examples_raw[:MAX_EXAMPLES]
                    if (t := _words(x, where, "an example", MAX_EXAMPLE_CHARS, problems))]
        lines_raw = item.get("lines")
        if not isinstance(lines_raw, list) or not lines_raw or not all(isinstance(x, str) for x in lines_raw):
            problems.append(f"{where}: no line id; every claim needs the ids of the master lines it rests on")
            continue
        if unknown := [x for x in lines_raw if x not in known]:
            verb = "is" if len(unknown) == 1 else "are"
            problems.append(f"{where}: {', '.join(unknown)} {verb} not a line of the master")
        lines = [x for x in dict.fromkeys(lines_raw) if x in known]
        cited = _plain(" ".join(known[x] for x in lines))
        for example in list(examples):
            if _plain(example) not in cited:
                problems.append(f"{where}: the example \"{example}\" is not in the words of the lines cited")
                examples.remove(example)
        if not (claim and lines):
            continue
        one = merged.setdefault(claim.lower(), {"claim": claim, "examples": [], "lines": []})
        have = {e.lower() for e in one["examples"]}
        one["examples"] = [*one["examples"], *(x for x in examples if x.lower() not in have)]
        one["lines"] += [x for x in lines if x not in one["lines"]]
    return list(merged.values()), problems


# --- rendering and the record ------------------------------------------------------------------------------------


def render_claim(claim: dict, *, lines: bool = False) -> str:
    """One claim as a line: "Experience with program management (example: a, b)"; `lines` appends the master line ids
    it rests on."""
    head = f"Experience with {claim['claim']}"
    if claim.get("examples"):
        head += f" (example: {', '.join(claim['examples'])})"
    return head + (f" [{', '.join(claim['lines'])}]" if lines else "")


def render(claims: list[dict], *, lines: bool = False) -> str:
    return "\n".join(f"- {render_claim(c, lines=lines)}" for c in claims)


def build(resume: documents.Resume, claims: list[dict]) -> Record:
    """The version's record in the shared shape: one field, the claims, quoted from the first line the first claim
    rests on, and refused if that quote is not in the master."""
    known = visible_texts(resume)
    record = Record({"claims": Field(claims, SONNET, known[claims[0]["lines"][0]])})
    if check_quotes(record, "\n".join(known.values())):
        raise ProfileError("claims: a quote is not in the master")
    return record


# --- storage and the job ----------------------------------------------------------------------------------------


async def stored(session: AsyncSession, owner: str, version: int) -> StructuredRecord | None:
    return await session.scalar(select(StructuredRecord).where(
        StructuredRecord.owner == owner, StructuredRecord.kind == KIND, StructuredRecord.role_id.is_(None),
        StructuredRecord.fingerprint == str(version)))


async def keep(session: AsyncSession, owner: str, version: int, record: Record) -> StructuredRecord:
    """Store the claims of a master version, replacing the ones it has (one record for each version)."""
    row = await stored(session, owner, version)
    if row is None:
        row = StructuredRecord(owner=owner, kind=KIND, role_id=None, fingerprint=str(version))
        session.add(row)
    row.fields, row.partial, row.rules_version = record.as_dict()["fields"], False, RULES_VERSION
    await session.flush()
    return row


async def ensure(session: AsyncSession, owner: str) -> ParseJob | None:
    """Make the one claims job the master in force still needs, as `structured_profile.ensure` does for its record:
    none when it has no versions or nothing citable, its claims are kept, a job for it is waiting or being read, or it
    has had `MOST_TRIES`. The job is a waiting one that a Sonnet session takes when one is free (no agent has to be
    there now). Called when a master version is saved and whenever the pool looks for work."""
    version = await profile_store.newest_version(session, owner, "resume")
    if not version or await stored(session, owner, version) is not None:
        return None
    resume = await version_resume(session, owner, version)
    if resume is None or not claims_text(resume).strip():
        return None
    ref = job_ref(version)
    if await session.scalar(select(ParseJob.id).where(
            ParseJob.owner == owner, ParseJob.kind == ParseKind.CLAIMS, ParseJob.role_id == ref,
            ParseJob.state.in_((ParseState.WAITING, ParseState.READING))).limit(1)):
        return None
    tries = await session.scalar(select(func.count()).select_from(ParseJob).where(
        ParseJob.owner == owner, ParseJob.kind == ParseKind.CLAIMS, ParseJob.role_id == ref))
    if (tries or 0) >= MOST_TRIES:
        return None
    job = ParseJob(owner=owner, kind=ParseKind.CLAIMS, role_id=ref, text=claims_text(resume),
                   state=ParseState.WAITING, created_at=datetime.now(UTC))
    session.add(job)
    await session.flush()
    return job


async def report(session: AsyncSession, owner: str, job: ParseJob, fields) -> tuple[int, list[dict]] | None:
    """Check and keep a session's claims for the job's master version: the version and its claims, an empty list when
    the report holds none (the caller fails the job), or None when the master is gone. Raises `ProblemsError` naming
    every problem when the report breaks a rule (the job stays open for another try)."""
    version = version_of(job.role_id)
    resume = None if version is None else await version_resume(session, owner, version)
    if version is None or resume is None:
        return None
    claims, problems = check(fields, resume)
    if problems:
        raise ProblemsError("; ".join(problems))
    if not claims:
        return version, []
    await keep(session, owner, version, build(resume, claims))
    return version, claims


# --- who reads them ----------------------------------------------------------------------------------------------


async def newest(session: AsyncSession, owner: str) -> tuple[int, list[dict]] | None:
    """The claims of the newest master version that has them, with that version; None when none are made yet."""
    row = await session.scalar(select(StructuredRecord).where(
        StructuredRecord.owner == owner, StructuredRecord.kind == KIND, StructuredRecord.role_id.is_(None)
    ).order_by(cast(StructuredRecord.fingerprint, Integer).desc()).limit(1))
    if row is None or "claims" not in (row.fields or {}):
        return None
    return int(row.fingerprint), list(row.fields["claims"]["value"])


async def short_profile_text(session: AsyncSession, owner: str) -> str:
    """The claims for the Haiku ranker's short profile: one line each, no ids (they mean nothing to a ranker); empty
    when none are made yet."""
    made = await newest(session, owner)
    return render(made[1]) if made else ""


@dataclass
class MasterInput:
    """What the Second Impression is given in place of the master text: the claims with their line ids, the full text
    of every line they cite, and the resume itself, the whole master still to hand."""

    version: int
    claims: list[dict]
    text: str
    cited: dict[str, str] = field(default_factory=dict)
    resume: documents.Resume | None = None


async def second_impression_input(session: AsyncSession, owner: str) -> MasterInput | None:
    """The Second Impression's master input, or None when no claims are made yet (it reads the master as before)."""
    made = await newest(session, owner)
    if made is None:
        return None
    version, claims = made
    resume = await version_resume(session, owner, version)
    if resume is None:
        return None
    known = visible_texts(resume)
    cited = {x: known[x] for c in claims for x in c["lines"] if x in known}
    return MasterInput(version, claims, render(claims, lines=True), cited, resume)
