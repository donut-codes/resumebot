"""The packet writer (PRD path 43): one packet put together from the story library.

The order is `playbooks/jobsearch/build_packet.bpmn`; this module holds its seats and its code steps.

1. **Compose** (an Opus seat): chooses the library's resume lines and their order within the owner's limits, the scope
   lines, the competencies, the two or three letter blocks, and writes the few new sentences: the headline and
   summary by the owner's rules, the letter's opening, the links between blocks and the close. Code turns the library
   ids into the master lines they rest on (`assemble`), prints one line of each claim (`story_library.claims`), puts a
   role's work owned inside the employer before its client work where the owner's rules ask for that, and cuts bullets
   until the resume fits the owner's page limit (`trim`).
2. **Once-over**, in the same turn: the composer reads the whole packet as a seasoned hiring manager and editor and
   cuts it. Code holds every draft to the owner's per-role counts and prints only the competency terms that are skills
   of a printed line (`settle`).
3. **The gate**: code fact checks (`code_findings`: citations, the owner's banned phrases and style rules, the client
   rule, the per-role counts, uncited letter paragraphs, universal words; scope inflation as a flag), then one facts
   read (a Sonnet seat) that traces every claim by meaning and is given the roles and education the resume prints.
   Nothing about style is ever sent back.
4. A failed gate goes to a **fix** (a Sonnet seat at low effort, the named lines only), then the gate again; a fact
   check still failing goes to the **arbitrator** (Opus), whose fixes get one final pass.
5. **File**: the app's checks on the final packet, the pay ask from the role's research, the rendered resume; ready or
   held for the owner.

Services flush; the route that owns the request commits.
"""

from __future__ import annotations

import difflib
import io
import json
import re
import uuid
from datetime import UTC, datetime

import pypdf
from process_engine.models import ProcInstance
from process_engine.registry import actions as registry
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch import process
from app.jobsearch.fit import comp
from app.jobsearch.measures.pay_place import hourly_part_time
from app.jobsearch.models import Opportunity, Packet
from app.jobsearch.postings import text as posting_text
from app.jobsearch.profile import store as profile_store
from app.jobsearch.profile.documents import Documents, rules_for
from app.jobsearch.states import PacketState
from app.jobsearch.work import stuck
from app.jobsearch.writing import docs_render, packets, seats
from app.jobsearch.writing import library as story_library

#: The process's agent steps (`build_packet.bpmn`).
COMPOSE, FACTS_1, FIX, FACTS_2, ARBITRATE, FINAL_FIX = (
    "Compose", "Facts_1", "Fix", "Facts_2", "Arbitrate", "Final_fix")
#: Where the packet keeps its working draft, what the gates found and the arbitrator's ruling, in its brief.
DRAFT, GATES, TODO, RULING, STEP = "draft", "gates", "fix_todo", "arbitrator", "step"
#: The universal words a letter claim may not add to its cited lines.
UNIVERSAL = ("each", "every", "all", "always", "entire", "whole", "both", "everyone", "every time")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")
_PAGES = re.compile(r"\b(?:at most|no more than|up to|never more than|maximum of|max(?:imum)?|within)\s+"
                    r"(one|two|three|1|2|3)\s+(?:full\s+)?pages?\b", re.I)
_PAGE_WORDS = {"one": 1, "two": 2, "three": 3, "1": 1, "2": 2, "3": 3}


# --- reading the packet ---------------------------------------------------------------------------------------------


async def _packet(session: AsyncSession, instance: ProcInstance) -> Packet:
    packet = await session.get(Packet, uuid.UUID(str((instance.data or {}).get("packet"))))
    if packet is None:
        raise seats.SeatError("the packet this process belongs to isn't there")
    return packet


def _brief(packet: Packet) -> dict:
    return json.loads(json.dumps(packet.brief or {}, default=str))


def _keep(packet: Packet, brief: dict) -> None:
    packet.brief = json.loads(json.dumps(brief, default=str))


def resume_page_limit(docs: Documents, target_role: str | None = None) -> int | None:
    """The most pages the owner's resume rules allow ("at most two pages"), read from their own words; None when they
    set none."""
    w = docs.writing
    rules = [*w.resume_global, *w.resume_experience, *(w.role_rules.get(target_role or "") or [])]
    found = [_PAGE_WORDS[m.lower()] for r in rules for m in _PAGES.findall(r.says.split("Verify:")[0])]
    return min(found) if found else None


def requirement_match(role: Opportunity | None, data: dict | None = None) -> list[str]:
    """The role's research as a requirement match: each item the posting asks for, how the master meets it (P proven,
    T transferable, U not shown) and the master lines that show it; with the library (`data`), also the library lines
    that rest on those master lines, so the composer can choose the evidence the research found."""
    a = (role.analysis if role is not None else None) or {}
    lines = story_library.usable_lines(data or {})
    out = []
    for part in ("required", "preferred", "responsibilities"):
        for r in a.get(part) or []:
            if isinstance(r, dict) and r.get("item"):
                cites = f" [{', '.join(r['from'])}]" if r.get("from") else ""
                why = f": {r['why']}" if r.get("why") else ""
                group = f" [{r['group']}]" if r.get("group") else ""
                found = sorted(ln["id"] for ln in lines if ln["kind"] != story_library.SCOPE
                               and set(ln.get("from") or []) & set(r.get("from") or []))
                evidence = f" (library lines: {', '.join(found)})" if found else ""
                out.append(f"{part}{group}: ({r.get('class', '?')}) {r['item']}{why}{cites}{evidence}")
    return out


def claim_labels(data: dict) -> dict[str, str]:
    """Each usable line's claim, by line id, as a composer reads it: lines with the same label tell one accomplishment
    and never print together."""
    lines = story_library.usable_lines(data)
    by_id = {ln["id"]: ln for ln in lines}
    groups = story_library.claims(lines)
    return {i: str(by_id[root].get("claim") or by_id[root].get("work") or root) for i, root in groups.items()}


def library_for_compose(data: dict, docs: Documents) -> dict:
    """The library as a composer chooses from it: each role (in the master's order) with what prints above it, the
    owner's count for it, its scope line and its lines (each with its claim and the skills it shows); then the
    achievements."""
    lines = story_library.usable_lines(data)
    claim = claim_labels(data)
    out = []
    for r in docs.resume.roles:
        rules = story_library.role_rules(data, r.id)
        mine = [ln for ln in lines if ln.get("role") == r.id]
        scope = next((ln for ln in mine if ln["kind"] == story_library.SCOPE), None)
        limit = ("no set count" if rules["min"] is None and rules["max"] is None
                 else f"{rules['min'] or 0} to {rules['max'] if rules['max'] is not None else 'any'} lines")
        out.append({
            "role": r.id, "printed_above": " | ".join(x for x in (r.employer, r.title, f"{r.start} to {r.end}",
                                                                  r.descriptor) if x),
            "limit": limit, **({"scope_line_always_prints": True} if rules["scope"] else {}),
            "scope_line": {"id": scope["id"], "text": scope["text"]} if scope else None,
            "lines": [{"id": ln["id"], "claim": claim.get(ln["id"]),
                       **({"side": ln.get("side")} if rules["firm_first"] else {}), "for": ln.get("for"),
                       "skills": ln.get("skills") or [], "words": len(ln["text"].split()), "text": ln["text"]}
                      for ln in mine if ln["kind"] == story_library.BULLET]})
    return {"roles": out, "achievements": [{"id": ln["id"], "claim": claim.get(ln["id"]),
                                            "skills": ln.get("skills") or [], "text": ln["text"]}
                                           for ln in lines if ln["kind"] == story_library.ACHIEVEMENT]}


def blocks_for_compose(data: dict) -> list[dict]:
    return [{"story": s.get("title"), "shows": s.get("index"),
             "blocks": [{"id": b["id"], "for": b.get("for"), "text": story_library.block_text(b)}
                        for b in s.get("blocks") or [] if not b.get("held")]}
            for s in data.get("stories") or []]


async def _role_and_library(session: AsyncSession, owner: str, packet: Packet):
    role = await session.get(Opportunity, packet.role_id)
    library = await story_library.usable(session, owner)
    if library is None:
        raise seats.ReplyError("there is no story library to put the packet together from yet")
    return role, library


async def _posting(session: AsyncSession, role: Opportunity | None) -> str:
    if role is None:
        return ""
    text = (await posting_text.held(session, role)).text or ""
    return f"{role.company}: {role.title}\n\n{text}".strip()


# --- compose and once-over --------------------------------------------------------------------------------------------


def _owner_rules(docs: Documents, target: tuple[str, str] | None) -> dict:
    return rules_for(docs.writing, "resume", "letter", "review", role=target)


async def _target(session: AsyncSession, owner: str, role: Opportunity | None) -> tuple[str, str] | None:
    criteria = (await profile_store.current(session, owner)).criteria
    t = criteria.role(role.target_role if role is not None else None)
    return (t.id, t.name) if t else None


async def _common(session: AsyncSession, owner: str, packet: Packet, role: Opportunity | None, library) -> str:
    docs = await profile_store.owner_documents(session, owner)
    profile = await profile_store.current(session, owner)
    limit = resume_page_limit(docs, role.target_role if role is not None else None)
    earlier = None
    if packet.feedback:
        from sqlalchemy import select

        earlier = await session.scalar(select(Packet).where(
            Packet.role_id == packet.role_id, Packet.version < packet.version, Packet.status != "writing")
            .order_by(Packet.version.desc()).limit(1))
    low, high = packets.letter_word_limits(docs.writing, role.target_role if role is not None else None)
    return (seats.section("THE POSTING", await _posting(session, role) or "(no posting text is held)")
            + seats.section("THE REQUIREMENT MATCH (the role's research: each item the posting asks for, how the "
                            "master meets it: P proven, T transferable, U not shown, the master lines that show "
                            "it, and the library lines that rest on them)",
                            "\n".join(requirement_match(role, library.data or {})) or "(no research yet)")
            + seats.section("THE RESUME LIBRARY (approved lines by role; choose and order them, never reword one)",
                            library_for_compose(library.data or {}, docs))
            + seats.section("THE LETTER BLOCKS (approved; by story)", blocks_for_compose(library.data or {}))
            + seats.section("THE OWNER'S LIMITS", {
                "resume_pages": limit or "not set", "letter_words": f"{low} to {high}" if low or high else "not set"})
            + seats.section("HOW THE CANDIDATE WANTS TO COME ACROSS (the owner's words: a guide to tone only, never "
                            "quoted and never a source of facts)", docs.resume.philosophy or "(not given)")
            + (seats.section("THE OWNER'S NOTES ON THIS ROLE", role.notes) if role is not None and role.notes else "")
            + (seats.section("THE OWNER'S POSITIONING", profile.criteria.positioning)
               if profile.criteria.positioning else "")
            + (seats.section("THE OWNER'S RULES FOR PACKETS", [r.says for r in profile.settings.rules
                                                               if r.area == "packets"])
               if any(r.area == "packets" for r in profile.settings.rules) else "")
            + (seats.section("THE CHANGE THE OWNER ASKED FOR", packet.feedback) if packet.feedback else "")
            + (seats.section("THE VERSION THEY ASKED TO CHANGE", {"resume": packets.printed(earlier.resume),
                                                                  "letter": earlier.letter})
               if earlier is not None else "")
            + seats.section("THE OWNER'S RULES", _owner_rules(docs, await _target(session, owner, role))))


async def build_compose(session: AsyncSession, owner: str, instance: ProcInstance) -> str:
    packet = await _packet(session, instance)
    role, library = await _role_and_library(session, owner, packet)
    return await _common(session, owner, packet, role, library)


def _ids(raw) -> list[str]:
    return [str(i) for i in raw or [] if isinstance(i, str | int)]


def without_employer(text: str, employer: str) -> str:
    """The master's own role intro without the employer's name, which prints just above it: a leading "Employer:" or
    "At Employer,", "Employer's practice" as "the practice", and "for Employer" or "at Employer" dropped."""
    full = re.sub(r"\s*\([^)]*\)", "", " ".join(employer.split())).strip(" ,")
    first = (full.split() or [""])[0].strip(",")
    out = " ".join(text.split())
    if not first:
        return out
    n = "(?:" + "|".join(re.escape(x) for x in dict.fromkeys([full, first])) + ")"
    out = re.sub(rf"^(?:at\s+)?{n}(?:'s)?\b[\s,:;-]*", "", out, flags=re.I)
    out = re.sub(rf"\b{n}['\u2019]s\s+", "the ", out, flags=re.I)
    out = re.sub(rf"\s+(?:at|for|with)\s+{n}\b(?:\s*\([^)]*\))?", "", out, flags=re.I)
    out = re.sub(r"\bthe the\b", "the", out, flags=re.I).strip(" ,;:-")
    return out[:1].upper() + out[1:]


def assemble(reply: dict, data: dict, docs: Documents, problems: list[str]) -> dict:
    """A compose or once-over reply turned into the packet's draft: library ids resolved to the master lines they rest
    on, the roles in the master's order, a role's firm-side lines first where the owner's rules ask for it, one line of
    each claim (the first chosen; an achievement after every role's lines), and a scope line where the owner's rules
    say one always prints (the master's own role intro when the library holds none). What does not fit is named in
    `problems`."""
    lines = {ln["id"]: ln for ln in story_library.usable_lines(data)}
    claim_of = story_library.claims(list(lines.values()))
    blocks = {b["id"]: b for b in story_library.usable_blocks(data)}
    master = story_library.master_lines(docs.resume)
    roles_by_id = {r.id: r for r in docs.resume.roles}

    def resolve(ids) -> list[str]:
        out: set[str] = set()
        for i in _ids(ids):
            if i in lines:
                out |= set(lines[i].get("from") or [])
            elif i in blocks:
                out |= set(story_library.block_ids(blocks[i]))
            elif i in master or i in roles_by_id:
                out.add(i)
            else:
                problems.append(f"cites {i!r}, which is neither a library line nor a master line")
        return sorted(out)

    res: dict = reply["resume"] if isinstance(reply.get("resume"), dict) else {}
    picked = {r.get("role"): r for r in res.get("roles") or [] if isinstance(r, dict)}
    unknown = [k for k in picked if k not in roles_by_id]
    problems += [f"chose role {k!r}, which the master doesn't have" for k in unknown]
    roles, works = [], set()
    for r in docs.resume.roles:
        if r.id not in picked:
            continue
        p = picked[r.id]
        rules = story_library.role_rules(data, r.id)
        chosen = []
        # A role's lines are library ids; a reply that wrote them as bullets citing a library id is read the same way,
        # and the library's approved words print, never the reply's.
        named = _ids(p.get("lines")) or [next((i for i in _ids(b.get("from")) if i in lines), str(b.get("text") or ""))
                                         for b in p.get("bullets") or [] if isinstance(b, dict)]
        for i in named:
            ln = lines.get(i)
            if ln is None or ln.get("role") != r.id or ln["kind"] != story_library.BULLET:
                problems.append(f"put {i!r} under {r.title}: not one of its library lines")
                continue
            claim = claim_of.get(ln["id"], ln["id"])
            if claim in works:
                problems.append(f"chose {i}, a second line of one claim: the second is dropped")
                continue
            works.add(claim)
            chosen.append(ln)
        if rules["firm_first"]:
            chosen = [x for x in chosen if x.get("side") != "client"] + [x for x in chosen if x.get("side") == "client"]
        scope = next((ln for ln in lines.values() if ln["kind"] == story_library.SCOPE and ln.get("role") == r.id),
                     None)
        intro, intro_from = "", []
        if scope and (rules["scope"] or p.get("intro")):
            intro, intro_from = scope["text"], scope.get("from") or []
        elif rules["scope"] and r.intro.strip():  # the library's scope line was held out: the master's own words
            intro, intro_from = without_employer(r.intro, r.employer), [r.id]
        roles.append({"role": r.id, "intro": intro, "intro_from": intro_from,
                      "bullets": [{"text": x["text"], "from": x.get("from") or [], "lib": x["id"]} for x in chosen]})
    comps = []
    for c in res.get("competencies") or []:
        if not isinstance(c, dict):
            continue
        items = []
        for x in c.get("items") or []:
            term = x.get("term") if isinstance(x, dict) else x
            backed = x.get("backed_by") if isinstance(x, dict) else None
            if not term:
                continue
            items.append({"term": str(term), "backed_by": resolve([backed]) if backed in lines or backed in master
                          else []})
        if items:
            comps.append({"area": str(c.get("area") or ""), "items": items})
    achievements = []
    for i in _ids(res.get("achievements")):
        if i not in lines or lines[i]["kind"] != story_library.ACHIEVEMENT:
            continue
        if claim_of.get(i, i) in works:
            problems.append(f"chose achievement {i}, which tells a claim a printed line tells: it is dropped")
            continue
        works.add(claim_of.get(i, i))
        achievements.append({"text": lines[i]["text"], "from": lines[i].get("from") or [], "lib": i})
    chosen_blocks = [i for i in _ids(reply.get("blocks")) if i in blocks]
    problems += [f"named block {i!r}, which is not in the library" for i in _ids(reply.get("blocks"))
                 if i not in blocks]
    letter = []
    for p in (reply.get("letter") or {}).get("paragraphs") or []:
        text = p.get("text") if isinstance(p, dict) else p
        if str(text or "").strip():
            letter.append({"text": str(text).strip(), "from": resolve(p.get("from")) if isinstance(p, dict) else []})
    return {
        "thread": str(reply.get("thread") or ""),
        "resume": {"headline": str(res.get("headline") or ""), "summary": str(res.get("summary") or ""),
                   "summary_from": resolve(res.get("summary_from")), "competencies": comps,
                   "achievements": achievements, "roles": roles},
        "letter": letter, "blocks": chosen_blocks,
    }


_STOP = frozenset("and or of the for in to with on at by a an".split())


def _stems(text: str) -> set[str]:
    return {w.lower()[:5] for w in re.findall(r"[A-Za-z0-9]+", text) if w.lower() not in _STOP}


def carried(term: str, texts: list[str]) -> bool:
    """Whether the printed lines a term rests on carry it: more than half of its words are theirs (a two-word term
    needs both), so a term a line only brushes against is not printed."""
    words = [w.lower()[:5] for w in re.findall(r"[A-Za-z0-9]+", term) if w.lower() not in _STOP]
    have = set().union(*(_stems(t) for t in texts)) if texts else set()
    return bool(words) and sum(1 for w in words if w in have) * 2 > len(words)


def _skill_key(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower().replace("&", " and ")))


def settle(draft: dict, data: dict, docs: Documents) -> list[str]:
    """What code holds the draft to, whoever wrote it last (the composer or a fix): each role within the owner's most
    lines (the last ones go), and only the competency terms a printed line backs: a skill the library lists for a
    printed line, or a term a printed line that lists no skills carries (`carried`; a line kept from a library built
    before lines listed skills). Returns what it cut, which is kept on the draft."""
    res = draft.get("resume") or {}
    cut: list[str] = []
    for r in res.get("roles") or []:
        most = story_library.role_rules(data, r["role"])["max"]
        if most is not None and len(r.get("bullets") or []) > most:
            cut += [f"{r['role']}: {b['text'][:60]}" for b in r["bullets"][most:]]
            r["bullets"] = r["bullets"][:most]
    on_page = [*[b for r in res.get("roles") or [] for b in r.get("bullets") or []], *(res.get("achievements") or [])]
    by_id = {ln["id"]: ln for ln in story_library.usable_lines(data)}
    listed = [(b["text"], (by_id.get(str(b.get("lib") or "")) or {}).get("skills") or []) for b in on_page]
    skills = {_skill_key(s) for _text, names in listed for s in names}
    unlisted = [text for text, names in listed if not names]
    comps = []
    for c in res.get("competencies") or []:
        items = []
        for x in c.get("items") or []:
            term = x.get("term") if isinstance(x, dict) else str(x)
            # a term prints when a printed line backs it, whichever line the composer named
            if term and (_skill_key(term) in skills or any(carried(term, [t]) for t in unlisted)):
                items.append(x)
            else:
                cut.append(f"competency {term!r}: no printed line shows it")
        if items:
            comps.append({**c, "items": items})
    res["competencies"] = comps
    draft["settled"] = [*(draft.get("settled") or []), *cut]
    return cut


def cut_of(draft: dict) -> packets.ResumeCut:
    """The draft's resume in the app's shape (competencies as their terms)."""
    res = draft.get("resume") or {}
    roles = [{"role": r["role"], "intro": r.get("intro") or "",
              "bullets": [{"text": b["text"], "from": b.get("from") or []} for b in r.get("bullets") or []]}
             for r in res.get("roles") or []]
    return packets.ResumeCut.model_validate({
        "headline": res.get("headline") or "", "summary": res.get("summary") or "",
        "summary_from": res.get("summary_from") or [],
        "competencies": [{"area": c.get("area") or "", "items": [x["term"] if isinstance(x, dict) else str(x)
                                                                  for x in c.get("items") or []]}
                         for c in res.get("competencies") or []],
        "achievements": [{"text": a["text"], "from": a.get("from") or []} for a in res.get("achievements") or []],
        "roles": roles or [{"role": "none"}]})


def pages_of(printed: dict, docs: Documents) -> int:
    pdf = docs_render.resume_pdf(printed, margin_inches=docs.writing.page_margin_inches)
    return len(pypdf.PdfReader(io.BytesIO(pdf)).pages)


def trim(draft: dict, data: dict, docs: Documents, limit: int | None) -> tuple[int, list[str]]:
    """Cut bullets until the resume prints within the owner's page limit: each time the last bullet of the role with
    the most bullets above its least (the older role on a tie). Returns the pages and the library ids cut."""
    cut: list[str] = []
    pages = pages_of(packets.render_resume(cut_of(draft), docs, ""), docs)
    roles = (draft.get("resume") or {}).get("roles") or []
    while limit and pages > limit:
        spare = [(len(r["bullets"]) - (story_library.role_rules(data, r["role"])["min"] or 1), n, r)
                 for n, r in enumerate(roles)]
        spare = [x for x in spare if x[0] > 0]
        if not spare:
            break
        role = max(spare, key=lambda x: (x[0], x[1]))[2]
        cut.append(str(role["bullets"].pop().get("lib") or ""))
        pages = pages_of(packets.render_resume(cut_of(draft), docs, ""), docs)
    return pages, cut


async def _put_together(session: AsyncSession, owner: str, instance: ProcInstance, reply: dict, step: str,
                        next_step: str) -> dict:
    packet = await _packet(session, instance)
    role, library = await _role_and_library(session, owner, packet)
    docs = await profile_store.owner_documents(session, owner)
    if not isinstance((reply.get("resume") or {}).get("roles"), list) or not isinstance(reply.get("letter"), dict):
        raise seats.ReplyError("the reply has no resume with a roles list, or no letter object")
    problems: list[str] = []
    draft = assemble(reply, library.data or {}, docs, problems)
    settle(draft, library.data or {}, docs)
    if not draft["resume"]["roles"]:
        raise seats.ReplyError("the reply chose no role the master has")
    pages, cut = trim(draft, library.data or {}, docs, resume_page_limit(
        docs, role.target_role if role is not None else None))
    draft |= {"pages": pages, "trimmed": cut, "problems": problems, "library": library.version}
    brief = _brief(packet)
    brief[DRAFT] = draft
    brief[step] = {"thread": draft["thread"], "once_over": reply.get("once_over")}
    brief[STEP] = next_step
    _keep(packet, brief)
    await session.flush()
    return {"pages": pages, "problems": len(problems)}


async def answer_compose(session: AsyncSession, owner: str, instance: ProcInstance, reply: dict) -> dict:
    return await _put_together(session, owner, instance, reply, "composed", "checking")


# --- the gate ---------------------------------------------------------------------------------------------------


def _source(docs: Documents, ids) -> str:
    texts, notes = story_library.master_lines(docs.resume), story_library.notes_of(docs.resume)
    return " ".join(f"{texts.get(i, '')} {notes.get(i, '')}" for i in ids or [])


def new_sentences(letter: list[dict], blocks: list[dict]) -> list[dict]:
    """The letter's sentences written for this letter: none of the chosen blocks' sentences reads like them."""
    src = [str(s.get("text") or "").strip().lower() for b in blocks for s in b.get("sentences") or []]
    out = []
    for n, p in enumerate(letter, 1):
        if re.match(r"^dear[ ,]", p["text"].strip(), re.I) and len(p["text"]) < 80:
            continue
        for s in _SENTENCE.split(p["text"].strip()):
            best = max((difflib.SequenceMatcher(None, s.lower(), x).ratio() for x in src), default=0.0)
            if best < 0.8:
                out.append({"paragraph": n, "sentence": s})
    return out


_NUMBER_WORDS = frozenset("one two three four five six seven eight nine ten eleven twelve twenty thirty forty fifty "
                          "hundred thousand half".split())


def _figures(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", text.lower())
    return set(re.findall(r"\d[\d,.]*", text)) | {w for w in words if w in _NUMBER_WORDS}


_ESTIMATE = re.compile(r"\bestimat", re.I)


def dropped_hedges(where: str, text: str, cites, docs: Documents) -> list[str]:
    """A figure the master hedges keeps its hedge: a line that gives a figure its cited master lines always write with
    "about" or the like (`story_library.dropped_hedges`), or shares a figure with a cited line that calls it an
    estimate without saying so itself, claims a measurement the master never made."""
    texts = story_library.master_lines(docs.resume)
    out = [f"{where} gives {f!r} without the hedge the master keeps on it"
           for f in story_library.dropped_hedges(text, " ".join(texts.get(i, "") for i in cites or []))]
    if _ESTIMATE.search(text):
        return out
    for i in cites or []:
        line = texts.get(i, "")
        if _ESTIMATE.search(line) and _figures(text) & _figures(line):
            out.append(f"{where} gives a figure from {i} without its hedge: the master calls it an estimate")
    return out


def code_findings(draft: dict, data: dict, docs: Documents, target_role: str | None) -> tuple[list[str], list[str]]:
    """The code fact checks on a draft: what fails it (hard), and the flags the facts read decides."""
    cut = cut_of(draft)
    letter = draft.get("letter") or []
    rows = packets.packet_checks(cut, [p["text"] for p in letter], sorted({i for p in letter for i in p["from"]}),
                                 docs, target_role)
    hard = [f"{r['check']}: {p}" for r in rows if r["blocking"] and not r["ok"] for p in r.get("problems") or []]
    flags = [f"{r['check']}: {p}" for r in rows if not r["blocking"] and not r["ok"] for p in r.get("problems") or []]
    names = story_library.protected(data)
    res = draft.get("resume") or {}
    pieces = [("headline", res.get("headline") or ""), ("summary", res.get("summary") or "")]
    pieces += [(f"competencies: {c['area']}", " · ".join(x["term"] for x in c.get("items") or []))
               for c in res.get("competencies") or []]
    for r in res.get("roles") or []:
        role = next((x for x in docs.resume.roles if x.id == r["role"]), None)
        under = role.title if role else r["role"]
        rules = story_library.role_rules(data, r["role"])
        count = len(r.get("bullets") or [])
        if (rules["min"] is not None and count < rules["min"]) or (rules["max"] is not None and count > rules["max"]):
            hard.append(f"{under} has {count} bullets; the owner's rules say {rules['min'] or 0} to "
                        f"{rules['max'] if rules['max'] is not None else 'any'}")
        if rules["scope"] and not r.get("intro"):
            hard.append(f"{under} prints no scope line; the owner's rules say it always does")
        pieces += [(f"scope line under {under}", r.get("intro") or "")]
        pieces += [(f"bullet {n} under {under}", b["text"]) for n, b in enumerate(r.get("bullets") or [], 1)]
    pieces += [(f"letter paragraph {n}", p["text"]) for n, p in enumerate(letter, 1)]
    for where, text in pieces:
        hard += [f"{where} names {n!r}, which the owner's client rule keeps out of writing"
                 for n in story_library.protected_hits(names, text)]
    hard += dropped_hedges("summary", res.get("summary") or "", res.get("summary_from"), docs)
    for r in res.get("roles") or []:
        hard += [h for n, b in enumerate(r.get("bullets") or [], 1)
                 for h in dropped_hedges(f"bullet {n} under {r['role']}", b["text"], b.get("from"), docs)]
    hard += [h for n, p in enumerate(letter, 1)
             for h in dropped_hedges(f"letter paragraph {n}", p["text"], p.get("from"), docs)]
    for n, p in enumerate(letter, 1):
        if re.match(r"^dear[ ,]", p["text"].strip(), re.I) or len(p["text"].split()) < 12:
            continue
        if not p.get("from"):
            flags.append(f"letter paragraph {n} ('{p['text'][:60]}...') cites no master line: check every claim in it")
            continue
        source = _source(docs, p["from"]).lower()
        said = p["text"].lower()
        flags += [f"letter paragraph {n} says {w!r}, which its cited lines do not: check it claims no more cases"
                  for w in UNIVERSAL if re.search(rf"\b{w}\b", said) and not re.search(rf"\b{w}\b", source)]
    if draft.get("problems"):
        flags += [f"assembly: {p}" for p in draft["problems"]]
    return hard, flags


async def action_checks(session, instance, data):
    """The code fact checks on the draft, kept for the facts read beside it (round 1 after the compose, round 2 after
    the fix). The draft is held to the owner's counts and to carried competency terms first (`settle`)."""
    packet = await session.get(Packet, uuid.UUID(data["packet"]))
    if packet is None:
        raise seats.SeatError("the packet this process belongs to isn't there")
    owner = data["owner"]
    docs = await profile_store.owner_documents(session, owner)
    library = await story_library.usable(session, owner)
    role = await session.get(Opportunity, packet.role_id)
    brief = _brief(packet)
    draft = brief.get(DRAFT) or {}
    lib_data = (library.data if library else {}) or {}
    settle(draft, lib_data, docs)
    brief[DRAFT] = draft
    hard, flags = code_findings(draft, lib_data, docs, role.target_role if role is not None else None)
    blocks = [b for b in story_library.usable_blocks(lib_data) if b["id"] in set(draft.get("blocks") or [])]
    new = new_sentences(draft.get("letter") or [], blocks)
    flags += [f"letter paragraph {x['paragraph']}, a sentence written for this letter: \"{x['sentence']}\": check "
              "every "
              "claim in it" for x in new]
    gates = list(brief.get(GATES) or [])
    gates.append({"round": len(gates) + 1, "code_hard": hard, "code_flags": flags})
    brief[GATES], brief[STEP] = gates, "checking"
    _keep(packet, brief)
    await session.flush()
    return {"hard": len(hard)}


def resume_lines(draft: dict, docs: Documents) -> str:
    """The draft's resume, line by line with the master lines each cites, as a facts read and the arbitrator read it."""
    res = draft.get("resume") or {}
    out = [f"headline: {res.get('headline', '')}",
           f"summary (cites {res.get('summary_from')}): {res.get('summary', '')}"]
    out += [f"competencies: {c['area']}: {' · '.join(x['term'] for x in c.get('items') or [])}"
            for c in res.get("competencies") or []]
    out += [f"achievement: {a['text']}   (cites {a.get('from')})" for a in res.get("achievements") or []]
    roles = {r.id: r for r in docs.resume.roles}
    for r in res.get("roles") or []:
        role = roles.get(r["role"])
        out.append(f"\n[{role.title if role else r['role']}, {role.employer if role else ''}]"
                   + (f" descriptor printed by code: {role.descriptor}" if role and role.descriptor else ""))
        if r.get("intro"):
            out.append(f"scope line (cites {r.get('intro_from')}): {r['intro']}")
        out += [f"- {b['text']}   (cites {b.get('from')})" for b in r.get("bullets") or []]
    return "\n".join(out)


def letter_lines(draft: dict) -> str:
    return "\n\n".join(f"[{n}] {p['text']}   (cites {p.get('from')})"
                       for n, p in enumerate(draft.get("letter") or [], 1))


def cited_ids(draft: dict) -> set[str]:
    res = draft.get("resume") or {}
    ids = set(res.get("summary_from") or [])
    ids |= {i for c in res.get("competencies") or [] for x in c.get("items") or [] for i in x.get("backed_by") or []}
    ids |= {i for a in res.get("achievements") or [] for i in a.get("from") or []}
    for r in res.get("roles") or []:
        ids.add(r["role"])
        ids |= set(r.get("intro_from") or [])
        ids |= {i for b in r.get("bullets") or [] for i in b.get("from") or []}
    return ids | {i for p in draft.get("letter") or [] for i in p.get("from") or []}


async def build_facts(session: AsyncSession, owner: str, instance: ProcInstance) -> str:
    packet = await _packet(session, instance)
    docs = await profile_store.owner_documents(session, owner)
    brief = _brief(packet)
    draft = brief.get(DRAFT) or {}
    gate = (brief.get(GATES) or [{}])[-1]
    res = draft.get("resume") or {}
    backing = "\n".join(f"{c['area']}: " + "; ".join(f"{x['term']} (backed by {x.get('backed_by')})"
                                                     for x in c.get("items") or [])
                        for c in res.get("competencies") or [])
    w = docs.writing
    return (seats.section("THE RESUME", resume_lines(draft, docs))
            + seats.section("COMPETENCY BACKING", backing or "(none)")
            + seats.section("THE COVER LETTER", letter_lines(draft))
            + seats.section("MASTER LINES THEY CITE (with the owner's notes)",
                            story_library.lines_with_notes(docs, cited_ids(draft)))
            + seats.section("THE CANDIDATE'S ROLES AND EDUCATION (printed from the master: a claim that rests on these "
                            "is backed)", story_library.roles_and_education(docs))
            + seats.section("THE OWNER'S HEADLINE AND SUMMARY RULES",
                            [r.says for r in [*w.resume_header, *w.resume_summary]] or "(none)")
            + seats.section("THE OWNER'S RULES ON WORDING", rules_for(w, "review"))
            + seats.section("CODE FLAGS (decide each)", "\n".join(gate.get("code_flags") or []) or "(none)"))


async def answer_facts(session: AsyncSession, owner: str, instance: ProcInstance, reply: dict) -> dict:
    packet = await _packet(session, instance)
    if not isinstance(reply.get("failures"), list):
        raise seats.ReplyError("the reply has no failures list")
    brief = _brief(packet)
    gates = list(brief.get(GATES) or [{}])
    facts = [f"facts {f.get('doc', '')} {f.get('check', '')}: \"{f.get('line', '')}\": {f.get('reason', '')}"
             for f in reply["failures"] if isinstance(f, dict)]
    gate = {**gates[-1], "facts": reply.get("failures"), "dismissed": reply.get("dismissed") or [],
            "fact_fails": facts}
    gate["fails"] = [*(gate.get("code_hard") or []), *facts]
    gates[-1] = gate
    brief[GATES], brief[TODO] = gates, gate["fails"]
    brief[STEP] = "fixing" if gate["fails"] else "checking"
    _keep(packet, brief)
    await session.flush()
    return {"fails": len(gate["fails"]), "facts": len(facts)}


# --- fixes and the arbitrator -------------------------------------------------------------------------------------


async def build_fix(session: AsyncSession, owner: str, instance: ProcInstance) -> str:
    packet = await _packet(session, instance)
    role = await session.get(Opportunity, packet.role_id)
    docs = await profile_store.owner_documents(session, owner)
    brief = _brief(packet)
    draft = brief.get(DRAFT) or {}
    current = {"resume": {k: v for k, v in (draft.get("resume") or {}).items()},
               "letter": {"paragraphs": draft.get("letter") or []}}
    return (seats.section("THE CURRENT RESUME", current["resume"])
            + seats.section("THE CURRENT LETTER", current["letter"])
            + seats.section("FIX THESE, AND CHANGE ONLY THESE LINES", "\n".join(brief.get(TODO) or []))
            + seats.section("MASTER LINES THEY CITE (with the owner's notes)",
                            story_library.lines_with_notes(docs, cited_ids(draft)))
            + seats.section("THE CANDIDATE'S ROLES AND EDUCATION", story_library.roles_and_education(docs))
            + seats.section("THE OWNER'S RULES", _owner_rules(docs, await _target(session, owner, role))))


def _fixed_draft(draft: dict, reply: dict, docs: Documents) -> dict:
    """The draft with the fix's resume and letter put in place, keeping what the fix left out."""
    res = reply.get("resume")
    letter = (reply.get("letter") or {}).get("paragraphs") if isinstance(reply.get("letter"), dict) else None
    if not isinstance(res, dict) and letter is None:
        raise seats.ReplyError("the reply has neither a resume nor a letter")
    out = json.loads(json.dumps(draft))
    known = {r.id for r in docs.resume.roles}
    if isinstance(res, dict):
        merged = {**(out.get("resume") or {}), **{k: v for k, v in res.items() if k in (
            "headline", "summary", "summary_from", "competencies", "achievements", "roles")}}
        merged["roles"] = [r for r in merged.get("roles") or [] if isinstance(r, dict) and r.get("role") in known]
        for r in merged["roles"]:
            r["bullets"] = [{"text": str(b.get("text") or "").strip(), "from": _ids(b.get("from")),
                             **({"lib": b["lib"]} if b.get("lib") else {})}
                            for b in r.get("bullets") or [] if isinstance(b, dict) and str(b.get("text") or "").strip()]
        merged["competencies"] = [
            {"area": str(c.get("area") or ""), "items": [x if isinstance(x, dict) else {"term": str(x), "backed_by": []}
                                                         for x in c.get("items") or []]}
            for c in merged.get("competencies") or [] if isinstance(c, dict)]
        if not merged["roles"]:
            raise seats.ReplyError("the fixed resume has no role the master has")
        out["resume"] = merged
    if letter is not None:
        out["letter"] = [{"text": str(p.get("text") if isinstance(p, dict) else p).strip(),
                          "from": _ids(p.get("from")) if isinstance(p, dict) else []}
                         for p in letter if str(p.get("text") if isinstance(p, dict) else p).strip()]
    return out


async def answer_fix(session: AsyncSession, owner: str, instance: ProcInstance, reply: dict) -> dict:
    packet = await _packet(session, instance)
    docs = await profile_store.owner_documents(session, owner)
    brief = _brief(packet)
    brief[DRAFT] = _fixed_draft(brief.get(DRAFT) or {}, reply, docs)
    brief[STEP] = "checking"
    brief.setdefault("fixes", []).append(list(brief.get(TODO) or []))
    _keep(packet, brief)
    await session.flush()
    return {"fixed": True}


async def build_arbitrate(session: AsyncSession, owner: str, instance: ProcInstance) -> str:
    packet = await _packet(session, instance)
    role = await session.get(Opportunity, packet.role_id)
    docs = await profile_store.owner_documents(session, owner)
    brief = _brief(packet)
    draft = brief.get(DRAFT) or {}
    findings, n = [], 0
    for gate in brief.get(GATES) or []:
        for f in gate.get("code_hard") or []:
            n += 1
            findings.append(f"F{n} (round {gate['round']}, CODE): {f}")
        for f in gate.get("fact_fails") or []:
            n += 1
            findings.append(f"F{n} (round {gate['round']}): {f}")
    return (seats.section("THE POSTING", await _posting(session, role) or "(no posting text is held)")
            + seats.section("THE THREAD", draft.get("thread") or "")
            + seats.section("THE RESUME NOW", resume_lines(draft, docs))
            + seats.section("THE COVER LETTER NOW", letter_lines(draft))
            + seats.section("FINDINGS, ALL ROUNDS", "\n".join(findings) or "(none)")
            + seats.section("MASTER LINES THE DOCUMENTS CITE (with the owner's notes)",
                            story_library.lines_with_notes(docs, cited_ids(draft)))
            + seats.section("THE CANDIDATE'S ROLES AND EDUCATION", story_library.roles_and_education(docs))
            + seats.section("THE OWNER'S RULES ON WORDING", rules_for(docs.writing, "review")))


async def answer_arbitrate(session: AsyncSession, owner: str, instance: ProcInstance, reply: dict) -> dict:
    packet = await _packet(session, instance)
    if not isinstance(reply.get("rulings"), list):
        raise seats.ReplyError("the reply has no rulings list")
    fixes = [f"{f.get('doc', '')}: \"{f.get('line', '')}\": change to / do: {f.get('change', '')}"
             for f in reply.get("fix_list") or [] if isinstance(f, dict) and f.get("change")]
    brief = _brief(packet)
    brief[RULING] = {"rulings": reply["rulings"], "fix_list": reply.get("fix_list") or []}
    brief[TODO], brief[STEP] = fixes, "fixing"
    _keep(packet, brief)
    await session.flush()
    return {"fixes": len(fixes)}


# --- filing -------------------------------------------------------------------------------------------------------


def _gate_row(brief: dict) -> dict:
    """What the gate did, as a check row that never blocks."""
    gates = brief.get(GATES) or []
    ruling = brief.get(RULING) or {}
    said = [f"read {len(gates)} time{'s' if len(gates) != 1 else ''}"]
    if ruling:
        real = sum(1 for r in ruling.get("rulings") or [] if isinstance(r, dict) and r.get("real"))
        said.append(f"the arbitrator ruled {real} of {len(ruling.get('rulings') or [])} findings real and fixed "
                    f"{len(ruling.get('fix_list') or [])}")
    return packets.row("The facts gate", [], blocking=False, passed="; ".join(said))


async def action_file(session, instance, data):
    """The final packet: the app's checks, the pay ask from the role's research, the resume as it prints; then the
    process files it ready or holds it."""
    owner = data["owner"]
    packet = await session.get(Packet, uuid.UUID(data["packet"]))
    if packet is None:
        raise seats.SeatError("the packet this process belongs to isn't there")
    docs = await profile_store.owner_documents(session, owner)
    profile = await profile_store.current(session, owner)
    role = await session.get(Opportunity, packet.role_id)
    brief = _brief(packet)
    draft = brief.get(DRAFT) or {}
    library = await story_library.usable(session, owner)
    settle(draft, (library.data if library else {}) or {}, docs)  # the final pass may have added lines or terms
    brief[DRAFT] = draft
    cut = cut_of(draft)
    letter = draft.get("letter") or []
    letter_doc = packets.Letter.model_validate({"paragraphs": [p["text"] for p in letter],
                                                "from": sorted({i for p in letter for i in p.get("from") or []})})
    fixes = packets.mechanical_fixes(cut, letter_doc, docs)
    if not cut.education:
        cut.education = packets.master_education(docs)
    target = role.target_role if role is not None else None
    # A fractional or part-time role paid by the hour has no yearly ask, and no check of one (docs/PRD.md 30.4).
    hourly = role is not None and hourly_part_time(role.title, role.comp)
    inputs = None if hourly else comp.from_research(role.analysis if role is not None else None)
    checks = packets.packet_checks(cut, letter_doc.paragraphs, letter_doc.from_, docs, target,
                                   profile=None if hourly else profile, comp_inputs=inputs)
    worked = packets.worked_ask(inputs, profile, target)
    checks.append(_gate_row(brief))
    packet.resume = packets.render_resume(cut, docs, packets.header_location(role, docs.about, profile))
    packet.letter = letter_doc.model_dump(mode="json", by_alias=True)
    packet.comp = worked or ({"inputs": inputs.model_dump(mode="json", by_alias=True)} if inputs else {})
    packet.reasoning = {"problem": draft.get("thread") or "", "headline": "", "chosen": [], "left_out": [],
                        "letter": "", "for_owner": []}
    packet.checks = packets.stored(checks)
    packet.written_at = datetime.now(UTC)
    brief[STEP] = "done"
    if fixes:
        brief["fixed_by_the_app"] = fixes
    _keep(packet, brief)
    if role is not None and not role.description:
        await posting_text.take(session, role, posting_text.FromPostings(), now=packet.written_at)
    await session.flush()
    failed = packets.failures(checks)
    return {"ok": not failed, "problems": len(failed)}


async def fail(session: AsyncSession, owner: str, instance: ProcInstance, why: str) -> None:
    """A seat failed every try: the packet is closed as failed, so it can be asked for again. No notification is
    raised: the role's packet says it failed and offers Build again (the owner, 2026-10-24, docs/PRD.md 45)."""
    packet = await session.get(Packet, uuid.UUID(str((instance.data or {}).get("packet"))))
    await stuck.cancel_instance(session, instance.id, why[:200], f"packet {(instance.data or {}).get('packet')}")
    if packet is None or packet.status != PacketState.WRITING:
        return
    packet.status = PacketState.FAILED
    await session.flush()


# --- registering ------------------------------------------------------------------------------------------------


async def _library_ready(session: AsyncSession, owner: str, instance: ProcInstance) -> bool:
    return await story_library.usable(session, owner) is not None


async def _owners(session: AsyncSession, owner: str, instance: ProcInstance) -> bool:
    packet = await session.get(Packet, uuid.UUID(str((instance.data or {}).get("packet"))))
    return packet is not None and (packets.launched(packet) or await packets.owner_asked(session, owner, packet))


async def _launched(session: AsyncSession, owner: str, instance: ProcInstance) -> bool:
    packet = await session.get(Packet, uuid.UUID(str((instance.data or {}).get("packet"))))
    return packet is not None and packets.launched(packet)


for _element, _model, _label, _build, _answer, _effort, _ready in (
        (COMPOSE, seats.OPUS, "Composing a packet", build_compose, answer_compose, None, _library_ready),
        (FACTS_1, seats.SONNET, "Checking a packet's facts", build_facts, answer_facts, None, None),
        (FIX, seats.SONNET, "Fixing a packet", build_fix, answer_fix, "low", None),
        (FACTS_2, seats.SONNET, "Checking a packet's facts again", build_facts, answer_facts, None, None),
        (ARBITRATE, seats.OPUS, "Ruling on a packet's findings", build_arbitrate, answer_arbitrate, None, None),
        (FINAL_FIX, seats.SONNET, "Making a packet's final fixes", build_fix, answer_fix, "low", None)):
    seats.register(seats.Seat(process=process.PACKET, element=_element, model=_model, label=_label, build=_build,
                              answer=_answer, effort=_effort, ready=_ready, owners=_owners, launched=_launched))
seats.on_fail(process.PACKET, fail)

_SOURCE = "backend/app/jobsearch/writing/packet_writer.py"
registry.register("jobs.packet_checks", action_checks, inputs=["owner", "packet"], outputs=["hard"],
                  description="Runs the code fact checks on the packet's draft, for the facts read beside them.",
                  source=_SOURCE)
registry.register("jobs.packet_file", action_file, inputs=["owner", "packet"], outputs=["ok", "problems"],
                  description="Runs the app's checks on the final packet, works out the pay ask and renders the "
                              "resume.",
                  source=_SOURCE)
