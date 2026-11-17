"""The story library (PRD path 43): what every packet is put together from.

The library holds, for one owner's master resume:

- **resume lines**: a short, rule-clean resume bullet for each master entry worth printing (a bullet, an achievement,
  an evidence entry), a second one only where it serves two kinds of role, and a scope line for each role, each citing
  the master lines it rests on. Each names its claim (the accomplishment it tells) and the skills it shows. A packet
  chooses and orders these; it never rewrites them, never prints two lines of one claim (`claims`), and lists only the
  skills of the lines it prints as competencies.
- **letter blocks**: one polished cover-letter paragraph for each of the candidate's stories (a second angle where a
  story serves two kinds of role), each sentence citing its master lines. A letter is two or three of them with new
  joins written for the job.
- **rules read from the owner's settings**: the names the owner's client rule keeps out of writing, the wordings the
  owner's rules approve, and for each role the bullet count the owner's rules set, whether a scope line always prints,
  and whether the work owned inside the employer comes before client work. A model reads them from the owner's own
  words when the library is built; code enforces them on every line of every packet.

It is built by an agent job (`playbooks/jobsearch/build_library.bpmn`) when the master resume or the writing rules
change, its two parts written side by side (`build_library_part.bpmn`: the resume lines with the owner's rules,
and the letter blocks): only the entries whose master lines changed are written again (and the stories that
cite them), and when the rules changed every line is checked again under them. Lines are checked when they are built
(code checks, then a facts read by meaning), fixed once, and a line still failing is held out, so a packet never
needs a rule repair. Each build is a version (`StoryLibrary`); the owner sees the newest on My Resume, read only.

No one's data lives here: every figure the checks enforce is the owner's setting or the library's own standard for a
short line.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from process_engine.models import RUNNING, WAITING, ProcInstance
from process_engine.registry import actions as registry
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import process
from app.jobsearch.models import StoryLibrary
from app.jobsearch.profile import integrity, never_write, style_rules
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.documents import Documents, Resume, rules_for
from app.jobsearch.work import engine_calls
from app.jobsearch.writing import claim_scope, seats
from app.trail import service as trail

BUILDING, READY, FAILED = "building", "ready", "failed"
#: A build older than this that never finished is closed as failed, so a new one can start.
STALE_BUILD = timedelta(hours=6)
#: The library's own standard for a short line: most words of a bullet, an achievement (one printed line) and a scope
#: line, and the fewest a bullet may have unless it is a wording the owner approved.
BULLET_WORDS, ACHIEVEMENT_WORDS, SCOPE_WORDS, FEWEST_WORDS = 30, 18, 35, 10
SCOPE, BULLET, ACHIEVEMENT = "scope", "bullet", "achievement"
#: The process's agent steps (`build_library.bpmn`), and those of the two parts it writes side by side
#: (`build_library_part.bpmn`).
CHECK_1, CHECK_2, FIX_STEP = "Check_1", "Check_2", "Fix"
LINES_STEP, LETTERS_STEP = "Write_lines", "Write_letters"
#: The kinds of part (`plan_parts`). The resume lines are one turn: split by role, every part repeated the
#: instructions, the owner's rules and the master, and a build cost half again as much (tried 2026-10-24).
LINES_PART, LETTERS_PART = "lines", "letters"

_PAST_IRREGULAR = frozenset("""led built ran wrote drew took made set grew won drove brought taught began held kept
found gave sold cut put met chose rebuilt rewrote oversaw undertook became got spent sought sent lent spoke stood upheld
wound laid paid read shaped rolled split shut bought caught fought thought understood withdrew forecast broadcast
overcame outgrew""".split())
_FIRST_PERSON = re.compile(r"\bI\b|\b(?:me|my|mine|we|our|ours|us)\b")


# --- the master's entries -----------------------------------------------------------------------------------------


def master_lines(resume: Resume) -> dict[str, str]:
    """The text of every master line a library line may cite, by id."""
    out = {b.id: b.text for r in resume.roles for b in r.bullets}
    out |= {x.id: x.text for x in [*resume.achievements, *resume.projects, *resume.awards, *resume.summary_variants]}
    out |= {e.id: f"{e.title}: {e.text}" for r in resume.roles for e in r.evidence}
    return out


def notes_of(resume: Resume) -> dict[str, str]:
    """The owner's note on each master line and role that has one: the limit on how it may be used."""
    out = {b.id: b.note for r in resume.roles for b in r.bullets if b.note}
    out |= {x.id: x.note for x in [*resume.achievements, *resume.projects, *resume.awards, *resume.summary_variants]
            if x.note}
    out |= {r.id: r.note for r in resume.roles if r.note}
    return out


def entries(resume: Resume) -> list[dict]:
    """Every master entry the library decides on: achievements, each role's bullets, and each evidence entry once (one
    filed under several roles lists them all)."""
    out: list[dict] = [{"entry": a.id, "kind": ACHIEVEMENT} for a in resume.achievements]
    seen: dict[str, dict] = {}
    for role in resume.roles:
        out += [{"entry": b.id, "kind": BULLET, "role": role.id} for b in role.bullets]
        for e in role.evidence:
            if e.id in seen:
                seen[e.id]["filed_under"].append(role.id)
                continue
            seen[e.id] = {"entry": e.id, "kind": "evidence", "title": e.title, "filed_under": [role.id]}
            out.append(seen[e.id])
    return out


def _digest(value: object) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def sources(resume: Resume) -> dict[str, str]:
    """A digest of each entry's words and note, and of each role's printed facts, by id: what tells a later build
    which entries changed."""
    out = {b.id: _digest([b.text, b.note]) for r in resume.roles for b in r.bullets}
    out |= {a.id: _digest([a.text, a.note]) for a in resume.achievements}
    out |= {e.id: _digest([e.title, e.text]) for r in resume.roles for e in r.evidence}
    out |= {f"role:{r.id}": _digest([r.title, r.employer, r.descriptor, r.intro, r.note, r.start, r.end,
                                     [b.id for b in r.bullets], [e.id for e in r.evidence]]) for r in resume.roles}
    return out


def master_view(docs: Documents) -> dict:
    """The master as a library seat reads it: every part with its ids and the owner's notes."""
    r = docs.resume.model_dump(mode="json")
    return {k: r[k] for k in ("summary", "achievements", "competencies", "roles", "education", "projects", "awards",
                              "philosophy", "summary_variants", "notes")}


def lines_with_notes(docs: Documents, ids) -> dict:
    """The master lines these ids name, each with the owner's note when it has one."""
    texts, notes = master_lines(docs.resume), notes_of(docs.resume)
    roles = {r.id: r for r in docs.resume.roles}
    out: dict[str, Any] = {}
    for i in sorted({x for x in ids if x}):
        if i in texts:
            out[i] = {"text": texts[i], **({"note": notes[i]} if i in notes else {})}
        elif i in roles:
            out[i] = {"role": f"{roles[i].title}, {roles[i].employer}", **({"note": notes[i]} if i in notes else {})}
    return out


def roles_and_education(docs: Documents) -> dict:
    """What a resume prints from the master itself: each role's employer, title, dates and descriptor, and the
    education. A facts read is given these, so a headline or summary that rests on them is not failed as uncited."""
    return {"roles": [{"role": r.id, "employer": r.employer, "title": r.title, "dates": f"{r.start} to {r.end}",
                       **({"descriptor": r.descriptor} if r.descriptor else {}),
                       **({"scope": r.intro} if r.intro else {})}
                      for r in docs.resume.roles],
            "education": [f"{e.degree}, {e.school}".strip(", ") for e in docs.resume.education]}


# --- the stored library -------------------------------------------------------------------------------------------


async def newest(session: AsyncSession, owner: str, status: str | None = READY) -> StoryLibrary | None:
    query = select(StoryLibrary).where(StoryLibrary.owner == owner)
    if status:
        query = query.where(StoryLibrary.status == status)
    return await session.scalar(query.order_by(StoryLibrary.version.desc()).limit(1))


async def _versions(session: AsyncSession, owner: str) -> tuple[int, int]:
    return (await profile_store.newest_version(session, owner, "resume"),
            await profile_store.newest_version(session, owner, "writing"))


async def building(session: AsyncSession, owner: str, now: datetime | None = None) -> StoryLibrary | None:
    """The build going now, if any (one older than `STALE_BUILD` is not)."""
    moment = now or datetime.now(UTC)
    row = await newest(session, owner, BUILDING)
    return row if row is not None and row.created_at >= moment - STALE_BUILD else None


async def is_current(session: AsyncSession, owner: str) -> bool:
    row = await newest(session, owner)
    return row is not None and (row.resume_version, row.writing_version) == await _versions(session, owner)


async def usable(session: AsyncSession, owner: str) -> StoryLibrary | None:
    """The library a packet is put together from: the newest ready version, unless a build is going for newer master
    or rules (a packet then waits for it)."""
    row = await newest(session, owner)
    if row is None:
        return None
    if (row.resume_version, row.writing_version) == await _versions(session, owner):
        return row
    return None if await building(session, owner) is not None else row


async def ensure_current(session: AsyncSession, owner: str, *, now: datetime | None = None) -> StoryLibrary | None:
    """Start a build when the newest library is not from the master and writing rules in force and none is going.
    Returns the build started, or None. A build that never finished is closed as failed first."""
    docs = await profile_store.owner_documents(session, owner)
    if not docs.resume.roles or await is_current(session, owner):
        return None
    moment = now or datetime.now(UTC)
    if await building(session, owner, moment) is not None:
        return None
    for stale in (await session.scalars(select(StoryLibrary).where(
            StoryLibrary.owner == owner, StoryLibrary.status == BUILDING))).all():
        stale.status, stale.done_at = FAILED, moment
    resume_version, writing_version = await _versions(session, owner)
    version = (await session.scalar(select(func.max(StoryLibrary.version)).where(StoryLibrary.owner == owner)) or 0) + 1
    row = StoryLibrary(owner=owner, version=version, status=BUILDING, resume_version=resume_version,
                       writing_version=writing_version, data={})
    async with session.begin_nested():
        session.add(row)
        await session.flush()
        instance = await process.engine().start(session, process.LIBRARY, {"owner": owner, "library": str(row.id)},
                                                subject=f"jobsearch:library:{row.id}")
        row.instance_id = instance.id
    await session.flush()
    return row


async def started(row: StoryLibrary | None) -> None:
    """Wake a session for a build just started (after the commit)."""
    if row is not None:
        await engine_calls.knock(str(row.id), reason=engine_calls.PACKET_ASKED)


def view(row: StoryLibrary | None, docs: Documents, *, current: bool, going: bool) -> dict:
    """The library as the owner reads it on My Resume: each role's scope line and lines, the achievements and the
    stories with their blocks, and what was held out."""
    if row is None:
        return {"version": None, "current": False, "building": going, "roles": [], "achievements": [], "stories": []}
    data = row.data or {}
    lines = data.get("lines") or []

    def shown(ln: dict) -> dict:
        return {"id": ln["id"], "text": ln["text"], "from": ln.get("from") or [], "held": bool(ln.get("held")),
                **({"why_held": ln["why_held"]} if ln.get("why_held") else {})}

    roles = []
    for r in docs.resume.roles:
        mine = [ln for ln in lines if ln.get("role") == r.id]
        scope = next((ln for ln in mine if ln["kind"] == SCOPE), None)
        roles.append({"role": r.id, "title": r.title, "employer": r.employer,
                      "scope": shown(scope) if scope else None,
                      "lines": [shown(ln) for ln in mine if ln["kind"] == BULLET]})
    return {
        "version": row.version, "built_at": row.done_at.isoformat() if row.done_at else None,
        "current": current, "building": going, "roles": roles,
        "achievements": [shown(ln) for ln in lines if ln["kind"] == ACHIEVEMENT],
        "stories": [{"id": s["id"], "title": s.get("title", ""), "index": s.get("index", ""),
                     "blocks": [{"id": b["id"], "for": b.get("for", ""), "text": block_text(b),
                                 "from": block_ids(b), "held": bool(b.get("held"))}
                                for b in s.get("blocks") or []]}
                    for s in data.get("stories") or []],
    }


def block_text(block: dict) -> str:
    return " ".join(str(s.get("text") or "").strip() for s in block.get("sentences") or [])


def block_ids(block: dict) -> list[str]:
    return sorted({i for s in block.get("sentences") or [] for i in s.get("from") or []})


def usable_lines(data: dict) -> list[dict]:
    return [ln for ln in data.get("lines") or [] if not ln.get("held")]


def claims(lines: list[dict]) -> dict[str, str]:
    """The claim each resume line tells, by line id: lines that share one never print together. Lines share a claim
    when the writer gave them the same one, when they were written for the same master entry, or when one rests on
    the entry another was written for (it tells part of that line's story). Scope lines have none."""
    told = [ln for ln in lines if ln.get("kind") != SCOPE]
    parent = {ln["id"]: ln["id"] for ln in told}

    def root(i: str) -> str:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(a: str, b: str) -> None:
        parent[root(a)] = root(b)

    by_key: dict[str, str] = {}
    by_entry: dict[str, str] = {}
    for ln in told:
        key = str(ln.get("claim") or ln.get("work") or "")
        for seen, value in ((by_key, key), (by_entry, str(ln.get("entry") or ""))):
            if value:
                if value in seen:
                    join(ln["id"], seen[value])
                else:
                    seen[value] = ln["id"]
    for ln in told:
        for i in ln.get("from") or []:
            if i != ln.get("entry") and i in by_entry:
                join(ln["id"], by_entry[i])
    return {ln["id"]: root(ln["id"]) for ln in told}


def usable_blocks(data: dict) -> list[dict]:
    return [{**b, "story": s["id"], "story_title": s.get("title", "")}
            for s in data.get("stories") or [] for b in s.get("blocks") or [] if not b.get("held")]


# --- the owner's rules, as the library read them ------------------------------------------------------------------


def role_rules(data: dict, role_id: str) -> dict:
    """What the owner's rules say about one role: `min` and `max` bullets (None: no count set), `firm_first` (work
    owned inside the employer before client work) and `scope` (its scope line always prints)."""
    found = ((data.get("rules") or {}).get("roles") or {}).get(role_id) or {}
    return {"min": found.get("min"), "max": found.get("max"), "firm_first": bool(found.get("firm_first")),
            "scope": bool(found.get("scope"))}


def protected(data: dict) -> list[str]:
    """The names the owner's client rule keeps out of writing, longest first."""
    names = {str(n).strip() for n in (data.get("rules") or {}).get("protected") or [] if str(n).strip()}
    return sorted(names, key=len, reverse=True)


def approved(data: dict) -> set[str]:
    return {_bare(str(w)) for w in (data.get("rules") or {}).get("approved") or [] if str(w).strip()}


def _bare(text: str) -> str:
    return " ".join(text.split()).strip().rstrip(".").lower()


def protected_hits(names: list[str], text: str) -> list[str]:
    """The protected names a text holds, as whole words, case as written."""
    return [n for n in names if re.search(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])", text)]


_HEDGES = r"(?:about|approximately|roughly|around|nearly|almost|an estimated|estimated)"
_FIGURE = (r"(?:\$?\d[\d,.]*%?[kKmMbB]?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|"
           r"twenty|thirty|forty|fifty|hundred|half)")
_BARE = re.compile(rf"(?<![\w$-])({_FIGURE})(?![\w-])", re.I)


def _hedge_before(text: str, at: int) -> bool:
    return bool(re.search(rf"\b{_HEDGES}\s+(?:a\s+)?\S*$", text[:at], re.I))


def dropped_hedges(text: str, source: str) -> list[str]:
    """The figures the master hedges ("about two months") that this text gives bare. A figure the master hedges every
    time it writes it keeps a hedge wherever a line carries it: without one the line claims a measurement the master
    never made. (A figure the master also writes bare somewhere is not counted: its bare use may be that one.)"""
    hedged, bare = set(), set()
    for m in _BARE.finditer(source):
        (hedged if _hedge_before(source, m.start()) else bare).add(_figure_key(source, m))
    always = hedged - bare
    out = []
    for m in _BARE.finditer(text):
        key = _figure_key(text, m)
        if key in always and not _hedge_before(text, m.start()) and " ".join(key).strip() not in out:
            out.append(" ".join(key).strip())
    return out


def _figure_key(text: str, m: re.Match) -> tuple[str, str]:
    """A figure and the word after it ("six months"), so a hedged "about two months" is not matched by "two
    analysts"."""
    after = re.match(r"\s+([A-Za-z]+)", text[m.end():])
    return m.group(1).lower(), (after.group(1).lower() if after else "")


def i_runs(sentences: list[str]) -> tuple[int, int]:
    """How many sentences open with "I", and the longest run of them in a row."""
    run = most = count = 0
    for s in sentences:
        if re.match(r"^\W*I\b", s.strip()):
            count, run = count + 1, run + 1
            most = max(most, run)
        else:
            run = 0
    return count, most


def opens_past(text: str) -> bool:
    word = (re.findall(r"[A-Za-z]+", text) or [""])[0].lower()
    return word.endswith("ed") or word in _PAST_IRREGULAR


# --- code checks ----------------------------------------------------------------------------------------------------


def _text_checks(docs: Documents, texts: list[tuple[str, str]]) -> list[str]:
    """The app's own text checks on these (where, text) pairs: the owner's banned phrases and the built-in kinds,
    certifications the master lacks, unshipped work written as delivered, and the style rules that are on."""
    writing = docs.writing
    out = never_write.find_hits(writing, texts)
    out += integrity.certification_hits(writing, docs.resume, texts)
    out += integrity.unshipped_hits(texts)
    out += [f"{m.rule}: {m.where}: {m.snippet!r}" for m in style_rules.find_matches(writing, texts, set())]
    return out


def check_line(line: dict, docs: Documents, data: dict) -> tuple[list[str], list[str]]:
    """Code checks for one library line: (what fails it, flags for the facts read)."""
    ident, text, kind = line["id"], " ".join(str(line.get("text") or "").split()), line.get("kind")
    ids = master_lines(docs.resume)
    roles = {r.id: r for r in docs.resume.roles}
    hard = [f"cites {i!r}, which is not a master line" for i in line.get("from") or []
            if i not in ids and i not in roles]  # a scope line may rest on its role itself
    if not line.get("from"):
        hard.append("cites no master line")
    if not text:
        return [*hard, "has no words"], []
    hard += [f"names {n!r}, which the owner's client rule keeps out of writing"
             for n in protected_hits(protected(data), text)]
    if _FIRST_PERSON.search(text):
        hard.append("is in the first person")
    words = len(text.split())
    if kind == SCOPE:
        role = roles.get(line.get("role") or "")
        if words > SCOPE_WORDS:
            hard.append(f"is {words} words; a scope line is at most {SCOPE_WORDS}")
        if role is not None:
            employer = (role.employer.split() or [""])[0]
            if employer and re.search(rf"\b{re.escape(employer)}\b", text, re.I):
                hard.append(f"names the employer ({role.employer}), which prints above it")
            if role.descriptor.strip() and claim_scope.repeats(role.descriptor, text):
                hard.append(f"repeats the role's descriptor {role.descriptor!r}, which prints above it")
    else:
        most = ACHIEVEMENT_WORDS if kind == ACHIEVEMENT else BULLET_WORDS
        if words > most:
            hard.append(f"is {words} words; the most is {most}")
        if words < FEWEST_WORDS and _bare(text) not in approved(data) and _bare(text) not in {
                _bare(t) for t in ids.values()}:
            hard.append(f"is {words} words: too thin to print as a bullet")
        if not opens_past(text):
            hard.append("does not open with a past-tense verb")
    hard += _text_checks(docs, [(ident, text)])
    source = " ".join(f"{ids.get(i, '')} {notes_of(docs.resume).get(i, '')}" for i in line.get("from") or [])
    hard += [f"gives {f!r} without the hedge the master keeps on it (\"about\", \"approximately\")"
             for f in dropped_hedges(text, " ".join(ids.get(i, "") for i in line.get("from") or []))]
    flags = claim_scope.inflation(text, source)
    return hard, flags


def check_block(block: dict, docs: Documents, data: dict) -> tuple[list[str], list[str]]:
    """Code checks for one letter block: (what fails it, flags for the facts read)."""
    ids, notes = master_lines(docs.resume), notes_of(docs.resume)
    hard: list[str] = []
    flags: list[str] = []
    sentences = [str(s.get("text") or "").strip() for s in block.get("sentences") or []]
    if not sentences:
        return ["has no sentences"], []
    for s in block.get("sentences") or []:
        said = str(s.get("text") or "").strip()
        bad = [i for i in s.get("from") or [] if i not in ids]
        if bad:
            hard.append(f"'{said[:50]}...' cites {bad}, which are not master lines")
        if not s.get("from"):
            flags.append(f"'{said[:50]}...' cites nothing: check every claim in it")
        source = " ".join(f"{ids.get(i, '')} {notes.get(i, '')}" for i in s.get("from") or [])
        hard += [f"'{said[:50]}...' gives {f!r} without the hedge the master keeps on it"
                 for f in dropped_hedges(said, " ".join(ids.get(i, "") for i in s.get("from") or []))]
        flags += [f"'{said[:50]}...': {p}" for p in claim_scope.inflation(said, source)]
    text = " ".join(sentences)
    hard += [f"names {n!r}, which the owner's client rule keeps out of writing"
             for n in protected_hits(protected(data), text)]
    count, most = i_runs(sentences)
    if most >= 2 or count * 2 >= len(sentences) and len(sentences) > 1:
        hard.append(f"voice: {count} of {len(sentences)} sentences open with 'I' ({most} in a row); let the client, "
                    "the work and the team carry more of them")
    hard += _text_checks(docs, [(block["id"], text)])
    return hard, flags


def _items(data: dict, ids: set[str] | None) -> tuple[list[dict], list[dict]]:
    lines = [ln for ln in data.get("lines") or [] if ids is None or ln["id"] in ids]
    blocks = [b for s in data.get("stories") or [] for b in s.get("blocks") or [] if ids is None or b["id"] in ids]
    return lines, blocks


def run_code_checks(data: dict, docs: Documents, ids: set[str] | None) -> dict:
    """The code checks on these items (all of them for None): {failing: {id: [why]}, flags: {id: [flag]}}."""
    failing: dict[str, list[str]] = {}
    flags: dict[str, list[str]] = {}
    lines, blocks = _items(data, ids)
    for ln in lines:
        hard, flag = check_line(ln, docs, data)
        if hard:
            failing[ln["id"]] = hard
        if flag:
            flags[ln["id"]] = flag
    for b in blocks:
        hard, flag = check_block(b, docs, data)
        if hard:
            failing[b["id"]] = hard
        if flag:
            flags[b["id"]] = flag
    return {"failing": failing, "flags": flags}


# --- the build: what changed --------------------------------------------------------------------------------------


async def _draft(session: AsyncSession, data: dict) -> StoryLibrary:
    row = await session.get(StoryLibrary, uuid.UUID(str(data["library"])))
    if row is None:
        raise seats.SeatError("the library this build belongs to isn't there")
    return row


def _save_data(row: StoryLibrary, data: dict) -> None:
    row.data = json.loads(json.dumps(data, default=str))


def plan(docs: Documents, before: dict | None, rules_changed: bool) -> dict:
    """What a build writes, from the newest library and the master now: the entries to decide again, the roles whose
    scope line is written again, the stories written again, the master entries no story cites yet, and what is kept."""
    now = sources(docs.resume)
    all_entries = [e["entry"] for e in entries(docs.resume)]
    if not before:
        return {"entries": all_entries, "scopes": [r.id for r in docs.resume.roles], "stories": "all",
                "new_material": [], "rules_changed": True, "lines": [], "kept_stories": []}
    was = before.get("sources") or {}
    changed = {k for k, v in now.items() if was.get(k) != v} | {k for k in was if k not in now}
    kept_lines = [ln for ln in before.get("lines") or []
                  if not ({ln.get("entry"), *(ln.get("from") or [])} & changed)
                  and not (ln["kind"] == SCOPE and f"role:{ln.get('role')}" in changed)
                  and (ln.get("entry") in now or ln["kind"] == SCOPE)]
    decided = {ln.get("entry") for ln in before.get("lines") or []} | set(before.get("decided") or [])
    redo = [e for e in all_entries if e in changed or e not in decided]
    scopes = [r.id for r in docs.resume.roles if f"role:{r.id}" in changed
              or not any(ln["kind"] == SCOPE and ln.get("role") == r.id for ln in kept_lines)]
    stories = before.get("stories") or []
    redo_stories = [s["id"] for s in stories if any(set(block_ids(b)) & changed for b in s.get("blocks") or [])]
    kept_stories = [s for s in stories if s["id"] not in redo_stories]
    cited = {i for s in kept_stories for b in s.get("blocks") or [] for i in block_ids(b)}
    new_material = [e for e in all_entries if e not in was and e not in cited]
    return {"entries": redo, "scopes": scopes, "stories": redo_stories, "new_material": new_material,
            "rules_changed": rules_changed, "lines": kept_lines, "kept_stories": kept_stories}


def plan_parts(docs: Documents, todo: dict) -> list[dict]:
    """The parts a build writes side by side: the resume lines (always: the same turn reads the owner's rules, and a
    new entry may name a client the rules keep out), and the letter blocks when there are stories to write."""
    out: list[dict] = [{"kind": LINES_PART}]
    if todo.get("stories") == "all" or todo.get("stories") or todo.get("new_material"):
        out.append({"kind": LETTERS_PART})
    return out


async def action_gather(session, instance, data):
    """What this build writes (`plan`) and the parts written side by side, kept on the draft."""
    owner = data["owner"]
    row = await _draft(session, data)
    docs = await profile_store.owner_documents(session, owner)
    before = await newest(session, owner)
    prev = before.data if before is not None else None
    todo = plan(docs, prev, rules_changed=before is None or before.writing_version != row.writing_version)
    draft = {
        "rules": (prev or {}).get("rules") or {}, "sources": sources(docs.resume),
        "decided": sorted({*((prev or {}).get("decided") or []), *[ln.get("entry") for ln in todo["lines"]]} - {None}),
        "lines": todo.pop("lines"), "stories": todo.pop("kept_stories"), "todo": todo,
        "next": (prev or {}).get("next") or 1, "written": [], "log": [], "failing": {}, "flags": {},
        "built_from": {"library": before.version if before else None},
    }
    parts = plan_parts(docs, todo)
    _save_data(row, draft)
    await session.flush()
    work = bool(todo["entries"] or todo["scopes"] or todo["rules_changed"]) or len(parts) > 1
    return {"parts": parts, "work": work}


async def action_code_checks(session, instance, data):
    """The code checks on what this build wrote (round 1: every new item, or every item when the rules changed; round
    2: what the fix changed)."""
    row = await _draft(session, data)
    docs = await profile_store.owner_documents(session, data["owner"])
    draft = dict(row.data or {})
    ids: set[str] | None = None if (draft.get("todo") or {}).get("rules_changed") else set(draft.get("written") or [])
    found = run_code_checks(draft, docs, ids)
    draft["failing"], draft["flags"] = found["failing"], found["flags"]
    draft["checked"] = sorted(ids) if ids is not None else [x["id"] for x in [*_items(draft, None)[0],
                                                                                 *_items(draft, None)[1]]]
    draft["log"] = [*(draft.get("log") or []),
                    f"code checks: {len(draft['checked'])} items, {len(found['failing'])} fail"]
    _save_data(row, draft)
    await session.flush()
    return {"failing": len(found["failing"])}


async def action_save(session, instance, data):
    """Hold out what still fails, keep the version as ready, and start another build when the master or the rules
    moved while this one ran."""
    owner = data["owner"]
    row = await _draft(session, data)
    draft = dict(row.data or {})
    failing = draft.get("failing") or {}
    names = protected(draft)
    for ln in draft.get("lines") or []:
        if ln["id"] in failing:
            ln["held"], ln["why_held"] = True, "; ".join(failing[ln["id"]])[:500]
        if ln.get("skills"):  # a competency term is printed as written: one naming what the rules keep out goes
            ln["skills"] = [s for s in ln["skills"] if not protected_hits(names, s)]
    for s in draft.get("stories") or []:
        for b in s.get("blocks") or []:
            if b["id"] in failing:
                b["held"], b["why_held"] = True, "; ".join(failing[b["id"]])[:500]
    for key in ("todo", "failing", "flags", "fixed", "checked", "written"):
        draft.pop(key, None)
    held = sum(1 for ln in draft.get("lines") or [] if ln.get("held")) + sum(
        1 for s in draft.get("stories") or [] for b in s.get("blocks") or [] if b.get("held"))
    draft["log"] = [*(draft.get("log") or []), f"saved; {held} held out"]
    _save_data(row, draft)
    row.status, row.done_at = READY, datetime.now(UTC)
    await trail.record(session, owner, event="story_library_built", actor="agent", agent_slug="scout", who="Writer",
                       sentence=f"Your story library was built (version {row.version}): "
                                f"{len(usable_lines(draft))} resume lines and {len(usable_blocks(draft))} letter "
                                f"blocks{f'; {held} held out' if held else ''}.")
    await session.flush()
    # The master or the rules moved while this build ran: the next one starts now.
    await ensure_current(session, owner)
    return {"status": READY}


async def fail(session: AsyncSession, owner: str, instance: ProcInstance, why: str) -> None:
    """A seat of the build, or of one of its parts, failed every try: the build is closed as failed (its own run
    cancelled, and with it every part still going) and the newest ready library stays."""
    row = await session.get(StoryLibrary, uuid.UUID(str((instance.data or {}).get("library"))))
    if row is not None and row.status == BUILDING:
        row.status, row.done_at = FAILED, datetime.now(UTC)
        row.data = {**(row.data or {}), "failed": why}
    build = row.instance_id if row is not None and row.instance_id else instance.id
    await process.engine().cancel(session, build, why[:200])
    await trail.record(session, owner, event="story_library_failed", actor="app",
                       sentence="Your story library could not be built; the last one stays in use.", detail=why)
    await session.flush()


# --- the seats ------------------------------------------------------------------------------------------------------


async def _row_of(session: AsyncSession, instance: ProcInstance, *, lock: bool = False) -> tuple[StoryLibrary, dict]:
    """The draft library and its data. `lock`: the row is locked until the commit, so parts answering side by side
    each add to what the others wrote instead of writing over it."""
    if not lock:
        row = await _draft(session, instance.data or {})
        return row, dict(row.data or {})
    row = await session.get(StoryLibrary, uuid.UUID(str((instance.data or {}).get("library"))),
                            populate_existing=True)
    if row is None:
        raise seats.SeatError("the library this build belongs to isn't there")
    return row, dict(row.data or {})


def _owner_rules(docs: Documents) -> dict:
    return rules_for(docs.writing, "resume", "letter", "review")


async def build_lines(session: AsyncSession, owner: str, instance: ProcInstance) -> str:
    _row, draft = await _row_of(session, instance)
    docs = await profile_store.owner_documents(session, owner)
    todo = draft.get("todo") or {}
    wanted = set(todo.get("entries") or [])
    ents = [e for e in entries(docs.resume) if e["entry"] in wanted]
    roles = {r.id: r for r in docs.resume.roles}
    scopes = [{"role": i, "title": roles[i].title, "employer": roles[i].employer,
               "descriptor_printed_above": roles[i].descriptor} for i in todo.get("scopes") or [] if i in roles]
    kept = [{"id": ln["id"], "role": ln.get("role"), "claim": ln.get("claim") or ln.get("work"), "text": ln["text"]}
            for ln in usable_lines(draft) if ln["kind"] != SCOPE]
    return (seats.section("THE MASTER RESUME (every line has an id; notes are the owner's limits on how a line may be "
                          "used; evidence entries hold the owner's own notes)", master_view(docs))
            + seats.section("ENTRIES TO DECIDE (every one; evidence filed under several roles is listed once)",
                            ents or "(none: only the rules are read again)")
            + seats.section("THE SCOPE LINES TO WRITE", scopes or "(none)")
            + seats.section("LINES THE LIBRARY KEEPS (never written again; a new line never tells one of their claims "
                            "again)", kept or "(none)")
            + seats.section("THE OWNER'S RULES", _owner_rules(docs)))


def _rules_of(reply: dict, roles: set[str]) -> dict:
    rules = reply.get("rules")
    if not isinstance(rules, dict):
        raise seats.ReplyError("the reply has no rules object")
    by_role = {}
    for r in rules.get("roles") or []:
        if isinstance(r, dict) and r.get("role") in roles:
            lo, hi = r.get("min"), r.get("max")
            by_role[r["role"]] = {"min": lo if isinstance(lo, int) else None,
                                  "max": hi if isinstance(hi, int) else None,
                                  "firm_first": bool(r.get("firm_first")), "scope": bool(r.get("scope"))}
    return {"protected": [str(x) for x in rules.get("protected") or [] if str(x).strip()],
            "approved": [str(x) for x in rules.get("approved") or [] if str(x).strip()],
            "roles": by_role}


def _new_id(draft: dict) -> str:
    n = int(draft.get("next") or 1)
    draft["next"] = n + 1
    return f"L{n:03d}"


def _skills(raw) -> list[str]:
    out: list[str] = []
    for s in raw or []:
        name = " ".join(str(s).split())
        if name and name.lower() not in {x.lower() for x in out}:
            out.append(name)
    return out


async def answer_lines(session: AsyncSession, owner: str, instance: ProcInstance, reply: dict) -> dict:
    row, draft = await _row_of(session, instance, lock=True)
    docs = await profile_store.owner_documents(session, owner)
    roles = {r.id for r in docs.resume.roles}
    wanted = set((draft.get("todo") or {}).get("entries") or [])
    mine = {e["entry"]: e for e in entries(docs.resume) if e["entry"] in wanted}
    draft["rules"] = _rules_of(reply, roles)
    written = list(draft.get("written") or [])
    added = 0
    lines = list(draft.get("lines") or [])
    scoping = set((draft.get("todo") or {}).get("scopes") or [])
    lines = [ln for ln in lines if not (ln["kind"] == SCOPE and ln.get("role") in scoping)]
    for s in reply.get("scopes") or []:
        if isinstance(s, dict) and s.get("role") in scoping and str(s.get("text") or "").strip():
            ident = f"S-{s['role']}"
            if ident in written:
                continue
            lines.append({"id": ident, "kind": SCOPE, "role": s["role"], "text": str(s["text"]).strip(),
                          "from": [str(i) for i in s.get("from") or []]})
            written.append(ident)
            added += 1
    decided = set(draft.get("decided") or [])
    for e in reply.get("entries") or []:
        if not isinstance(e, dict) or e.get("entry") not in mine:
            continue
        decided.add(e["entry"])
        if e.get("status") != "bullet":
            continue
        entry = mine[e["entry"]]
        for b in e.get("bullets") or []:
            if not isinstance(b, dict) or not str(b.get("text") or "").strip():
                continue
            role = b.get("role") if b.get("role") in roles else None
            kind = ACHIEVEMENT if entry["kind"] == ACHIEVEMENT or role is None else BULLET
            ident = _new_id(draft)
            lines.append({"id": ident, "kind": kind, "role": role, "entry": e["entry"],
                          "claim": str(b.get("claim") or b.get("work") or e["entry"]),
                          "side": str(b.get("side") or ""), "for": str(b.get("for") or ""),
                          "skills": _skills(b.get("skills")), "text": str(b["text"]).strip(),
                          "from": [str(i) for i in b.get("from") or [e["entry"]]]})
            written.append(ident)
            added += 1
    draft["lines"], draft["written"], draft["decided"] = lines, written, sorted(decided)
    draft["log"] = [*(draft.get("log") or []), f"lines: {added} written"]
    _save_data(row, draft)
    await session.flush()
    return {"written": added}


async def build_letters(session: AsyncSession, owner: str, instance: ProcInstance) -> str:
    _row, draft = await _row_of(session, instance)
    docs = await profile_store.owner_documents(session, owner)
    todo = draft.get("todo") or {}
    kept = [{"id": s["id"], "title": s.get("title"), "index": s.get("index")} for s in draft.get("stories") or []]
    if todo.get("stories") == "all":
        redo: object = "every story: this is the first build"
    else:
        redo = todo.get("stories") or "(none)"
    return (seats.section("THE MASTER RESUME (every line has an id; notes are the owner's limits; evidence entries "
                          "hold the owner's own notes)", master_view(docs))
            + seats.section("HOW THE CANDIDATE WANTS TO COME ACROSS (the owner's words: a guide to tone, never quoted)",
                            docs.resume.philosophy or "(not given)")
            + seats.section("STORIES THE LIBRARY KEEPS (do not write these again)", kept or "(none)")
            + seats.section("STORIES TO WRITE AGAIN (their master lines changed)", redo)
            + seats.section("MASTER ENTRIES NO STORY TELLS YET (write a new story only where they hold one)",
                            todo.get("new_material") or "(none)")
            + seats.section("THE OWNER'S RULES", rules_for(docs.writing, "letter", "review")))


def _slug(text: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "story"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    taken.add(out)
    return out


def _sentences(raw) -> list[dict]:
    out = []
    for s in raw or []:
        if isinstance(s, dict) and str(s.get("text") or "").strip():
            out.append({"text": str(s["text"]).strip(), "from": [str(i) for i in s.get("from") or []]})
    return out


async def answer_letters(session: AsyncSession, owner: str, instance: ProcInstance, reply: dict) -> dict:
    row, draft = await _row_of(session, instance, lock=True)
    if not isinstance(reply.get("stories"), list):
        raise seats.ReplyError("the reply has no stories list")
    stories = list(draft.get("stories") or [])
    taken = {s["id"] for s in stories} | {b["id"] for s in stories for b in s.get("blocks") or []}
    written = list(draft.get("written") or [])
    for s in reply["stories"]:
        if not isinstance(s, dict):
            continue
        sid = _slug(str(s.get("id") or s.get("title") or "story"), taken)
        blocks = []
        for n, b in enumerate(s.get("blocks") or []):
            sentences = _sentences((b or {}).get("sentences"))
            if not sentences:
                continue
            bid = _slug(f"{sid}-{chr(97 + n)}", taken)
            blocks.append({"id": bid, "for": str((b or {}).get("for") or ""), "sentences": sentences})
            written.append(bid)
        if blocks:
            stories.append({"id": sid, "title": str(s.get("title") or ""), "index": str(s.get("index") or ""),
                            "blocks": blocks})
    draft["stories"], draft["written"] = stories, written
    draft["log"] = [*(draft.get("log") or []), f"letters: {len(stories)} stories"]
    _save_data(row, draft)
    await session.flush()
    return {"stories": len(stories)}


def _as_items(draft: dict, ids: set[str]) -> list[dict]:
    lines, blocks = _items(draft, ids)
    return ([{"id": ln["id"], "kind": ln["kind"], "text": ln["text"], "from": ln.get("from") or [],
              **({"skills": ln["skills"]} if ln.get("skills") else {})} for ln in lines]
            + [{"id": b["id"], "kind": "letter block", "sentences": b["sentences"]} for b in blocks])


def _facts_ids(draft: dict) -> set[str]:
    """What a facts read checks: what this build wrote (round 1) or what the fix changed (round 2)."""
    fixed = draft.get("fixed")
    return set(fixed) if fixed is not None else set(draft.get("written") or [])


async def build_check(session: AsyncSession, owner: str, instance: ProcInstance) -> str:
    _row, draft = await _row_of(session, instance)
    docs = await profile_store.owner_documents(session, owner)
    ids = _facts_ids(draft)
    items = _as_items(draft, ids)
    cited = {i for x in items for i in x.get("from") or []} | {
        i for x in items for s in x.get("sentences") or [] for i in s.get("from") or []}
    flags = [f"{k}: {f}" for k, v in (draft.get("flags") or {}).items() if k in ids for f in v]
    return (seats.section("THE ITEMS", items or "(none)")
            + seats.section("MASTER LINES THEY CITE (with the owner's notes)", lines_with_notes(docs, cited))
            + seats.section("THE CANDIDATE'S ROLES AND EDUCATION (as the resume prints them)",
                            roles_and_education(docs))
            + seats.section("THE OWNER'S RULES ON WORDING", rules_for(docs.writing, "review"))
            + seats.section("CODE FLAGS (decide each)", "\n".join(flags) or "(none)"))


async def answer_check(session: AsyncSession, owner: str, instance: ProcInstance, reply: dict) -> dict:
    row, draft = await _row_of(session, instance)
    if not isinstance(reply.get("failures"), list):
        raise seats.ReplyError("the reply has no failures list")
    ids = _facts_ids(draft)
    failing = dict(draft.get("failing") or {})
    for f in reply["failures"]:
        if isinstance(f, dict) and f.get("id") in ids:
            failing.setdefault(f["id"], []).append(f"facts: {str(f.get('reason') or '')[:400]}")
    draft["failing"] = failing
    # a skill the line does not show is dropped from the line; the line itself stands
    drop: dict[str, set[str]] = {}
    for x in reply.get("skills_dropped") or []:
        if isinstance(x, dict) and x.get("id") in ids and str(x.get("skill") or "").strip():
            drop.setdefault(x["id"], set()).add(" ".join(str(x["skill"]).split()).lower())
    for ln in draft.get("lines") or []:
        if ln["id"] in drop:
            ln["skills"] = [s for s in ln.get("skills") or [] if s.lower() not in drop[ln["id"]]]
    draft["log"] = [*(draft.get("log") or []), f"facts read: {len(ids)} items, {len(failing)} fail"]
    _save_data(row, draft)
    await session.flush()
    return {"failing": len(failing)}


async def build_fix(session: AsyncSession, owner: str, instance: ProcInstance) -> str:
    _row, draft = await _row_of(session, instance)
    docs = await profile_store.owner_documents(session, owner)
    failing = draft.get("failing") or {}
    items = _as_items(draft, set(failing))
    cited = {i for x in items for i in x.get("from") or []} | {
        i for x in items for s in x.get("sentences") or [] for i in s.get("from") or []}
    return (seats.section("THE ITEMS", items)
            + seats.section("FIX THESE (code checks and the facts read)",
                            "\n".join(f"{k}: {why}" for k, v in failing.items() for why in v))
            + seats.section("MASTER LINES THEY CITE (with the owner's notes)", lines_with_notes(docs, cited))
            + seats.section("THE OWNER'S RULES", _owner_rules(docs)))


async def answer_fix(session: AsyncSession, owner: str, instance: ProcInstance, reply: dict) -> dict:
    row, draft = await _row_of(session, instance)
    if not isinstance(reply.get("lines"), list) and not isinstance(reply.get("blocks"), list):
        raise seats.ReplyError("the reply has neither lines nor blocks")
    failing = set(draft.get("failing") or {})
    fixed = []
    by_line = {ln["id"]: ln for ln in draft.get("lines") or []}
    for x in reply.get("lines") or []:
        if isinstance(x, dict) and x.get("id") in failing and x["id"] in by_line and str(x.get("text") or "").strip():
            by_line[x["id"]]["text"] = str(x["text"]).strip()
            if x.get("from"):
                by_line[x["id"]]["from"] = [str(i) for i in x["from"]]
            fixed.append(x["id"])
    by_block = {b["id"]: b for s in draft.get("stories") or [] for b in s.get("blocks") or []}
    for x in reply.get("blocks") or []:
        if isinstance(x, dict) and x.get("id") in failing and x["id"] in by_block and _sentences(x.get("sentences")):
            by_block[x["id"]]["sentences"] = _sentences(x["sentences"])
            fixed.append(x["id"])
    # What the fix did not return still fails: it stays in `failing` and is held out when the build is saved.
    draft["failing"] = {k: v for k, v in (draft.get("failing") or {}).items() if k not in fixed}
    draft["carried"] = draft["failing"]
    draft["fixed"] = fixed
    draft["log"] = [*(draft.get("log") or []), f"fix: {len(fixed)} of {len(failing)} changed"]
    _save_data(row, draft)
    await session.flush()
    return {"fixed": len(fixed)}


async def action_recheck(session, instance, data):
    """Round 2's code checks, on what the fix changed; what the fix left alone keeps failing."""
    row = await _draft(session, data)
    docs = await profile_store.owner_documents(session, data["owner"])
    draft = dict(row.data or {})
    ids = set(draft.get("fixed") or [])
    found = run_code_checks(draft, docs, ids)
    draft["failing"] = {**(draft.pop("carried", None) or {}), **found["failing"]}
    draft["flags"] = found["flags"]
    draft["log"] = [*(draft.get("log") or []), f"code checks again: {len(ids)} items, {len(found['failing'])} fail"]
    _save_data(row, draft)
    await session.flush()
    return {"failing": len(draft["failing"])}


async def _always(session: AsyncSession, owner: str, instance: ProcInstance) -> bool:
    return True


for _process, _element, _model, _label, _build, _answer, _effort in (
        (process.LIBRARY_PART, LINES_STEP, seats.OPUS, "Writing the story library's resume lines", build_lines,
         answer_lines, None),
        (process.LIBRARY_PART, LETTERS_STEP, seats.OPUS, "Writing the story library's letter blocks", build_letters,
         answer_letters, None),
        (process.LIBRARY, CHECK_1, seats.SONNET, "Checking the story library's facts", build_check, answer_check, None),
        (process.LIBRARY, FIX_STEP, seats.OPUS, "Fixing lines in the story library", build_fix, answer_fix, "low"),
        (process.LIBRARY, CHECK_2, seats.SONNET, "Checking the story library's fixes", build_check, answer_check,
         None)):
    seats.register(seats.Seat(process=_process, element=_element, model=_model, label=_label, build=_build,
                              answer=_answer, effort=_effort, owners=_always))
seats.on_fail(process.LIBRARY, fail)
seats.on_fail(process.LIBRARY_PART, fail)

_SOURCE = "backend/app/jobsearch/writing/library.py"
registry.register("jobs.library_gather", action_gather, inputs=["owner", "library"],
                  outputs=["parts", "work"],
                  description="Works out what the build writes (the master entries and stories whose lines changed) "
                              "and the parts written side by side.",
                  source=_SOURCE)
registry.register("jobs.library_code_checks", action_code_checks, inputs=["owner", "library"], outputs=["failing"],
                  description="Runs the code checks on the lines and blocks this build wrote.", source=_SOURCE)
registry.register("jobs.library_recheck", action_recheck, inputs=["owner", "library"], outputs=["failing"],
                  description="Runs the code checks again on what the fix changed.", source=_SOURCE)
registry.register("jobs.library_save", action_save, inputs=["owner", "library"], outputs=["status"],
                  description="Holds out what still fails and keeps the library as the newest ready version.",
                  source=_SOURCE)


async def instance_of(session: AsyncSession, row: StoryLibrary) -> ProcInstance | None:
    return await session.get(ProcInstance, row.instance_id) if row.instance_id else None


async def going(session: AsyncSession, owner: str) -> bool:
    row = await building(session, owner)
    if row is None:
        return False
    instance = await instance_of(session, row)
    return instance is not None and instance.status in (RUNNING, WAITING)
