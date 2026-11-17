"""The owner's tracker spreadsheet, in and out.

`COLUMNS` is the tracker's own 25 columns in its own order (resumebot's `scripts/tracker_io.py`), the order the
export writes and the import expects. A role is stored under `gates.match_key(company, title)`, the key the scout
makes, never under the tracker's own `matchKey` column (which can be stale or hand-edited); that column is read only to
find a role an earlier import stored under it. The import reads only the `Tracker` sheet and never writes the file.

The import can be run again. A role the app has not done anything with since it came in (it was never researched,
given a packet, or moved by the owner or an agent) is brought up to date with the file; one it has, and one the
app's own search found, stays as the app has it. Each role the import stores also gets a posting in the log, with the
import as its source (`role_postings.py`, docs/PRD.md 39.9), so reading the file again, which stores none, adds none.
The tracker's words are read into the app's, not copied:

- its `moveType` is the older plugin's own tier for a role (remote, local, relocation, promotion ...), and the app's
  `move_type` is the way of working that the gates read, so only a word that is a way of working goes in;
- its `fit` is on the older plugin's own scale, which the app does not use, so the app's fit stays empty until the app
scores
  the role, and the older plugin's figure is kept in `fit_evidence`, where the role's page and the analyst read it;
- its `rejected` is the employer's no and the older plugin's own gates alike: a row that says it was screened out by a
gate
  (the older plugin's own words, `plugin_screen`) comes in as Gated out with that reason, and a no the employer sent
  (`[email-sync ...] rejection`) or one nothing explains stays Rejected;
- its `comp` is posted pay or the older plugin's own estimate, told apart by `compSource`: an estimate is kept in
  `fit_evidence` and `comp` stays empty, since the pay gate reads `comp` as pay the posting shows.

The export is one workbook: a `Tracker` sheet (every role that wasn't screened out, tracker columns in tracker order)
and a `ScreenedOut` sheet (the roles a gate dropped and why), so the owner's tracker can take the first as it is and
the second shows whether a gate misfires.
"""

import asyncio
import io
import re
import uuid
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.errors import DomainError
from app.jobsearch import stages
from app.jobsearch.measures.pay_place import WORK_MODES, match_key, parse_comp_text
from app.jobsearch.models import ORIGIN_TRACKER, STAGE_MOVES_AT, Opportunity, Packet, StageMove
from app.jobsearch.postings import role_postings
from app.jobsearch.states import StopGate, StopStep
from app.jobsearch.work import queue
from app.models.work import TrailEvent
from app.timeutil import utc_now
from app.trail import service as trail

SHEET = "Tracker"
SCREENED_SHEET = "ScreenedOut"

COLUMNS = [
    "id", "company", "title", "status", "fit", "level", "effort", "variant",
    "comp", "compSource", "location", "moveType", "sector", "found", "board",
    "url", "applyUrl", "ats", "notes", "packetComplete", "queueRank",
    "matchKey", "fitEvidence", "colAdjusted", "colAdjustedComp",
]

#: The tracker's column -> the `Opportunity` attribute that holds it.
ATTR = {
    "id": "slug", "company": "company", "title": "title", "status": "status", "fit": "fit", "level": "level",
    "effort": "effort", "variant": "variant", "comp": "comp", "compSource": "comp_source", "location": "location",
    "moveType": "move_type", "sector": "sector", "found": "found", "board": "board", "url": "url",
    "applyUrl": "apply_url", "ats": "ats", "notes": "notes", "packetComplete": "packet_complete",
    "queueRank": "queue_rank", "matchKey": "match_key", "fitEvidence": "fit_evidence", "colAdjusted": "col_adjusted",
    "colAdjustedComp": "col_adjusted_comp",
}
SCREENED_COLUMNS = ["company", "title", "location", "comp", "url", "board", "found", "reason"]


class TrackerError(DomainError):
    """The file isn't a tracker this app can read (plain words)."""


def slugify(company: str, title: str) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", f"{company} {title}".lower()).strip("-")
    return words[:120] or "role"


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().upper() in ("TRUE", "1", "YES")


def _int(value) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10]) if value else None
    except ValueError:
        return None


def read_tracker(source) -> list[dict]:
    """The tracker's rows as dicts keyed by the tracker's column names. `source` is a path or a file object. Refuses
    a file whose first sheet row isn't the tracker's header (it never guesses at another shape)."""
    try:
        import openpyxl

        workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises many things for a file that isn't a workbook
        raise TrackerError(f"that isn't a spreadsheet this app can read ({type(exc).__name__})") from exc
    try:
        if SHEET not in workbook.sheetnames:
            raise TrackerError(f"the workbook has no sheet named '{SHEET}'")
        rows = workbook[SHEET].iter_rows(values_only=True)
        header = [(_text(c) or "") for c in next(rows, ())]
        if header[: len(COLUMNS)] != COLUMNS:
            raise TrackerError("the tracker's header row isn't the 25 columns this app expects, in order")
        return [
            {c: (row[i] if i < len(row) else None) for i, c in enumerate(COLUMNS)}
            for row in rows if any(v is not None for v in row)
        ]
    finally:
        workbook.close()


# --- reading a row into the app's words -------------------------------------------------------------------------


def work_mode(word) -> str | None:
    """The way of working a tracker's `moveType` names, or None. The tracker's word is the older plugin's own tier for
    the role, and most of its words (local, relocation, unverified, promotion, lateral) are not a way of working; the
    app's `move_type` is read by the gates as the way of working, one of `WORK_MODES` as the scout reports it."""
    text = (_text(word) or "").lower()
    return text if text in WORK_MODES else None


#: A `fit` cell of the old tracker that says it screened the role out: "REJECTED - trajectory gate".
_FIT_REJECTED = re.compile(r"^\s*REJECTED\b[\s:–—-]*(?P<why>.*)$", re.IGNORECASE)
#: An email the employer sent, which the older plugin's email-sync wrote into the row's notes: a no that was the
#: employer's.
_EMPLOYER_SAID_NO = re.compile(r"\[email-sync[^\]]*\]\s*\[?rejection\b", re.IGNORECASE)
#: How the older plugin's own runs wrote down that one of its gates screened a role out, in notes: the `rejected: <x>
#: gate` of the nightly build, `gate-fail` and `gate-rejected`, `status->rejected: <why>`, "moved to rejected" with a
#: hard gate, and a same-company dedupe. `why` is the older plugin's words for it, when it gave some. A row that has
#: none of these, and no email from the employer, is not told apart from the employer's no (it stays Rejected).
_SCREEN_NOTES = (
    (re.compile(r"\brejected\s*(?::|--|—|-)\s*(?P<why>[a-z][a-z /-]{2,40}?\bgate\b[^|]*)", re.IGNORECASE), "a gate"),
    (re.compile(r"\bgate-(?:rejected|fail)\b[: ]*(?P<why>[^|]*)", re.IGNORECASE), "a gate"),
    (re.compile(r"\bstatus\s*->\s*rejected\s*:\s*(?P<why>[^|]*)", re.IGNORECASE), "a gate"),
    (re.compile(r"\bmoved\b[^.|]{0,40}?(?:to|->)\s*rejected\b(?P<why>)", re.IGNORECASE), "a hard gate"),
    (re.compile(r"(?P<why>rejected on same-company dedupe[^|]*|same-company dedupe:\s*superseded[^|]*)",
                re.IGNORECASE), "same-company dedupe"),
)
#: The app's gate a plugin reason is about, by its words; any other is `other`, which teaches the classifier nothing.
_SCREEN_GATES = (
    (re.compile(r"\b(?:comp|pay|salary|floor|ceiling)\b", re.IGNORECASE), StopGate.COMPENSATION),
    (re.compile(r"\b(?:location|relocation|hybrid|on-?site|office)\b", re.IGNORECASE), StopGate.LOCATION),
    (re.compile(r"\b(?:stale|expired|reposted)\b", re.IGNORECASE), StopGate.AGE),
    (re.compile(r"\bexclusion\b|\bexcluded employer\b", re.IGNORECASE), StopGate.EMPLOYER),
)
WHY_CHARS = 100


def _excerpt(text: str | None, limit: int = WHY_CHARS) -> str:
    """The first words of the older plugin's reason, cut at a word."""
    words = " ".join((text or "").split()).strip(" .:;|-–—")
    return words if len(words) <= limit else words[:limit].rsplit(" ", 1)[0].rstrip(" ,;:(") + "..."


def plugin_screen(raw: dict, status: str) -> tuple[str, StopGate] | None:
    """(the older plugin's reason, the gate it is about) when the old tracker itself screened this role out, else None.

    The older plugin has one word, `rejected`, for an employer's no and for its own gates, and its `dead` for a posting
    that came down. A `fit` cell that says REJECTED, with the gate after it, is the older plugin's own gate saying no
    (on a `rejected` or a `dead` row); so are the phrases it wrote into the notes of a `rejected` row for a gate it ran
    (`_SCREEN_NOTES`), unless the notes hold an email from the employer, which makes it the employer's no."""
    if status not in (stages.REJECTED, stages.GONE_QUIET):
        return None
    fit = raw.get("fit")
    found = _FIT_REJECTED.match(fit) if isinstance(fit, str) else None
    why = None
    if found:
        why = _excerpt(found["why"]) or "a gate"
    elif status == stages.REJECTED:
        text = " ".join(str(raw.get(c) or "") for c in ("notes", "fitEvidence"))
        if not _EMPLOYER_SAID_NO.search(text):
            for pattern, default in _SCREEN_NOTES:
                if (hit := pattern.search(text)) is not None:
                    why = _excerpt(hit["why"]) if len(_excerpt(hit["why"])) >= 3 else default
                    break
    if why is None:
        return None
    gate = next((g for pattern, g in _SCREEN_GATES if pattern.search(why)), StopGate.OTHER)
    return why, gate


#: What a `compSource` says about pay that is not the posting's own: the older plugin's estimates, research,
#: aggregators, a typical range for a grade, pay that was never shown. "posted (deloitte estimate)" is an estimate.
_ESTIMATE_SOURCE = re.compile(
    r"estimat|\best\b|research|aggregator|salary\.com|levels\.fyi|mirror|market|\btypical\b|unposted"
    r"|undisclosed|pending|placeholder|^\s*(?:not|no)\b", re.IGNORECASE)
#: What a `compSource` says about pay the posting shows.
_POSTED_SOURCE = re.compile(
    r"post(?:ed|ing)|disclosed|\bjd\b|listing|listed|\bats\b|careers?|"
    r"description|job page|pay range", re.IGNORECASE)
#: A source the older plugin left blank, or wrote as none or unknown, says nothing about where the pay came from.
_NO_SOURCE = re.compile(r"^\s*(?:(?:none|unknown)\b.*)?$", re.IGNORECASE)
_POSTED_IN_TEXT = re.compile(r"\bposted\b", re.IGNORECASE)
_ESTIMATE_IN_TEXT = re.compile(r"estimat|\best\b|unposted|not posted|undisclosed|not disclosed", re.IGNORECASE)


def posted_pay(source: str | None, comp: str) -> bool:
    """Whether a tracker's `comp` is pay the posting shows, by its `compSource`. When the source says nothing, the pay's
    own words are asked, and only "posted" in them counts."""
    said = (source or "").strip()
    if _NO_SOURCE.match(said):
        return bool(_POSTED_IN_TEXT.search(comp)) and not _ESTIMATE_IN_TEXT.search(comp)
    return not _ESTIMATE_SOURCE.search(said) and bool(_POSTED_SOURCE.search(said))


def pay_of(raw: dict) -> tuple[str | None, str | None, str | None]:
    """(pay as posted, where it came from, the older plugin's estimate) from a row's `comp` and `compSource`. The app's
    `comp` is pay the posting shows (the pay gate reads it so), so an estimate is not stored there: it is kept as the
    estimate it is, for `evidence_of`. Words with no figure in them ("Not disclosed") are not pay and are not kept."""
    comp, source = _text(raw.get("comp")), _text(raw.get("compSource"))
    if comp is None or parse_comp_text(comp) == (None, None):
        return None, source, None
    if posted_pay(source, comp):
        return comp, source, None
    return None, source or "estimate", comp


def evidence_of(raw: dict, estimate: str | None) -> str | None:
    """The row's `fit_evidence`: what the old tracker said about the role. Its own figure for the fit is on a scale the
    app does not use, so the app's `fit` stays empty and the figure is said here, where the role's page and the analyst
    read it, before the older plugin's own evidence line and its estimate of the pay."""
    fit = raw.get("fit")
    parts = []
    if (number := _int(fit)) is not None:
        parts.append(f"Your old tracker scored it {number}, on a scale of its own that this app does not use.")
    elif (word := _text(fit)) and not _FIT_REJECTED.match(word):
        parts.append(f"Your old tracker rated it {word}.")
    if line := _text(raw.get("fitEvidence")):
        parts.append(line)
    if estimate:
        parts.append(f"Pay, your old tracker's estimate and not posted: {estimate}")
    return " ".join(parts) or None


def row_values(raw: dict) -> dict:
    """The app's fields for one tracker row: what a new role is stored with, and what an untouched one is brought up
    to date with (`SYNCED`). The app's fit is left empty; see the module's note."""
    status = stages.from_tracker(_text(raw["status"]))
    screen = plugin_screen(raw, status)
    comp, source, estimate = pay_of(raw)
    reason = None
    if screen:
        reason = f"Screened out in your old tracker: {screen[0]}{'' if screen[0].endswith('.') else '.'}"
    return {
        "status": stages.GATED_OUT if screen else status, "fit": None, "level": _text(raw["level"]),
        "effort": _text(raw["effort"]), "variant": _text(raw["variant"]), "comp": comp, "comp_source": source,
        "location": _text(raw["location"]), "move_type": work_mode(raw["moveType"]), "sector": _text(raw["sector"]),
        "found": _date(raw["found"]), "board": _text(raw["board"]), "url": _text(raw["url"]),
        "apply_url": _text(raw["applyUrl"]), "ats": _text(raw["ats"]), "notes": _text(raw["notes"]),
        "packet_complete": _bool(raw["packetComplete"]), "queue_rank": _int(raw["queueRank"]),
        "fit_evidence": evidence_of(raw, estimate), "col_adjusted": _bool(raw["colAdjusted"]),
        "col_adjusted_comp": _text(raw["colAdjustedComp"]),
        "screened_out_reason": reason, "screened_out_by": screen[1].value if screen else None,
        # The old tracker's own screening was not a step of this app: no re-gate moves the role (docs/PRD.md 39.12).
        "screened_out_step": StopStep.TRACKER.value if screen else None,
    }


# --- coming in again ---------------------------------------------------------------------------------------------

#: The fields of an imported role that the file keeps up to date. Not the role's identity (company, title, key, the
#: tracker's id) and nothing the app fills in (employer, target role, research, packets).
SYNCED = (
    "status", "fit", "level", "effort", "variant", "comp", "comp_source", "location", "move_type", "sector", "found",
    "board", "url", "apply_url", "ats", "notes", "packet_complete", "queue_rank", "fit_evidence", "col_adjusted",
    "col_adjusted_comp", "screened_out_reason", "screened_out_by", "screened_out_step",
)


class _Stored:
    """The owner's roles, found by every key one is known by, so a tracker row that is one of them is not stored twice.
    A row is the role stored under its key `match_key(company, title)`, or under the key an earlier import stored it
    under (the tracker's own `matchKey` column), or one whose own company and title make that key (a role stored under
    a stale key). The first role to claim a key keeps it, so roles go in oldest first. The key is the scout's own and
    exact: whether two writings of one role are one (an "&" and an "and", "Acme" and "Acme, Inc.") is the search's
    dedupe rule to say, and this is the one place the import asks which role a row is."""

    def __init__(self, roles: list[Opportunity]) -> None:
        self._at: dict[str, Opportunity] = {}
        for role in roles:
            for key in (role.match_key, match_key(role.company, role.title)):
                self._at.setdefault(key, role)

    def find(self, company: str, title: str, legacy_key: str | None) -> Opportunity | None:
        for key in (match_key(company, title), (legacy_key or "").strip().lower()):
            if key and (found := self._at.get(key)) is not None:
                return found
        return None


#: What the app writes into a role's history that is not something done to the role itself: it followed the role's link
#: to the employer's own posting (`employer_posting.record_found`).
_FOLLOWED_LINK = "employer_posting_found"
#: The line an import that changed anything writes in the Activity, stamped with the import's one time (`stamp`), which
#: is also the time of every stage move the import made (the import gives the session that time for its moves,
#: `STAGE_MOVES_AT`: a move is otherwise stamped by the clock when its row is made). That is how the next import tells
#: its own moves from the app's.
IMPORTED = "tracker_imported"


async def _worked_on(session: AsyncSession, owner: str) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """(the roles the app has done something with since they came in, the roles whose link the app followed). The first
    are roles researched (the analyst asked or answered), given a packet, moved to another stage by someone other than
    an earlier import (the owner on the board, an agent, the switch on an employer's page: each leaves a stage move,
    whichever code made it), or with a line in their history. A role with none of these has not been touched, and the
    file may change it. The second keep the link the app took."""
    worked = set(await session.scalars(select(Opportunity.id).where(
        Opportunity.owner == owner, Opportunity.analysis_state.is_not(None))))
    worked |= set(await session.scalars(select(Packet.role_id).where(Packet.owner == owner)))
    ours = set(await session.scalars(
        select(TrailEvent.at).where(TrailEvent.owner == owner, TrailEvent.event == IMPORTED)))
    moves = (await session.execute(select(StageMove.role_id, StageMove.at).where(
        StageMove.owner == owner, StageMove.from_status.is_not(None)))).all()  # a role's coming in is no move
    worked |= {role_id for role_id, at in moves if at not in ours}
    history = (await session.execute(select(TrailEvent.link_id, TrailEvent.event).where(
        TrailEvent.owner == owner, TrailEvent.link_kind == "opportunity", TrailEvent.link_id.is_not(None)))).all()
    followed = {role_id for role_id, event in history if role_id is not None and event == _FOLLOWED_LINK}
    worked |= {role_id for role_id, event in history if role_id is not None and event != _FOLLOWED_LINK}
    return worked, followed


def _bring_up_to_date(role: Opportunity, values: dict, link_followed: bool) -> bool:
    """Set the role's synced fields to the file's. Returns whether any changed. A link the app followed is the app's,
    and an Ended stage says why it ended (docs/PRD.md 31.7): Rejected is the employer's no, the others are unknown."""
    changed = False
    for field in SYNCED:
        if link_followed and field == "url":
            continue
        if getattr(role, field) != values[field]:
            if field == "status":
                role.ended_reason = stages.default_end_reason(values["status"])
            setattr(role, field, values[field])
            changed = True
    return changed


async def import_tracker(session: AsyncSession, owner: str, source) -> dict:
    """Bring the tracker's rows into the app. A row that is a role the app has already is that role (matched by the key
    the scout makes, `match_key`); one that is not is stored as a new role, with the tracker's words read into the app's
    (see the module's note). A role from an earlier import that the app has not done anything with since is brought up
    to date with the file; one it has, or one the app's own search found, stays as the app has it.

    Returns counts: `rows` read, `added`, `already_there` (rows that are a role the app has: of them `updated` from the
    file and `left_as_in_app`, the rest were already as the file says), `repeated` (a row for a role an earlier row of
    the file already named) and `skipped_blank` (no company or title)."""
    rows = read_tracker(source)
    stamp = utc_now()  # the import's one time: the roles it stores, its stage moves and its line in the Activity
    await session.flush()  # a move already waiting is the caller's, not the import's
    session.sync_session.info[STAGE_MOVES_AT] = stamp
    try:
        return await _import_rows(session, owner, rows, stamp)
    finally:
        session.sync_session.info.pop(STAGE_MOVES_AT, None)


async def _import_rows(session: AsyncSession, owner: str, rows: list[dict], stamp: datetime) -> dict:
    """`import_tracker` once the file is read: the session stamps every stage move made here with `stamp`."""
    stored = _Stored(list((await session.scalars(
        select(Opportunity).where(Opportunity.owner == owner).order_by(Opportunity.created_at, Opportunity.id)
        .options(defer(Opportunity.description), defer(Opportunity.analysis), defer(Opportunity.fit_breakdown),
                 defer(Opportunity.gate_answers)))).all()))
    worked, followed = await _worked_on(session, owner)
    named: set = set()  # the roles (their ids) and the new keys the file has named so far
    values: list[dict] = []
    counts = {"rows": len(rows), "added": 0, "already_there": 0, "updated": 0, "left_as_in_app": 0, "repeated": 0,
              "skipped_blank": 0}
    for raw in rows:
        company, title = _text(raw["company"]), _text(raw["title"])
        if not company or not title:
            counts["skipped_blank"] += 1
            continue
        key = match_key(company, title)
        role = stored.find(company, title, _text(raw["matchKey"]))
        if (role.id if role is not None else key) in named:
            counts["repeated"] += 1
            continue
        named.add(role.id if role is not None else key)
        fields = row_values(raw)
        if role is None:
            values.append({
                "id": uuid.uuid4(), "owner": owner, "slug": _text(raw["id"]) or slugify(company, title),
                "company": company, "title": title, "match_key": key, "origin": ORIGIN_TRACKER, "created_at": stamp,
                **fields,
                # An ended row says only what its status does (docs/PRD.md 31.7): Rejected is the employer's no.
                "ended_reason": stages.default_end_reason(fields["status"]),
                # A Core insert bypasses the session hook that flags a role in Active Discussion (models.py).
                "landed_interview": fields["status"] == stages.ACTIVE_DISCUSSION,
            })
            continue
        counts["already_there"] += 1
        if role.origin != ORIGIN_TRACKER or role.id in worked:
            counts["left_as_in_app"] += 1
        elif _bring_up_to_date(role, fields, role.id in followed):
            counts["updated"] += 1
    for start in range(0, len(values), 500):
        chunk = values[start:start + 500]
        result = await session.execute(
            insert(Opportunity).values(chunk)
            .on_conflict_do_nothing(index_elements=["owner", "match_key"]).returning(Opportunity.id))
        stored_ids = {row[0] for row in result.all()}
        stored_now = len(stored_ids)
        counts["added"] += stored_now
        # A key that turned out to be stored already (written since the roles were read) is a role the app has.
        counts["already_there"] += len(chunk) - stored_now
        counts["left_as_in_app"] += len(chunk) - stored_now
        # Every role has a posting in the log, with the import as its source (docs/PRD.md 39.9): the roles stored now
        # get theirs, so the file read again, which stores none, adds none.
        await role_postings.log_imported(session, [v for v in chunk if v["id"] in stored_ids])
        # The roles stored now are looked at, as the hook would for roles saved through the session.
        queue.post_new_roles(session, owner, [v for v in chunk if v["id"] in stored_ids], cause="core:tracker.import")
    if counts["added"] or counts["updated"]:
        session.add(TrailEvent(
            owner=owner, at=stamp, actor="app", who=trail.APP_WHO, event=IMPORTED,
            sentence=(f"Read your tracker: {counts['added']} new, {counts['updated']} brought up to date, "
                      f"{counts['left_as_in_app']} left as the app has them.")))
    await session.flush()
    return counts


# --- export --------------------------------------------------------------------------------------------------


def _cell(value):
    if isinstance(value, date):
        return value.isoformat()
    return value


def _write_row(sheet, row: list) -> None:
    """Append a row to a write-only sheet as text, never as formulas: a posting's title or a note can start with `=`
    (a scout's report is data from the web), and openpyxl would store it as a formula that Excel runs when the sheet
    is opened. Each row is written once and not kept, so the workbook is never all in memory."""
    from openpyxl.cell import WriteOnlyCell

    cells = []
    for value in row:
        if isinstance(value, str) and value.startswith("="):
            cell = WriteOnlyCell(sheet, value=value)
            cell.data_type = "s"
            value = cell
        cells.append(value)
    sheet.append(cells)


EXPORT_SLICE = 500  # rows built between yields to the event loop


async def export_rows(session: AsyncSession, owner: str) -> tuple[list[list], list[list]]:
    """The two sheets' rows: `Tracker` (every role not screened out, the tracker's columns in the tracker's order,
    oldest first; the queue order is the page's) and `ScreenedOut`. Two queries of just the columns the sheets hold: a
    role also carries its posting text and research, and loading those whole for every role was most of the cost."""
    kept = (await session.execute(
        select(*[getattr(Opportunity, ATTR[c]) for c in COLUMNS])
        .where(Opportunity.owner == owner, Opportunity.status != stages.GATED_OUT)
        .order_by(Opportunity.created_at, Opportunity.slug))).all()
    dropped = (await session.execute(
        select(Opportunity.company, Opportunity.title, Opportunity.location, Opportunity.comp, Opportunity.url,
               Opportunity.board, Opportunity.found, Opportunity.screened_out_reason)
        .where(Opportunity.owner == owner, Opportunity.status == stages.GATED_OUT)
        .order_by(Opportunity.created_at, Opportunity.slug))).all()
    # The status column holds the stage's name (docs/PRD.md 23.10), which reads back in through from_tracker.
    status_at = COLUMNS.index("status")
    tracker_rows = []
    for start in range(0, len(kept), EXPORT_SLICE):
        for values in kept[start:start + EXPORT_SLICE]:
            row = [_cell(v) for v in values]
            row[status_at] = stages.LABELS.get(values[status_at], values[status_at])
            tracker_rows.append(row)
        await asyncio.sleep(0)  # the loop (and the writer's turn) is free between slices
    screened_rows = []
    for start in range(0, len(dropped), EXPORT_SLICE):
        screened_rows.extend([_cell(v) for v in values] for values in dropped[start:start + EXPORT_SLICE])
        await asyncio.sleep(0)
    return tracker_rows, screened_rows


async def build_workbook(tracker_rows: list[list], screened_rows: list[list]) -> bytes:
    """The .xlsx of the rows `export_rows` read. The export route reads the rows, ends its read and only then calls
    this, so no connection is held while the file is written."""
    # Writing the workbook touches no database, so it runs off the event loop.
    return await asyncio.to_thread(_workbook_bytes, tracker_rows, screened_rows)


def _workbook_bytes(tracker_rows: list[list], screened_rows: list[list]) -> bytes:
    import openpyxl

    workbook = openpyxl.Workbook(write_only=True)
    sheet = workbook.create_sheet(SHEET)
    sheet.append(COLUMNS)
    for row in tracker_rows:
        _write_row(sheet, row)
    out = workbook.create_sheet(SCREENED_SHEET)
    out.append(SCREENED_COLUMNS)
    for row in screened_rows:
        _write_row(out, row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
