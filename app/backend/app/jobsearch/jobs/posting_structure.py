"""A posting's structured record, the part a model fills, in shadow:
nothing reads it (the owner, 2026-10-20). For each version of a posting's text, one small Haiku session of the `parser`
kind (the structure mode of the job `jobs/parse.py` keeps) reads the text once and fills what code cannot: the
requirement, duty and preferred items, the keywords, certifications, clearance, degree level, seniority level and the
years asked for each area.

What makes the answer a record and not a summary:
- every item and field carries a `quote` that must be in the posting's text (`posting_facts.quote_in`); one that is not
  is refused, every problem named at once so the session can send it again;
- the numbers of the items are made here, never by the model: an item is numbered by where its quote stands in the
  text, so the same text numbers the same lines the same way; a rerun keeps the number an item had (matched by its
  quote and words) and numbers a new item after them;
- the fields code fills from step 2 (pay, years, degree word, work mode, travel, and the title from the listing) stay
  code: the model's answer only adds fields the code left empty;
- a snippet (a listing's preview, no whole text) makes a `partial` record, and a partial record never fails a gate.

A text version is the text the app holds of a role (`posting_text.held`), or, for a role holding none, the fullest
snippet a posting of it has (marked partial). The record is keyed by the role and the hash of that text, so a changed
text is a new version and clearing an analysis loses nothing. Nothing here names a person, an employer or a term.
"""

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobsearch.gating import posting_facts as pf
from app.jobsearch.gating import structured_fill
from app.jobsearch.models import Opportunity, ParseJob, StructuredRecord
from app.jobsearch.postings import role_postings
from app.jobsearch.postings import text as posting_text
from app.jobsearch.postings.record import Field, Record
from app.jobsearch.states import ParseKind, ParseState

RULES_VERSION = 1
#: The numbered lists: `{n, text, quote}` each.
ITEM_LISTS = ("requirements", "duties", "preferred")
#: A degree level, as `structured_fill` names them.
DEGREE_LEVELS = tuple(name for name, _ in structured_fill._DEGREES)
IMPORTANCE = ("required", "preferred")
FIELDS = (*ITEM_LISTS, "keywords", "certifications", "areas", "clearance", "degree", "level")

HOW = (
    "This is one job posting's text. Read it once and send what it states with jobs.parse_report: {id, fields} in the "
    "shape sent with it. Every item and field needs a `quote`: a line or phrase copied word for word from the posting. "
    "An item whose quote is not in the posting is refused. Do not number anything: the app numbers the items. Send "
    "`requirements` (one item for each thing the candidate must have), `duties` (one for each thing the job does), "
    "`preferred` (one for each nice-to-have), each as {text, quote}; `keywords` as {term, importance, quote} with "
    "importance required or preferred; `certifications` as {name, required, quote}; `areas` as {area, years, quote} "
    "for each area of experience that states a number of years; and, only where the posting states them, `clearance`, "
    "`degree` (one of " + ", ".join(DEGREE_LEVELS) + ") and `level` (the seniority), each as {value, quote}. Leave "
    "out whatever the posting does not state; never guess and never invent. Then call jobs.parse_next again."
)


class StructureError(ValueError):
    """A report that breaks the rules."""


def fingerprint(text: str) -> str:
    """The hash of a text version: what makes a record current."""
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


# --- what the session answers ---------------------------------------------------------------------------------------


def _where(norm_text: str, quote: str) -> int:
    at = norm_text.find(pf._norm(quote).strip(" .\"'"))
    return at if at >= 0 else len(norm_text)


def _key(*parts: str) -> str:
    return "|".join(pf._norm(p).strip(" .\"'").lower() for p in parts)


def _quote(raw: dict, where: str, text: str, problems: list[str]) -> str | None:
    quote = raw.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        problems.append(f"{where} has no quote: copy the line it rests on")
        return None
    if not pf.quote_in(text, quote):
        problems.append(f"{where}: its quote is not in the posting")
        return None
    return " ".join(quote.split())[:pf.QUOTE_CHARS]


def _str(raw: dict, key: str, where: str, problems: list[str]) -> str | None:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{where} needs `{key}` as text")
        return None
    return value.strip()


def _items(raw: object, name: str, text: str, problems: list[str]) -> list[tuple[dict, str]]:
    """The dicts of a list the session sent, each with its checked quote; the ones that fail are named in `problems`."""
    if not isinstance(raw, list):
        problems.append(f"{name} must be a list")
        return []
    out = []
    for i, item in enumerate(raw, 1):
        where = f"{name} item {i}"
        if not isinstance(item, dict):
            problems.append(f"{where} must be an object")
            continue
        quote = _quote(item, where, text, problems)
        if quote is not None:
            out.append((item, quote))
    return out


def _number(found: list[dict], name: str, text: str, previous: dict | None) -> list[dict]:
    """Numbers made by code: an item keeps the number it had (matched by its quote and words), a new one is numbered
    after the highest in the order its quote stands in the text, so the same text always numbers the same way."""
    norm = pf._norm(text)
    had = {_key(i.get("quote", ""), i.get("text", "")): int(i["n"])
           for i in ((previous or {}).get(name) or []) if isinstance(i, dict) and i.get("n")}
    top = max(had.values(), default=0)
    seen: set[str] = set()
    items: list[dict] = []
    for item in sorted(found, key=lambda i: (_where(norm, i["quote"]), i["text"].lower())):
        key = _key(item["quote"], item["text"])
        if key in seen:
            continue
        seen.add(key)
        if key not in had:
            top += 1
        items.append({"n": had.get(key, top), **item})
    return sorted(items, key=lambda i: i["n"])


def check(raw: object, text: str, previous: dict | None = None) -> tuple[dict, list[str]]:
    """The session's fields tidied: every item with a quote that is in the text, items numbered. `previous` is the
    cleaned fields of an earlier reading of the same text, whose numbers are kept. Returns `(clean, problems)`: an item
    that fails is left out and named, every problem at once."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        return {}, ["fields must be an object: {" + ", ".join(FIELDS) + "}"]
    if unknown := [str(k) for k in raw if k not in FIELDS]:
        problems.append(f"{', '.join(unknown)}: not a field; the fields are {', '.join(FIELDS)}")
    clean: dict = {}
    for name in ITEM_LISTS:
        found = []
        for item, quote in _items(raw.get(name, []), name, text, problems):
            words = _str(item, "text", f"{name} item", problems)
            if words:
                found.append({"text": words, "quote": quote})
        if found:
            clean[name] = _number(found, name, text, previous)
    keywords = []
    for item, quote in _items(raw.get("keywords", []), "keywords", text, problems):
        term = _str(item, "term", "a keyword", problems)
        importance = item.get("importance")
        if term and importance in IMPORTANCE:
            keywords.append({"name": term, "importance": importance, "quote": quote})
        elif term:
            problems.append(f"keyword {term}: importance must be one of {', '.join(IMPORTANCE)}")
    certs = []
    for item, quote in _items(raw.get("certifications", []), "certifications", text, problems):
        if name := _str(item, "name", "a certification", problems):
            certs.append({"name": name, "required": bool(item.get("required")), "quote": quote})
    areas = []
    for item, quote in _items(raw.get("areas", []), "areas", text, problems):
        area, years = _str(item, "area", "an area", problems), item.get("years")
        if area and isinstance(years, (int, float)) and not isinstance(years, bool) and years > 0:
            areas.append({"name": area, "years": years, "quote": quote})
        elif area:
            problems.append(f"area {area}: years must be a number")
    for name, found in (("stack", keywords), ("certifications", certs), ("areas", areas)):
        deduped = list({_key(i["name"]): i for i in found}.values())
        if deduped:
            clean[name] = deduped
    for name in ("clearance", "degree", "level"):
        if raw.get(name) is None:
            continue
        one = raw[name]
        if not isinstance(one, dict):
            problems.append(f"{name} must be {{value, quote}}")
            continue
        quote, value = _quote(one, name, text, problems), _str(one, "value", name, problems)
        if name == "degree" and value and value.lower() not in DEGREE_LEVELS:
            problems.append(f"degree must be one of {', '.join(DEGREE_LEVELS)}")
        elif quote and value:
            clean[name] = {"value": value.lower() if name == "degree" else value, "quote": quote}
    return clean, problems


def is_empty(clean: dict) -> bool:
    return not any(clean.values())


def build(text: str, clean: dict, listing_title: str | None = None, partial: bool = False) -> Record:
    """The posting's record: the fields code fills from the text (`structured_fill.fill`, source `code`, the title
    `listing`) stay as they are, and the model's checked fields are added only where the code left a field empty."""
    fields = dict(structured_fill.fill(text, listing_title, partial).fields)

    def put(name: str, value: object, quote: str) -> None:
        if name not in fields and value:
            fields[name] = Field(value, pf.HAIKU, quote)

    for name in ITEM_LISTS:
        if items := clean.get(name):
            put(name, items, items[0]["quote"])
    for name in ("stack", "certifications", "areas"):
        if items := clean.get(name):
            put(name, items, items[0]["quote"])
    for name in ("clearance", "degree", "level"):
        if one := clean.get(name):
            put(name, one["value"], one["quote"])
    return Record(fields, partial)


# --- the text versions, the jobs and the records ---------------------------------------------------------------------


async def text_version(session: AsyncSession, role: Opportunity) -> tuple[str, bool]:
    """The text to structure for a role and whether it is partial: the text the app holds (whole), else the fullest
    snippet a posting of the role has (partial), else nothing."""
    whole = (await posting_text.held(session, role)).text.strip()
    if whole:
        return whole, False
    snippets = [t for t in ((p.text or "").strip() for p in await role_postings.of_role(session, role)) if t]
    return (max(snippets, key=len), True) if snippets else ("", False)


async def stored(session: AsyncSession, owner: str, role_id: uuid.UUID, fp: str) -> StructuredRecord | None:
    """The record kept for this role's text version, if any."""
    return await session.scalar(select(StructuredRecord).where(
        StructuredRecord.owner == owner, StructuredRecord.kind == "posting", StructuredRecord.role_id == role_id,
        StructuredRecord.fingerprint == fp))


async def keep(session: AsyncSession, owner: str, role_id: uuid.UUID, fp: str, record: Record) -> StructuredRecord:
    """Store the record of a text version, replacing the one it has (one for each role and text)."""
    row = await stored(session, owner, role_id, fp)
    if row is None:
        row = StructuredRecord(owner=owner, kind="posting", role_id=role_id, fingerprint=fp)
        session.add(row)
    data = record.as_dict()
    row.fields, row.partial, row.rules_version = data["fields"], record.partial, RULES_VERSION
    await session.flush()
    return row


async def _open(session: AsyncSession, owner: str, role_id: uuid.UUID) -> bool:
    return await session.scalar(select(ParseJob.id).where(
        ParseJob.owner == owner, ParseJob.kind == ParseKind.STRUCTURE, ParseJob.role_id == str(role_id),
        ParseJob.state.in_((ParseState.WAITING, ParseState.READING))).limit(1)) is not None


async def ask(session: AsyncSession, owner: str, role_id: uuid.UUID) -> ParseJob | None:
    """A waiting job for the role's text as it is now, whether or not it has a record (a rerun of the same text keeps
    the numbers it gave). None for a role with no text. It waits for a free session of the pool, however long."""
    role = await session.get(Opportunity, role_id)
    if role is None or role.owner != owner:
        return None
    text, partial = await text_version(session, role)
    if not text:
        return None
    job = ParseJob(owner=owner, kind=ParseKind.STRUCTURE, role_id=str(role.id), text=text, state=ParseState.WAITING,
                   fields={"partial": partial})
    session.add(job)
    await session.flush()
    return job


async def queue(session: AsyncSession, owner: str, n: int = 50, *, dry_run: bool = False) -> int:
    """Ask for up to `n` roles' text versions that have no current record and no job open (newest roles first).
    `dry_run` only counts. Returns how many."""
    ids = (await session.scalars(select(Opportunity.id).where(Opportunity.owner == owner).order_by(
        Opportunity.found.desc().nulls_last(), Opportunity.id))).all()
    count = 0
    for role_id in ids:
        if count >= n:
            break
        role = await session.get(Opportunity, role_id)
        if role is None or await _open(session, owner, role_id):
            continue
        text, _ = await text_version(session, role)
        if not text:
            continue
        current = await stored(session, owner, role_id, fingerprint(text))
        if current is not None and current.rules_version == RULES_VERSION:
            continue
        if not dry_run:
            await ask(session, owner, role_id)
        count += 1
    return count


async def previous_fields(session: AsyncSession, owner: str, role_id: uuid.UUID, fp: str) -> dict | None:
    """The numbered items an earlier reading of this same text kept, for `check` to keep their numbers."""
    row = await stored(session, owner, role_id, fp)
    if row is None:
        return None
    return {name: (row.fields.get(name) or {}).get("value") or [] for name in ITEM_LISTS}
