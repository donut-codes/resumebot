"""The master's structured record: when the owner saves a new master
resume, one small Haiku session of the `parser` kind (the profile mode of the job `jobs/parse.py` keeps) reads it once
and names what it shows, and code does the rest. The master itself is never changed, and nothing shows the record on
screen yet (the owner, 2026-10-20).

What the model names, each tied to the master line ids that show it, never to a figure:
- `entries`: areas of work, skills, tools, methods and domains, each `{category, name, lines}`;
- `certifications`: `{name, lines}`;
- `highlights`: master lines worth carrying into a comparison, each `{line, terms}` with the names it shows.

What code fills: the titles held, the roles' dates, years (total, and for each area, skill, tool, method or domain,
worked out from the dates of the roles the cited lines sit in, months counted once however many roles overlap), and
education (the master's own entries). A name with no line id, or one that is not a line of this master, is refused,
every problem named at once so the session can send it again; a quote that is not in the master is no fact
(`structured_record.check_quotes`). A date code cannot read leaves a role with no months, never a guess.

Nothing here names a person, an employer or a skill: every name is the owner's own data, read from their master.
"""

import re
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.models import ParseJob, ProfileVersion, StructuredRecord
from app.jobsearch.postings.record import CODE, HAIKU, Field, Record, RecordError, check_quotes
from app.jobsearch.profile import documents
from app.jobsearch.profile import store as profile_store
from app.jobsearch.states import ParseKind, ParseState

#: What a named entry can be. `areas` are areas of work; the rest are the stack.
AREAS = "areas"
STACK_CATEGORIES = ("skills", "tools", "methods", "domains")
CATEGORIES = (AREAS, *STACK_CATEGORIES)
#: A name is a term, not a sentence.
MAX_NAME_CHARS = 80
#: The most each list holds in one answer.
MOST_ENTRIES, MOST_CERTIFICATIONS, MOST_HIGHLIGHTS = 400, 40, 80
RULES_VERSION = 1

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
_NUMERIC = re.compile(r"\b(\d{1,2})\s*[/.-]\s*(19\d{2}|20\d{2})\b|\b(19\d{2}|20\d{2})\s*[/.-]\s*(\d{1,2})\b")
_OPEN_END = re.compile(r"\b(present|current|now|today|ongoing|to date)\b", re.IGNORECASE)

HOW = (
    "This is the owner's master resume, one line per id: `[id] text`. Name what it shows, each tied to the ids of the "
    "lines that show it, and send it with jobs.parse_report: {id, fields} in the shape sent with it. `entries` is a "
    "list of {category, name, lines}: category is one of areas (an area of work), skills, tools, methods or domains, "
    "name is the term as the resume words it (a term, not a sentence), and lines are the ids that show it. "
    "`certifications` is a list of {name, lines} for each certification or licence the resume states. `highlights` is "
    "a list of {line, terms}: a line worth carrying into a comparison with a posting, and the names it shows. Use only "
    "ids that are in the text and never state years: code works them out from the dates. A name with no line id is "
    "refused. Leave a list empty when the resume has nothing for it; never invent. Then call jobs.parse_next again."
)


class ProfileError(RecordError):
    """A report that breaks the rules: every problem named at once."""


# --- the master as the session reads it ---------------------------------------------------------------------------


def line_texts(resume: documents.Resume) -> dict[str, str]:
    """Every line of the master a name can cite, by id: the lines `documents.master_texts` knows, and each competency
    group as its area and terms."""
    texts = documents.master_texts(resume)
    texts |= {c.id: ": ".join(x for x in (c.area, ", ".join(c.items)) if x) for c in resume.competencies}
    return texts


def numbered_text(resume: documents.Resume, hidden: frozenset[str] = frozenset(), *, summary: bool = True) -> str:
    """The master as the session is handed it: a line for each id, in the master's order, each role followed by the
    lines under it. A role's line carries its dates and tools, so the session can see where it sat. `hidden` ids are
    left out (a role hidden takes its lines with it); `summary=False` leaves out the summary, which has no id."""
    out: list[str] = []
    if summary and resume.summary.strip():
        out.append(f"summary: {resume.summary.strip()}")
    texts = line_texts(resume)
    shown = lambda ident: ident not in hidden  # noqa: E731
    out += [f"[{x.id}] {x.text}" for x in resume.summary_variants if shown(x.id)]
    out += [f"[{c.id}] {texts[c.id]}" for c in resume.competencies if shown(c.id)]
    out += [f"[{x.id}] {x.text}" for x in resume.achievements if shown(x.id)]
    for role in resume.roles:
        if not shown(role.id):
            continue
        dates = " - ".join(x for x in (role.start, role.end) if x)
        tools = f" (tools: {', '.join(role.tools)})" if role.tools else ""
        out.append(f"[{role.id}] {role.title}, {role.employer}" + (f", {dates}" if dates else "") + tools)
        out += [f"[{b.id}] {b.text}" for b in role.bullets if shown(b.id)]
        out += [f"[{e.id}] {e.title}: {e.text}" for e in role.evidence if shown(e.id)]
    out += [f"[{e.id}] {e.degree}, {e.school}" for e in resume.education if shown(e.id)]
    out += [f"[{x.id}] {x.text}" for x in [*resume.projects, *resume.awards] if shown(x.id)]
    return "\n".join(out)


# --- years, in code, from the dates ---------------------------------------------------------------------------------


def _month_index(text: str, *, end: bool, today: date) -> int | None:
    """A month as a count (year * 12 + month - 1) from how a resume writes one: "Jan 2019", "January 2019", "2019-03",
    "03/2019" or a bare year (the first month of it for a start, the last for an end). None when no year is there."""
    text = (text or "").strip()
    if not text:
        return None
    if end and _OPEN_END.search(text):
        return today.year * 12 + today.month - 1
    if m := _NUMERIC.search(text):
        month, year = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(4)), int(m.group(3)))
        if 1 <= month <= 12:
            return year * 12 + month - 1
    year = _YEAR.search(text)
    if not year:
        return None
    named = next((i for i, name in enumerate(_MONTHS) if re.search(rf"\b{name}[a-z]*\b", text, re.IGNORECASE)), None)
    month = named if named is not None else (11 if end else 0)
    return int(year.group(1)) * 12 + month


def role_months(role: documents.Role, today: date) -> frozenset[int]:
    """The months a role covers, both ends included. Empty when its start cannot be read; an end that is blank or says
    present runs to today, and one that cannot be read leaves the role with no months."""
    start = _month_index(role.start, end=False, today=today)
    end = _month_index(role.end, end=True, today=today) if role.end.strip() else (
        today.year * 12 + today.month - 1 if start is not None else None)
    if start is None or end is None or end < start:
        return frozenset()
    return frozenset(range(start, end + 1))


def years(months: frozenset[int] | set[int]) -> float | None:
    """Months counted once each, as years to a tenth; None when there are none."""
    return round(len(months) / 12, 1) if months else None


def _role_of_line(resume: documents.Resume) -> dict[str, str]:
    """The role each line sits in: its bullets, its evidence entries and the role's own line."""
    out: dict[str, str] = {}
    for role in resume.roles:
        out[role.id] = role.id
        out |= {b.id: role.id for b in role.bullets}
        out |= {e.id: role.id for e in role.evidence}
    return out


def years_of_lines(resume: documents.Resume, line_ids: list[str], today: date) -> float | None:
    """Years across the roles the cited lines sit in, counted once; lines outside any role (an achievement, a
    certification entry) add none."""
    by_role = {r.id: role_months(r, today) for r in resume.roles}
    of_line = _role_of_line(resume)
    months: set[int] = set()
    for line_id in line_ids:
        months |= by_role.get(of_line.get(line_id, ""), frozenset())
    return years(months)


# --- the check of a report -----------------------------------------------------------------------------


def _name(raw: object, where: str, problems: list[str]) -> str:
    if not isinstance(raw, str):
        problems.append(f"{where}: the name must be text")
        return ""
    name = " ".join(raw.split()).strip(" ,;:-")
    if not name:
        problems.append(f"{where}: the name is empty")
    elif len(name) > MAX_NAME_CHARS:
        problems.append(f"{where}: a name is a term, not a sentence ({len(name)} characters; at most {MAX_NAME_CHARS})")
        return ""
    return name


def _lines(raw: object, where: str, known: dict[str, str], problems: list[str]) -> list[str]:
    """The ids a name cites: at least one, each a line of this master."""
    if not isinstance(raw, list) or not raw or not all(isinstance(x, str) for x in raw):
        problems.append(f"{where}: no line id; every name needs the ids of the master lines that show it")
        return []
    unknown = [x for x in raw if x not in known]
    if unknown:
        verb = "is" if len(unknown) == 1 else "are"
        problems.append(f"{where}: {', '.join(unknown)} {verb} not a line of the master")
    return [x for x in dict.fromkeys(raw) if x in known]


def _listed(raw: object, key: str, most: int, problems: list[str]) -> list:
    if raw in (None, []):
        return []
    if not isinstance(raw, list) or not all(isinstance(x, dict) for x in raw):
        problems.append(f"{key} must be a list of objects")
        return []
    if len(raw) > most:
        problems.append(f"{key} has {len(raw)}; at most {most}")
    return raw


def check(raw: object, resume: documents.Resume) -> tuple[dict, list[str]]:
    """A session's report tidied, and every problem with it: names trimmed, repeats merged (their lines joined), each
    name tied to ids that are lines of this master. A name that breaks a rule is left out of the tidy result and named
    in the problems, so a caller that refuses on any problem names them all."""
    if not isinstance(raw, dict):
        return {"entries": [], "certifications": [], "highlights": []}, [
            "fields must be an object: {entries, certifications, highlights}"]
    problems: list[str] = []
    known = line_texts(resume)
    unknown_keys = [str(k) for k in raw if k not in ("entries", "certifications", "highlights")]
    if unknown_keys:
        problems.append(f"{', '.join(unknown_keys)} not a field; the fields are entries, certifications, highlights")

    entries: dict[tuple[str, str], list[str]] = {}
    for i, item in enumerate(_listed(raw.get("entries"), "entries", MOST_ENTRIES, problems)):
        where = f"entries[{i}]"
        category = item.get("category")
        if category not in CATEGORIES:
            problems.append(f"{where}: category must be one of {', '.join(CATEGORIES)}")
            continue
        name = _name(item.get("name"), where, problems)
        ids = _lines(item.get("lines"), where, known, problems)
        if name and ids:
            merged = entries.setdefault((category, name.lower()), [name])
            merged += [x for x in ids if x not in merged[1:]]

    certs: dict[str, list[str]] = {}
    for i, item in enumerate(_listed(raw.get("certifications"), "certifications", MOST_CERTIFICATIONS, problems)):
        where = f"certifications[{i}]"
        name = _name(item.get("name"), where, problems)
        ids = _lines(item.get("lines"), where, known, problems)
        if name and ids:
            merged = certs.setdefault(name.lower(), [name])
            merged += [x for x in ids if x not in merged[1:]]

    highlights: dict[str, list[str]] = {}
    for i, item in enumerate(_listed(raw.get("highlights"), "highlights", MOST_HIGHLIGHTS, problems)):
        where = f"highlights[{i}]"
        line = item.get("line")
        if not isinstance(line, str) or line not in known:
            problems.append(f"{where}: no line id; a highlight is a line of the master")
            continue
        terms = item.get("terms")
        if not isinstance(terms, list) or not all(isinstance(t, str) for t in terms):
            problems.append(f"{where}: terms must be a list of names")
            continue
        kept = highlights.setdefault(line, [])
        for term in (" ".join(t.split()) for t in terms):
            if term and term.lower() not in {k.lower() for k in kept}:
                kept.append(term)

    clean = {
        "entries": [{"category": c, "name": v[0], "lines": v[1:]} for (c, _), v in entries.items()],
        "certifications": [{"name": v[0], "lines": v[1:]} for v in certs.values()],
        "highlights": [{"line": line, "terms": terms} for line, terms in highlights.items()],
    }
    return clean, problems


def is_empty(clean: dict) -> bool:
    return not (clean["entries"] or clean["certifications"] or clean["highlights"])


# --- the record -----------------------------------------------------------------------------------------


def build(resume: documents.Resume, clean: dict, today: date) -> Record:
    """The master's record in the shared shape (`structured_record`): what the session named, with years worked out
    here from the dates, and the titles, total years and education code reads straight from the master. Each filled
    field is quoted from a line it rests on, and a record with a quote that is not in the master is refused."""
    known = line_texts(resume)
    fields: dict[str, Field] = {}

    def put(name: str, value: object, source: str, line_id: str) -> None:
        fields[name] = Field(value, source, known[line_id])

    if resume.roles:
        put("title", [r.title for r in resume.roles if r.title.strip()], CODE, resume.roles[0].id)
        total = years(frozenset().union(*(role_months(r, today) for r in resume.roles)))
        if total is not None:
            put("years", {"total": total, "roles": [
                {"line": r.id, "months": len(m)} for r in resume.roles if (m := role_months(r, today))]},
                CODE, resume.roles[0].id)
    if resume.education:
        put("education", [{"line": e.id, "degree": e.degree, "school": e.school} for e in resume.education],
            CODE, resume.education[0].id)

    def entry(item: dict) -> dict:
        out = {"name": item["name"], "lines": item["lines"]}
        if (y := years_of_lines(resume, item["lines"], today)) is not None:
            out["years"] = y
        return out

    areas = [entry(e) for e in clean["entries"] if e["category"] == AREAS]
    stack = [{**entry(e), "category": e["category"]} for e in clean["entries"] if e["category"] != AREAS]
    if areas:
        put("areas", areas, HAIKU, areas[0]["lines"][0])
    if stack:
        put("stack", stack, HAIKU, stack[0]["lines"][0])
    if clean["certifications"]:
        certs = clean["certifications"]
        put("certifications", certs, HAIKU, certs[0]["lines"][0])
    if clean["highlights"]:
        put("duties", clean["highlights"], HAIKU, clean["highlights"][0]["line"])

    record = Record(fields)
    if bad := check_quotes(record, "\n".join(known.values())):
        raise ProfileError(f"{', '.join(bad)}: a quote is not in the master")
    return record


# --- storage and the job -----------------------------------------------------------------------------


def job_ref(version: int) -> str:
    """What a profile job's `role_id` holds: the master version it was made from (a profile job belongs to no tab)."""
    return f"master:{version}"


def version_of(job_ref_text: str | None) -> int | None:
    head, _, tail = (job_ref_text or "").partition(":")
    return int(tail) if head == "master" and tail.isdigit() else None


async def stored(session: AsyncSession, owner: str, version: int) -> StructuredRecord | None:
    """The record kept for a master version: at most one."""
    return await session.scalar(select(StructuredRecord).where(
        StructuredRecord.owner == owner, StructuredRecord.kind == "profile", StructuredRecord.role_id.is_(None),
        StructuredRecord.fingerprint == str(version)))


async def keep(session: AsyncSession, owner: str, version: int, record: Record) -> StructuredRecord:
    """Store the record of a master version, replacing the one it already has (one record for each version)."""
    row = await stored(session, owner, version)
    if row is None:
        row = StructuredRecord(owner=owner, kind="profile", role_id=None, fingerprint=str(version))
        session.add(row)
    row.fields, row.partial, row.rules_version = record.as_dict()["fields"], False, RULES_VERSION
    await session.flush()
    return row


async def version_resume(session: AsyncSession, owner: str, version: int) -> documents.Resume | None:
    """The master resume as saved at `version`."""
    row = await session.scalar(select(ProfileVersion).where(
        ProfileVersion.owner == owner, ProfileVersion.kind == "resume", ProfileVersion.version == version))
    return documents.Resume.model_validate(row.data) if row else None


#: How many jobs one master version may have in a day: a lost or failed one is made again, never without end.
MOST_TRIES = 3


async def ensure(session: AsyncSession, owner: str) -> object | None:
    """Make the one profile job the master in force still needs: none when it has no versions, its record is kept, a
    job for it is already waiting or being read, or it has had `MOST_TRIES` already. The job is a waiting one that the
    pool picks up whenever a session is free: no agent has to be there now, and it does not expire untaken. Returns the
    job, or None. Called when a master version is saved and whenever the pool looks for work, so a restart or a lost
    job never leaves a version without its record."""

    version = await profile_store.newest_version(session, owner, "resume")
    if not version or await stored(session, owner, version) is not None:
        return None
    resume = await version_resume(session, owner, version)
    if resume is None or not numbered_text(resume).strip():
        return None
    ref = job_ref(version)
    if await session.scalar(select(ParseJob.id).where(
            ParseJob.owner == owner, ParseJob.kind == ParseKind.PROFILE, ParseJob.role_id == ref,
            ParseJob.state.in_((ParseState.WAITING, ParseState.READING))).limit(1)):
        return None
    tries = await session.scalar(select(func.count()).select_from(ParseJob).where(
        ParseJob.owner == owner, ParseJob.kind == ParseKind.PROFILE, ParseJob.role_id == ref))
    if (tries or 0) >= MOST_TRIES:
        return None
    job = ParseJob(owner=owner, kind=ParseKind.PROFILE, role_id=ref, text=numbered_text(resume),
                   state=ParseState.WAITING, created_at=datetime.now(UTC))
    session.add(job)
    await session.flush()
    return job
