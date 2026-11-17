"""Employer signals from public records (docs/PRD.md 28.12): state layoff notices (WARN), matched to the employers the
app knows by name, each kept as a dated "layoff" event with where it came from, so the "recent layoffs" fact the gates
and modifiers read comes from a record, not a guess.

The notices come from WARN Feed's daily, normalised copy of 48 states' published notices (CC BY 4.0: each event names
it). A night reads the recent notices; `load_warn(full=True)` reads the whole archive once, to fill the past.
Matching is by the employer's matching key against the notice's company name, exact only: a near name is not a match."""

import csv
import io
from datetime import date

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch.employers.core import key_of
from app.jobsearch.fetch.polite import Fetcher, Refused
from app.jobsearch.models import Employer
from app.timeutil import owner_today, utc_now

WARN_RECENT = "https://approjects-warn-act-notices.static.hf.space/data/latest.csv"
WARN_ARCHIVE = "https://approjects-warn-act-notices.static.hf.space/data/warn_notices.json"
WARN_SOURCE = "WARN notice, via WARN Feed (CC BY 4.0)"
#: Notices older than this many years are not kept: the layoff facts look back about two years.
KEEP_YEARS = 3


def _rows(reply_text: str, is_json: bool) -> list[dict]:
    if is_json:
        import json

        data = json.loads(reply_text)
        return data if isinstance(data, list) else (data.get("notices") or data.get("data") or [])
    return list(csv.DictReader(io.StringIO(reply_text)))


def event_for(notice: dict) -> dict | None:
    """The layoff event a notice becomes, or None when it has no date or is too old to matter."""
    when = str(notice.get("notice_date") or notice.get("effective_date") or "")[:10]
    try:
        day = date.fromisoformat(when)
    except ValueError:
        return None
    if day.year < owner_today().year - KEEP_YEARS:
        return None
    place = ", ".join(p for p in (str(notice.get("location") or "").strip(), str(notice.get("state") or "").strip())
                      if p)
    affected = str(notice.get("employees_affected") or "").strip()
    note = "; ".join(p for p in (f"{affected} affected" if affected else "", place,
                                 str(notice.get("notice_type") or "").strip()) if p)
    return {"kind": "layoff", "date": day.isoformat()[:7], "note": note or None, "source": WARN_SOURCE,
            "ref": f"warn:{notice.get('id') or (notice.get('company'), when, place)}"}


def match(notices: list[dict], employers: list[Employer]) -> dict:
    """Which notices belong to which employers, by exact matching key on the notice's names."""
    by_key: dict[str, Employer] = {}
    for e in employers:
        for k in {e.key, *(key_of(a) for a in e.aliases or [])}:
            if k:
                by_key.setdefault(k, e)
    found: dict = {}
    for n in notices:
        names = {key_of(n.get(f)) for f in ("company_canonical", "company", "company_dba") if n.get(f)}
        employer = next((by_key[k] for k in names if k in by_key), None)
        if employer is not None:
            found.setdefault(employer.id, (employer, []))[1].append(n)
    return found


async def load_warn(session: AsyncSession, owner: str, fetch: Fetcher, full: bool = False) -> dict:
    """Read the notices and add each matching one to its employer as a layoff event, once. Returns how many notices
    were read, how many employers gained events, and how many events were added."""
    url = WARN_ARCHIVE if full else WARN_RECENT
    await db.release_if_free(session)  # the notices are read with no transaction open; the employers are read after
    try:
        reply = await fetch.get(url)
    except (Refused, httpx.HTTPError) as exc:
        return {"read": 0, "employers": 0, "added": 0, "error": str(exc)}
    if reply.status_code != 200:
        return {"read": 0, "employers": 0, "added": 0, "error": f"the notices answered {reply.status_code}"}
    try:
        notices = _rows(reply.text, full)
    except ValueError as exc:
        return {"read": 0, "employers": 0, "added": 0, "error": f"the notices changed shape: {exc}"}
    employers = list((await session.scalars(select(Employer).where(Employer.owner == owner))).all())
    touched = added = 0
    for employer, theirs in match(notices, employers).values():
        if "events" in (employer.owner_fields or []):
            continue  # the owner set this employer's events by hand
        have = {e.get("ref") for e in employer.events or []}
        new = [ev for ev in (event_for(n) for n in theirs) if ev and ev["ref"] not in have]
        if not new:
            continue
        employer.events = sorted([*(employer.events or []), *new], key=lambda e: str(e.get("date") or ""))
        employer.sources = {**(employer.sources or {}),
                            "events": {"source": WARN_SOURCE, "at": utc_now().isoformat(), "by": "app"}}
        touched += 1
        added += len(new)
    await session.flush()
    return {"read": len(notices), "employers": touched, "added": added}
