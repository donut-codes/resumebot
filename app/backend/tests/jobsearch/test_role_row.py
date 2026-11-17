"""A role's row (docs/PRD.md paths 7 and 11): what the Pipeline list shows for one role, with words only for a problem
or something the owner acts on (24.18). One row for one role, wherever it is asked for: the column it is in, the role's
own page, the answer to a move and the answer to a star. Synthetic roles only."""

import json
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import event, select

from app import db
from app.jobsearch import stages
from app.jobsearch.fit import scoring
from app.jobsearch.models import ADDED_LINK, JobPosting, Opportunity, Packet
from app.jobsearch.pipeline import deck as pipeline
from app.jobsearch.pipeline import live
from app.jobsearch.pipeline import row as role_row
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import analysis
from app.timeutil import owner_today, utc_now
from tests.builders import GATE_ZERO, counted, rank_roles
from tests.employer_builders import make_employer
from tests.support import OWNER, PROFILE, load_profile

pytestmark = pytest.mark.usefixtures("the_clock_says_noon")

#: The row on file: every key with a value of the type it has, which the page's `BoardCard` is checked against
#: (`frontend/src/api/types/pipeline.ts`, `role-row.test.ts`). The frontend keeps a copy of the same file.
FIXTURE = db.BACKEND_DIR / "tests" / "role_row.json"
FRONTEND_COPY = db.BACKEND_DIR.parent / "frontend" / "src" / "api" / "role-row.json"


def _role(slug, status=stages.READY_TO_LAUNCH, **extra):
    day = owner_today() - timedelta(days=10)
    return Opportunity(**{"owner": OWNER, "slug": slug, "company": f"Co {slug}", "title": f"Role {slug}",
                          "status": status, "origin": "scout", "match_key": f"co {slug}|role {slug}", "posted": day,
                          "found": day, **extra})


def _packet(role, status, **brief) -> Packet:
    return Packet(owner=OWNER, role_id=role.id, version=1, status=status, feedback="", resume={}, letter={}, comp={},
                  reasoning={}, checks=[], brief=brief)


async def _spread() -> dict[str, str]:
    """About a dozen roles, each in a different state the row has words for. Returns their ids by slug."""
    now = utc_now()
    async with db.session_factory()() as session:
        oakfield = await make_employer(session, "Oakfield Works", headquarters="Oakfield, ST")
        roles = [
            _role("ranked", stages.FIRST_IMPRESSION),
            _role("waiting", stages.SECOND_IMPRESSION, analysis_state="waiting"),
            _role("deck", stages.SECOND_IMPRESSION, **counted()),
            _role("plain"),
            _role("writing"), _role("held"), _role("kept"), _role("failed"),
            _role("opened", go_opened_at=now),
            _role("rechecking", analysis_state="waiting"),
            _role("skipped", go_skip="the posting asks for a login"),
            _role("gave_up", stages.FIRST_IMPRESSION, browser_note="the page would not load"),
            _role("star", stages.SECOND_IMPRESSION, favorited_at=now),
            _role("hybrid", stages.SECOND_IMPRESSION, move_type="hybrid", location="", employer_id=oakfield.id),
            _role("asked", stages.SECOND_IMPRESSION, comp="$90,000 - $120,000",
                  analysis={"ask": {"ask": 100000, "below_floor": True}}),
            _role("applied", stages.APPLIED),
            _role("talking", stages.ACTIVE_DISCUSSION),
            _role("rejected", stages.REJECTED, ended_reason="they_said_no"),
            _role("gated", stages.GATED_OUT, screened_out_reason="pay under the floor"),
        ]
        session.add_all(roles)
        await session.flush()
        by_slug = {r.slug: r for r in roles}
        await rank_roles(session, {by_slug["ranked"].id: 77, by_slug["waiting"].id: 60})
        for slug, status, brief in (("writing", "writing", {"step": "checking"}), ("held", "held", {}),
                                    ("kept", "ready", {}), ("failed", "failed", {})):
            session.add(_packet(by_slug[slug], status, **brief))
        await session.commit()
        return {r.slug: str(r.id) for r in roles}


async def _listed(client, role_id: str) -> dict:
    """The role's card in the column its status puts it in."""
    body = (await client.get("/api/v1/board")).json()
    cards = {c["id"]: c for col in body["columns"] for c in col["cards"]}
    return cards[role_id]


async def _pane(client, role_id: str) -> dict:
    return (await client.get(f"/api/v1/roles/{role_id}")).json()["role"]


async def _row(role_id: str) -> dict:
    async with db.session_factory()() as session:
        return dict(await role_row.row(session, OWNER, uuid.UUID(role_id)))


async def _thin(client, role_id: str) -> dict:
    """The row on its own: what the page reads again when the live stream says a role changed."""
    return (await client.get(f"/api/v1/roles/{role_id}/row")).json()


async def _everywhere(client, role_id: str) -> dict:
    """The role as every way of asking for it says it."""
    return {"column": await _listed(client, role_id), "page": await _pane(client, role_id),
            "row": await _row(role_id), "thin": await _thin(client, role_id)}


def _disagreements(slug: str, **asked) -> list[str]:
    """What two of the answers say differently, key by key, over the keys they share: a role reads one way wherever it
    is asked for."""
    (first_name, first), *rest = asked.items()
    out = []
    for name, other in rest:
        shared = first.keys() & other.keys()
        assert shared, (slug, first_name, name)
        differs = {k: (first[k], other[k]) for k in sorted(shared) if first[k] != other[k]}
        if differs:
            out.append(f"{slug}: {first_name} and {name} disagree: {differs}")
    return out


async def test_a_role_reads_the_same_wherever_it_is_asked_for(client):
    ids = await _spread()
    problems = []
    for slug, role_id in ids.items():
        asked = await _everywhere(client, role_id)
        problems += _disagreements(slug, **asked)
        assert set(asked["column"]) == set(role_row.Row.__annotations__) == set(asked["thin"]), slug
        assert set(asked["column"]) == asked["page"].keys() & asked["column"].keys(), slug

    # A move answers with the row the role's new column shows.
    # (a role with no read that is moved to Ready to Launch stays where it is: the move waits for its read)
    for slug, to, lands in (("deck", stages.READY_TO_LAUNCH, stages.READY_TO_LAUNCH),
                            ("ranked", stages.READY_TO_LAUNCH, stages.FIRST_IMPRESSION),
                            ("kept", stages.APPLIED, stages.APPLIED), ("rechecking", stages.APPLIED, stages.APPLIED)):
        answer = (await client.post(f"/api/v1/roles/{ids[slug]}/move", json={"to": to})).json()
        answer.pop("counts")
        assert answer["status"] == lands
        problems += _disagreements(slug, move=answer, **await _everywhere(client, ids[slug]))
    back = (await client.post(f"/api/v1/roles/{ids['rejected']}/move-back")).json()
    back.pop("counts")
    problems += _disagreements("rejected", moved_back=back, **await _everywhere(client, ids["rejected"]))

    # So does a star.
    for slug in ("applied", "deck"):
        starred = (await client.put(f"/api/v1/roles/{ids[slug]}/favorite", json={"favorite": True})).json()["role"]
        assert starred["favorite"] is True
        problems += _disagreements(slug, star=starred, **await _everywhere(client, ids[slug]))
    assert not problems, "; ".join(problems)


async def test_a_role_that_is_none_of_the_owners_has_no_row_to_read(client):
    assert (await client.get(f"/api/v1/roles/{uuid.uuid4()}/row")).status_code == 404


# --- what a row says happens next -----------------------------------------------------------------------------------

_FIRST, _SECOND, _READY = stages.FIRST_IMPRESSION, stages.SECOND_IMPRESSION, stages.READY_TO_LAUNCH
_OPENED = datetime(2026, 10, 21, 9, 10, tzinfo=UTC)
#: A role in each state a row has something to say of (docs/PRD.md 24.18): (what it is, its status, what it holds, what
#: its row says next). A `rank` gives it its First Impression estimate; a `packet` is its newest one (status and brief).
#: None is the normal run of a stage: the row says nothing of it, since the stage and the number say all there is.
_SAYS = [
    ("a deck role", _SECOND, {}, None),
    ("a deck role not sourced", _SECOND, {"sourced_at": None}, None),
    ("a ranked first impression", _FIRST, {"rank": 77}, None),
    ("an unranked first impression", _FIRST, {}, None),
    # The gate stopped it, with its reason: that stays. Old: "... Not read unless you star it or move it" after it; the
    # owner, 2026-11-06, took that constant tail off (docs/PRD.md 24.18).
    ("a first impression the ranker ruled out on a gate", _FIRST, {"rank": GATE_ZERO},
     "Gate: pay is under the floor."),
    ("a starred first impression the ranker ruled out", _FIRST, {"rank": GATE_ZERO, "favorited_at": _OPENED}, None),
    ("a first impression asked for in Ready that the ranker ruled out", _FIRST,
     {"rank": GATE_ZERO, "ready_asked_at": _OPENED}, None),
    ("a first impression read line by line", _FIRST, {"analysis_state": analysis.DONE}, None),
    ("a deck role read line by line", _SECOND, {"analysis_state": analysis.DONE}, None),
    ("a read that failed", _SECOND, {"analysis_state": analysis.FAILED}, "Analyst didn't finish: send it on yourself"),
    ("a role waiting for its text", _SECOND, {"analysis_state": analysis.NO_TEXT}, None),
    ("a starred role not sourced", _SECOND, {"favorited_at": _OPENED, "sourced_at": None}, None),
    ("a starred role not ranked", _SECOND, {"favorited_at": _OPENED}, None),
    ("a role asked for in Ready not sourced", _SECOND, {"ready_asked_at": _OPENED, "sourced_at": None}, None),
    ("a role asked for in Ready not ranked", _SECOND, {"ready_asked_at": _OPENED}, None),
    ("a role asked for in Ready whose read failed", _SECOND,
     {"ready_asked_at": _OPENED, "analysis_state": analysis.FAILED}, "Analyst didn't finish: send it on yourself"),
    ("a role asked for in Ready and starred", _SECOND, {"ready_asked_at": _OPENED, "favorited_at": _OPENED}, None),
    ("a read that graded nothing", _SECOND, counted(required=[]), role_row.UNGRADED_WORDS),
    ("a read that graded something", _SECOND, counted(), None),
    ("a followup", stages.FOLLOWUP_NEEDED, {}, None),
    ("a landing zone role", stages.LANDING_ZONE, {}, None),
    ("a cleansed role", stages.CLEANSED_DATA, {}, None),
    ("an application", stages.APPLIED, {}, None),
    ("a talk", stages.ACTIVE_DISCUSSION, {}, None),
    # Ready to Launch: a packet in the making shows its step; one that failed or is held waits on the owner.
    ("a ready role with no packet", _READY, {}, None),
    ("a ready role whose packet is being written", _READY, {"packet": ("writing", {"step": "checking"})},
     "Its facts being checked"),
    ("a ready role with its packet", _READY, {"packet": ("ready", {})}, None),
    ("a ready role whose packet failed", _READY, {"packet": ("failed", {})}, "The packet failed"),
    ("a ready role whose packet is held", _READY, {"packet": ("held", {})}, "The packet is held"),
    ("a ready role whose only packet was replaced", _READY, {"packet": ("replaced", {})}, None),
    ("a ready role whose posting was checked open", _READY,
     {"go_check": "open", "go_checked_at": _OPENED, "packet": ("ready", {})}, None),
    ("a ready role Launch opened", _READY, {"go_opened_at": _OPENED, "packet": ("ready", {})},
     "Opened. Mark Applied when sent"),
    # Closed: why it ended, else the gate's reason, else its stage.
    ("a rejection", stages.REJECTED, {"ended_reason": stages.THEY_SAID_NO}, "They said no"),
    ("a withdrawal", stages.WITHDREW, {"ended_reason": stages.I_PASSED}, "I passed on it"),
    ("a quiet end", stages.GONE_QUIET, {}, "Not known"),
    ("a gate's stop", stages.GATED_OUT, {"screened_out_reason": "pay under the floor"}, "pay under the floor"),
    ("a stop with no reason", stages.GATED_OUT, {}, "Gated out"),
    # What is said first, whatever the stage says: a role Launch opened is opened, and what Go does next with the others
    # (skips them, waits for their research) is not said of it.
    ("a skipped ready role Launch opened", _READY, {"go_skip": "the posting asks for a login", "go_opened_at": _OPENED},
     "Opened. Mark Applied when sent"),
    ("a ready role Launch opened whose research is waiting", _READY,
     {"analysis_state": analysis.WAITING, "go_opened_at": _OPENED}, "Opened. Mark Applied when sent"),
    ("a skipped ready role being re-checked", _READY,
     {"go_skip": "the posting asks for a login", "analysis_state": analysis.WAITING},
     "Launch in Browser skips it: the posting asks for a login"),
    ("a ready role being re-checked", _READY, {"analysis_state": analysis.WAITING}, role_row.RECHECKING_WORDS),
    ("a ranked role the browser gave up on", _FIRST, {"rank": 77, "browser_note": "the page would not load"},
     "The browser gave up after its tries: the page would not load"),
    ("a ready role with a browser note", _READY, {"browser_note": "the page would not load"}, None),
    # Waiting for the analyst is a normal state, whatever the role's place in the line.
    ("a role with the analyst", _SECOND, {"analysis_state": analysis.WAITING}, None),
]


async def test_next_says_only_what_is_wrong_or_waits_on_the_owner(js):
    async with db.session_factory()() as session:
        made = []
        for n, (name, status, fields, said) in enumerate(_SAYS):
            held = {k: v for k, v in fields.items() if k not in ("rank", "packet")}
            made.append((name, _role(f"s{n}", status, **held), fields.get("rank"), fields.get("packet"), said))
        session.add_all([role for _, role, _, _, _ in made])
        await session.flush()
        await rank_roles(session, {role.id: rank for _, role, rank, _, _ in made if rank is not None})
        for _, role, _, packet, _ in made:
            if packet:
                session.add(_packet(role, packet[0], **packet[1]))
        await session.flush()
        got = {name: (await role_row.row(session, OWNER, role.id))["next"] for name, role, _, _, _ in made}
    assert got == {name: said for name, _, _, _, said in made}


async def test_a_gate_zeros_card_shows_zero_and_a_plain_roles_card_its_rank_as_written(js):
    """Old: the owner's rank map made a plain role's card 55 and an applied role's too (the map's figure). New
    (docs/PRD.md 24.15): the card shows the rank as written, a plain role 20, a gate zero's 0 wherever the role is. Its
    line is the gate's reason, without the "Estimate 0:" the number says (24.18); an applied role's row says nothing."""
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        roles = {s: _role(s, status) for s, status in
                 (("gate", _FIRST), ("plain", _FIRST), ("applied", stages.APPLIED))}
        session.add_all(roles.values())
        await session.flush()
        await rank_roles(session, {roles["gate"].id: GATE_ZERO, roles["plain"].id: 20, roles["applied"].id: GATE_ZERO})
        rows = {slug: await role_row.row(session, OWNER, role.id) for slug, role in roles.items()}
    assert {slug: row["score"] for slug, row in rows.items()} == {"gate": 0, "plain": 20, "applied": 0}, (
        "the ranker's gate is Haiku's zero; an applied role's card says the same zero")
    assert rows["gate"]["next"].startswith("Gate: pay is under the floor.")
    assert rows["applied"]["next"] is None


async def test_a_ready_role_says_of_its_posting_check_only_what_is_not_confirmed_or_could_not_be_checked(js):
    """Old: every check said something ("Posting open when checked today", "The browser is checking the posting is
    still open", and for a lapsed one "...; Launch checks it again"). New (docs/PRD.md 24.16, 24.18): a posting found
    open, and a browser still looking, are the normal run and say nothing; one the page did not confirm says so with
    the day, and one the browser could not check says so."""
    now = utc_now()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        roles = {slug: _role(slug, _READY, **fields) for slug, fields in {
            "today": {"go_check": "entered", "go_checked_at": now},
            "gos": {"go_check": "open", "go_checked_at": now},
            "looking": {"go_check": "looking", "go_checked_at": now - timedelta(minutes=5)},
            "gos_browser": {"go_check": "browser", "go_checked_at": now - timedelta(minutes=5)},
            "lapsed": {"go_check": "looking", "go_checked_at": now - timedelta(hours=3)},
            "unclear": {"go_check": "unclear", "go_checked_at": now - timedelta(days=3)},
            "never": {},
            "opened": {"go_check": "open", "go_checked_at": now, "go_opened_at": now}}.items()}
        session.add_all(roles.values())
        await session.flush()
        said = {slug: (await role_row.row(session, OWNER, r.id))["next"] for slug, r in roles.items()}
    assert said == {
        "today": None, "gos": None, "looking": None, "gos_browser": None,
        "lapsed": "The browser could not check the posting",
        "unclear": f"Posting not confirmed when checked {(now - timedelta(days=3)):%b} {(now - timedelta(days=3)).day}",
        "never": None,
        "opened": "Opened. Mark Applied when sent"}


async def test_a_packet_that_failed_and_a_posting_not_confirmed_are_both_said_on_a_ready_row(js):
    now = utc_now()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        role = _role("both", _READY, go_check="unclear", go_checked_at=now)
        session.add(role)
        await session.flush()
        session.add(_packet(role, "failed"))
        await session.flush()
        said = (await role_row.row(session, OWNER, role.id))["next"]
    assert said == "The packet failed · Posting not confirmed when checked today"


async def test_pay_mostly_under_the_floor_is_said_of_a_role_in_play_and_of_no_other(js):
    gates = {"compensation": {"amount": 120_000, "against": "maximum"}}
    note = "Pay mostly below your floor: $70,000–$150,000"
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates)
        roles = {slug: _role(slug, status, comp="$70,000-$150,000", **more) for slug, status, more in (
            ("deck", _SECOND, {}), ("ready", _READY, {}), ("skipped", _READY, {"go_skip": "no login"}),
            ("rejected", stages.REJECTED, {"ended_reason": stages.THEY_SAID_NO}))}
        roles["reaches"] = _role("reaches", _SECOND, comp="$115,000-$150,000")
        session.add_all(roles.values())
        await session.flush()
        said = {slug: (await role_row.row(session, OWNER, r.id))["next"] for slug, r in roles.items()}
    assert said == {
        "deck": note, "ready": note, "skipped": f"Launch in Browser skips it: no login · {note}",
        "rejected": "They said no", "reaches": None}


# --- work, pay and age ----------------------------------------------------------------------------------------------


async def test_a_row_gives_standard_answers_for_work_pay_and_age(js, monkeypatch):
    monkeypatch.setattr(role_row, "owner_today", lambda: date(2026, 10, 7))
    async with db.session_factory()() as session:
        roles = {
            "remote": _role("remote", location="United States (Lakeside, IL listed on Boardone header); aggregator "
                            "lists Remote", move_type="remote", comp="$250,000 - $315,000 base + annual bonus",
                            posted=date(2026, 10, 2)),
            "hybrid": _role("hybrid", location="Riverton, ST", move_type="hybrid", posted=None, found=None),
            "onsite": _role("onsite", location="Elmwood, ST", move_type="onsite"),
            "unsaid": _role("unsaid", location=None, move_type=None, comp=None, posted=None, found=date(2026, 10, 7)),
            "from_the_place": _role("from_the_place", location="Open to a hybrid week", move_type=None),
        }
        session.add_all(roles.values())
        await session.flush()
        rows = {slug: await role_row.row(session, OWNER, r.id) for slug, r in roles.items()}
    assert rows["remote"]["work"] == {"mode": "remote", "place": "Lakeside, IL", "words": "Remote"}
    assert rows["remote"]["pay"] == "$250K–$315K"
    assert rows["remote"]["age"] == {"days": 5, "words": "5 days", "from": "posted"}
    assert rows["hybrid"]["work"]["words"] == "Hybrid · Riverton, ST"
    assert rows["hybrid"]["age"] == {"days": None, "words": "Unknown", "from": None}
    assert rows["onsite"]["work"]["words"] == "In person · Elmwood, ST"
    assert rows["unsaid"]["work"] == {"mode": "unknown", "place": None, "words": "Not stated"}
    assert rows["unsaid"]["pay"] == "Not posted"
    assert rows["unsaid"]["age"] == {"days": 0, "words": "Today", "from": "found"}
    assert rows["from_the_place"]["work"]["mode"] == "hybrid", "the work-mode word failing, the location line says it"


async def test_the_pay_expectation_keeps_the_employers_city_for_a_role_with_none(client):
    async with db.session_factory()() as session:
        oakfield = await make_employer(session, "Oakfield Works", headquarters="Oakfield, ST")
        role = _role("hq", _SECOND, move_type="hybrid", location="", employer_id=oakfield.id,
                     analysis={"ask": {"ask": 100000}})
        session.add(role)
        await session.commit()
    page = (await client.get(f"/api/v1/roles/{role.id}")).json()
    assert page["role"]["work"]["words"] == "Hybrid", "the row says only what the role says"
    assert "hybrid in Oakfield, ST" in page["comp_answer"]["assumptions"]


# --- the shape of a row ---------------------------------------------------------------------------------------------


async def _sample_rows() -> list[role_row.Row]:
    """A row for a role with everything to say, one that ended and one a gate stopped: between them every key of the
    row holds a value."""
    now = utc_now()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        oakfield = await make_employer(session, "Oakfield Works", headquarters="Oakfield, ST")
        full = _role("full", fit=3, posted=owner_today(), favorited_at=now, go_opened_at=now, comp="$90,000 - $120,000",
                     move_type="hybrid", location="Riverton, ST", employer_id=oakfield.id, url="https://x.test/jobs/1",
                     apply_url="https://x.test/jobs/1/apply", analysis={"ask": {"ask": 100000}})
        over = _role("over", stages.REJECTED, ended_reason=stages.THEY_SAID_NO)
        stopped = _role("stopped", stages.GATED_OUT, screened_out_reason="pay under the floor")
        session.add_all([full, over, stopped])
        await session.flush()
        session.add(_packet(full, "writing", step="checking"))
        await rank_roles(session, {full.id: 80})
        await session.flush()
        return [await role_row.row(session, OWNER, r.id) for r in (full, over, stopped)]


def _kinds(row) -> dict:
    """The row's keys with, for a nested one, its keys: what the page's type has to say."""
    return {k: _kinds(v) if isinstance(v, dict) else None for k, v in row.items()}


async def test_the_row_has_the_frozen_shape(js):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = await _sample_rows()
    for row in rows:
        assert _kinds(row) | {"packet": None} == _kinds(fixture) | {"packet": None}, "the same keys and nested keys"
        for key, value in row.items():
            if value is not None:
                assert type(value) is type(fixture[key]), key
    assert _kinds(next(r for r in rows if r["packet"])["packet"]) == _kinds(fixture["packet"])
    assert all(fixture[key] is not None for key in fixture), "the fixture holds a value of every key's type"
    assert all(any(r[key] is not None for r in rows) for key in fixture), "and every key is tried with one"
    async with db.session_factory()() as session:
        session.add(JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats="owner", ext_id="w1", company=ADDED_LINK, title="Posting 1",
            match_key="firm 1|posting 1", location="Remote", work_mode="remote", url="https://jobs.example.test/1",
            first_seen=utc_now(), last_seen=utc_now(), filtered_at=utc_now(), links=[], route="roles"))
        await session.flush()
        (added,) = (await role_row.columns(session, OWNER, "added_links"))[0]["cards"]
    assert added.keys() == fixture.keys(), "an added link's row has the same keys as a role's"


def test_the_frontends_copy_of_the_row_is_the_same_file_where_both_are_in_the_checkout():
    if not FRONTEND_COPY.exists():
        pytest.skip("the backend's test image holds only the backend; a checkout holds both")
    assert FRONTEND_COPY.read_text(encoding="utf-8") == FIXTURE.read_text(encoding="utf-8"), (
        "write the same file to app/backend/tests/role_row.json and app/frontend/src/api/role-row.json")


# --- the lanes ------------------------------------------------------------------------------------------------------


async def test_a_move_and_a_star_answer_with_the_row_the_column_now_shows(client):
    ids = await _spread()
    moved = (await client.post(f"/api/v1/roles/{ids['ranked']}/move", json={"to": stages.APPLIED})).json()
    counts = moved.pop("counts")
    applied = (await client.get("/api/v1/board", params={"column": stages.APPLIED})).json()["columns"][0]
    assert moved in applied["cards"] and counts["lanes"][stages.APPLIED] == applied["count"]
    first = (await client.get("/api/v1/board", params={"column": stages.FIRST_IMPRESSION})).json()["columns"][0]
    assert ids["ranked"] not in [c["id"] for c in first["cards"]] and counts["lanes"][stages.FIRST_IMPRESSION] == (
        first["count"])

    starred = (await client.put(f"/api/v1/roles/{ids['deck']}/favorite", json={"favorite": True})).json()
    favorites = (await client.get("/api/v1/favorites")).json()
    assert starred["role"] in favorites["cards"] and starred["counts"]["favorites"] == favorites["count"]
    lane = (await client.get("/api/v1/board", params={"column": starred["role"]["status"]})).json()["columns"][0]
    assert starred["role"] in lane["cards"]


async def test_every_lane_counts_what_the_rail_counts(js):
    now = utc_now()
    async with db.session_factory()() as session:
        session.add_all([
            *(_role(f"{status}{n}", status, favorited_at=now if n == 0 else None)
              for status in stages.STAGES for n in range(2)),
            JobPosting(owner=OWNER, employer_id=uuid.uuid4(), ats="owner", ext_id="w1", company=ADDED_LINK,
                       title="Posting 1", match_key="firm 1|posting 1", location="Remote", first_seen=now,
                       last_seen=now, filtered_at=now, links=[], route="roles")])
        await session.flush()
        counts = await live.counts(session, OWNER)
        shown = {c["key"]: c for c in await role_row.columns(session, OWNER)}
        starred = await role_row.favorites(session, OWNER)
    assert set(shown) == set(counts["lanes"]) and all(counts["lanes"].values())
    assert {key: c["count"] for key, c in shown.items()} == counts["lanes"]
    assert starred["count"] == counts["favorites"] == len(starred["cards"]) > 0


async def test_a_score_is_the_number_the_lane_sorts_by(js):
    worked = {"answers": [], "asks": [], "possible": 5, "decided": 5, "percent": 40, "total": 2, "math": 2}
    today = owner_today()
    cases = {  # slug: (what it holds, its rank, the score its row shows)
        "read": ({"fit": 2, "fit_breakdown": worked, **counted()}, 90, 40),  # its read, not its rank
        "ranked": ({"posted": today - timedelta(days=10)}, 70, 70),
        "stale": ({"posted": today - timedelta(days=45)}, 70, 49),
        # Old (24.11, 2026-11-03): 70, the score without the 20 of its first hours. New (24.15, 2026-11-06): 90.
        "fresh": ({"posted": today}, 70, 90),
        # Old (24.15, first build): 115 kept to 100 on the card, while the list sorted by 115. New (the owner,
        # 2026-11-06, the list is sorted by exactly the number the card shows): the card says 115.
        "over_100": ({"posted": today}, 95, 115),
        "over_100_less": ({"posted": today}, 90, 110),
        "tiny_and_stale": ({"posted": today - timedelta(days=45)}, 5, 0),
        "unscored": ({"posted": today}, None, None),
    }
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        made = {slug: _role(slug, _SECOND, **{"posted": today - timedelta(days=10), **fields})
                for slug, (fields, _, _) in cases.items()}
        session.add_all(made.values())
        await session.flush()
        await rank_roles(session, {made[slug].id: rank for slug, (_, rank, _) in cases.items() if rank is not None})
        rows = {slug: await role_row.row(session, OWNER, r.id) for slug, r in made.items()}
        ranks = await pipeline.ranks(session, OWNER)
        read = await analysis.reads(session, [r.id for r in made.values()])
        span = scoring.bounds(await profile_store.current(session, OWNER))
        sorts_by = {slug: pipeline.points(r, ranks.get(r.id), span, r.id in read, owner_today(), utc_now())
                    for slug, r in made.items()}
        cards = (await role_row.columns(session, OWNER, _SECOND))[0]["cards"]
        lane = [c["title"] for c in cards]
    assert {slug: row["score"] for slug, row in rows.items()} == {slug: want for slug, (_, _, want) in cases.items()}
    assert {slug: row["score"] for slug, row in rows.items()} == {
        slug: None if slug == "unscored" else points for slug, points in sorts_by.items()}, (
        "the card prints the number the lane sorts by")
    assert sorts_by["fresh"] == 90 and sorts_by["over_100"] == 115 and sorts_by["over_100_less"] == 110
    assert sorts_by["unscored"] == 0, "no score is 0 in the order and no number on the card"
    ordered = sorted(made, key=lambda slug: -sorts_by[slug])
    assert [t.removeprefix("Role ") for t in lane][:4] == ordered[:4], "the lane sorts by the number, bonus and all"
    numbers = [c["score"] for c in cards if c["score"] is not None]
    assert numbers == sorted(numbers, reverse=True) and len(set(numbers)) == len(numbers), (
        "the numbers the cards print fall down the lane, two over 100 apart: no two cards tie but by their number")


async def test_a_new_roles_row_carries_the_bonus_in_its_number_as_a_key_and_says_nothing_of_it_in_words(js):
    """Old (docs/PRD.md 24.15, 24.17): the row's line said what the hours add ("Estimate 70: waits to be read · Its
    first hours add 20") beside a card of 90. New (24.18): the line says nothing of it and the row carries the bonus as
    `first_hours_bonus`, the points of `score` that are the first hours', for the New tag to say (New (+20))."""
    today = owner_today()
    worked = {"answers": [], "asks": [], "possible": 5, "decided": 5, "percent": 40, "total": 2, "math": 2}
    now = utc_now()
    cases = {  # slug: (status, what it holds, rank, the bonus in the number, whether it is new)
        "unread": (_FIRST, {}, 70, 20, True),
        "older": (_FIRST, {"posted": today - timedelta(days=10)}, 70, 0, False),
        "gate_zero": (_FIRST, {}, GATE_ZERO, 0, True),
        "read": (_SECOND, {"fit": 2, "fit_breakdown": worked, **counted()}, 90, 20, True),
        "ready": (_READY, {"go_check": "entered", "go_checked_at": now}, 60, 20, True),
        "applied": (stages.APPLIED, {}, 70, 20, True),
        "unscored": (_SECOND, {}, None, 0, True),
    }
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        made = {slug: _role(slug, status, **{"posted": today, **fields})
                for slug, (status, fields, *_) in cases.items()}
        session.add_all(made.values())
        await session.flush()
        await rank_roles(session, {made[slug].id: rank for slug, (_, _, rank, *_) in cases.items()
                                   if rank is not None})
        rows = {slug: await role_row.row(session, OWNER, r.id) for slug, r in made.items()}
    assert {slug: (row["first_hours_bonus"], row["new"]) for slug, row in rows.items()} == {
        slug: (bonus, new) for slug, (_, _, _, bonus, new) in cases.items()}
    assert {slug: rows[slug]["score"] for slug in ("unread", "older", "gate_zero", "ready", "applied", "unscored")} == {
        "unread": 90, "older": 70, "gate_zero": 0, "ready": 80, "applied": 90, "unscored": None}, (
        "the bonus is in the number the card prints, and a rank of 0 is not lifted")
    assert all("first hours" not in (row["next"] or "") for row in rows.values()), "no sentence says what they add"
    assert rows["gate_zero"]["next"].startswith("Gate: pay is under the floor.")


async def test_a_rows_bonus_is_the_owners_own_and_zero_when_they_turned_the_first_hours_off(js):
    today = owner_today()
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "golden_bonus": 35})
        fresh = _role("fresh", _FIRST, posted=today)
        session.add(fresh)
        await session.flush()
        await rank_roles(session, {fresh.id: 50})
        row = await role_row.row(session, OWNER, fresh.id)
        assert (row["score"], row["first_hours_bonus"], row["new"], row["next"]) == (85, 35, True, None)
        await load_profile(session, OWNER, settings={**PROFILE["settings"], "golden_bonus": 0})
        row = await role_row.row(session, OWNER, fresh.id)
        assert (row["score"], row["first_hours_bonus"], row["new"], row["next"]) == (50, 0, True, None)


async def test_the_ranked_list_is_three_stages_as_one_list_by_the_one_number_and_work_is_the_rest(js):
    """docs/PRD.md 24.17: the same roles as their lanes, in another grouping, for the page to draw apart."""
    today = owner_today()
    read = {**counted(), "fit": 2, "fit_breakdown": {"math": 1, "percent": 80, "asks": []}}
    made = {slug: _role(slug, status, posted=today - timedelta(days=days), **more) for slug, status, days, more in (
        ("ready", _READY, 10, read), ("second", _SECOND, 10, {}), ("first", _FIRST, 10, {}), ("low", _FIRST, 10, {}),
        ("follow", stages.FOLLOWUP_NEEDED, 20, {}), ("landing", stages.LANDING_ZONE, 5, {}),
        ("cleansed", stages.CLEANSED_DATA, 12, {}), ("applied", stages.APPLIED, 1, {}))}
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        session.add_all(made.values())
        await session.flush()
        await rank_roles(session, {made["second"].id: 90, made["first"].id: 85, made["low"].id: 30})
        (ranked,) = await role_row.columns(session, OWNER, stages.RANKED)
        (work,) = await role_row.columns(session, OWNER, stages.WORK)
        second_page = (await role_row.columns(session, OWNER, stages.RANKED, page=2, size=3))[0]
        board_keys = [c["key"] for c in await role_row.columns(session, OWNER)]
        counts = await live.counts(session, OWNER)
    assert (ranked["key"], ranked["label"], ranked["count"]) == ("ranked", "Ranked list", 4)
    assert [(c["title"].removeprefix("Role "), c["status"], c["score"]) for c in ranked["cards"]] == [
        ("second", _SECOND, 90), ("first", _FIRST, 85), ("ready", _READY, 80), ("low", _FIRST, 30)], (
        "First Impression, Second Impression and Ready to Launch as one list, sorted by the number each card shows")
    assert (work["key"], work["label"], work["count"]) == ("work", "Work in progress", 3)
    assert [c["title"].removeprefix("Role ") for c in work["cards"]] == ["landing", "cleansed", "follow"], (
        "the lanes' own order: the younger posting first")
    assert (second_page["page"], second_page["pages"], [c["title"] for c in second_page["cards"]]) == (
        2, 2, ["Role low"])
    assert "ranked" not in board_keys and "work" not in board_keys, "the board's lanes are as they were"
    assert counts["groups"] == {"work": 3, "ranked": 4}
    assert counts["lanes"][stages.FIRST_IMPRESSION] == 2, "the lanes are counted as before"


async def test_the_board_route_gives_each_group_as_a_column_by_its_key(client):
    async with db.session_factory()() as session:
        session.add_all([_role("a", _FIRST), _role("b", stages.CLEANSED_DATA)])
        await session.commit()
    ranked = (await client.get("/api/v1/board", params={"column": "ranked"})).json()["columns"]
    work = (await client.get("/api/v1/board", params={"column": "work"})).json()["columns"]
    assert [(c["key"], [x["title"] for x in c["cards"]]) for c in ranked] == [("ranked", ["Role a"])]
    assert [(c["key"], [x["title"] for x in c["cards"]]) for c in work] == [("work", ["Role b"])]
    assert (await client.get("/api/v1/board", params={"column": "nothing"})).status_code == 404


async def test_a_page_costs_the_same_statements_for_three_roles_or_thirty(js):
    async def statements(count: int) -> dict[str, int]:
        async with db.session_factory()() as session:
            session.add_all([_role(f"{count}-{n}-{status}", status, comp="$90,000 - $120,000")
                             for n in range(count) for status in (_SECOND, _READY, stages.APPLIED)])
            await session.flush()
            one = await session.scalar(select(Opportunity.id).where(Opportunity.slug.like(f"{count}-%")).limit(1))
            seen: list[str] = []

            def noted(_conn, _cursor, statement, _parameters, _context, _many):
                seen.append(statement)

            engine = db.engine().sync_engine
            out = {}
            for name, ask in (("queue lane", lambda: role_row.columns(session, OWNER, _SECOND)),
                              ("other lane", lambda: role_row.columns(session, OWNER, stages.APPLIED)),
                              ("every lane", lambda: role_row.columns(session, OWNER)),
                              ("one row", lambda: role_row.row(session, OWNER, one))):
                seen.clear()
                event.listen(engine, "before_cursor_execute", noted)
                try:
                    await ask()
                finally:
                    event.remove(engine, "before_cursor_execute", noted)
                out[name] = len(seen)
            await session.rollback()
            return out

    few, many = await statements(3), await statements(30)
    assert few == many, f"3 roles cost {few} statements, 30 cost {many}"


async def test_the_board_holds_no_transaction_while_it_shapes_a_lane(js, monkeypatch):
    """A lane's cards are made from what its reads brought, with the transaction ended: a request that held one
    connection through its reads would starve the others."""
    await _spread()
    async with db.session_factory()() as session:
        session.add(JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats="owner", ext_id="w1", company=ADDED_LINK, title="Posting 1",
            match_key="firm 1|posting 1", location="Remote", work_mode="remote", url="https://jobs.example.test/1",
            first_seen=utc_now(), last_seen=utc_now(), filtered_at=utc_now(), links=[], route="roles"))
        await session.commit()
    open_while_shaping: list[tuple[str, bool]] = []
    shape, shape_posting = role_row._shape, role_row._shape_posting

    def noting(name, real):
        def shaped(*args):
            open_while_shaping.append((name, db.in_transaction()))
            return real(*args)

        return shaped

    monkeypatch.setattr(role_row, "_shape", noting("a role", shape))
    monkeypatch.setattr(role_row, "_shape_posting", noting("an added link", shape_posting))
    async with db.session_factory()() as session:
        columns = await role_row.columns(session, OWNER)
    assert sum(len(c["cards"]) for c in columns) >= 15, "every lane that holds cards was shaped"
    assert {name for name, _ in open_while_shaping} == {"a role", "an added link"}
    assert [name for name, held in open_while_shaping if held] == [], "a transaction was open while a card was made"
