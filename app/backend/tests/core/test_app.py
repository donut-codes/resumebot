"""The resumebot API: the board, export and health, over HTTP (synthetic roles, its own database)."""

import io
import re
from datetime import UTC, date, datetime, timedelta

import openpyxl

from app import db
from app.jobsearch.models import Opportunity
from app.settings import APP_NAME, VERSION
from tests.builders import rank_roles
from tests.support import pin_clock

#: The day the board is read on. Pinned (the test below sets the app's clock to noon of it), so the roles' ages never
#: depend on the day or the zone the suite runs in.
TODAY = date(2026, 10, 14)
NOON_TODAY = datetime(TODAY.year, TODAY.month, TODAY.day, 12, tzinfo=UTC)

SOURCED = datetime(2026, 10, 1, tzinfo=UTC)  # the green check: a sourced role is on deck, in Second Impression


def role(slug, company, title, fit, days_old, **extra):
    return Opportunity(
        owner="tester", slug=slug, company=company, title=title, fit=fit, origin="scout",
        match_key=f"{company}|{title}".lower(), posted=TODAY - timedelta(days=days_old), found=TODAY,
        fit_evidence=f"evidence for {title}", url=f"https://x.test/{slug}", **{"status": "second_impression", **extra})


async def test_health_names_the_app_and_its_version(client):
    body = (await client.get("/health")).json()
    assert body == {"app": "resumebot", "status": "ok", "version": VERSION, "db": "ok"}
    assert APP_NAME == "resumebot"


def test_the_version_is_the_one_in_pyproject():
    # A release bumps backend/pyproject.toml, frontend/package.json and settings.VERSION together; this catches a
    # missed one on the backend side, so no test has to name the number.
    pyproject = (db.BACKEND_DIR / "pyproject.toml").read_text()
    assert re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1) == VERSION


async def test_the_board_lists_found_roles_by_fit_and_gated_out_with_reasons(
    client, monkeypatch,
):
    # The app's one clock, not one module's reading of it: the board's request takes its day from `owner_today()` and
    # its instant from `utc_now()`, and the stored order the lanes are read in is worked out from that instant
    # (`stored_order.refresh`: the day is the owner's day of it). With only the day of `pipeline/row.py` set, the order
    # was worked out on the real day, and from 2026-11-11 the three-day-old role was over 30 days old.
    pin_clock(monkeypatch, NOON_TODAY)
    async with db.session_factory()() as session:
        # Second Impression is read in the pipeline's order: the first-impression rank, plus the first-72-hours bonus,
        # less 15 for a posting over 30 days old.
        stale, fresh4, fresh2 = (role("stale5", "Stale Co", "Stale Five", 5, 40, sourced_at=SOURCED),
                                 # 72 hours old: not in the first 72
                                 role("fresh4", "Fresh Co", "Fresh Four", 4, 3, sourced_at=SOURCED),
                                 role("fresh2", "Low Co", "Fresh Two", 2, 1, sourced_at=SOURCED))
        session.add_all([stale, fresh4, fresh2])
        await session.flush()
        await rank_roles(session, {stale.id: 95, fresh4.id: 90, fresh2.id: 50})  # 80 (stale), 90, 70 (a day old: +20)
        session.add_all([
            Opportunity(owner="tester", slug="x", company="Globex", title="Dropped VP", status="gated_out",
                        match_key="globex|dropped vp", origin="scout", url="https://x.test/g",
                        screened_out_reason="excluded employer"),
        ])
        await session.commit()
    columns = {c["key"]: c for c in (await client.get("/api/v1/board")).json()["columns"]}
    found = columns["second_impression"]["cards"]
    assert [r["title"] for r in found] == ["Fresh Four", "Stale Five", "Fresh Two"]
    assert columns["closed"]["count"] == 1
    dropped = columns["closed"]["cards"][0]
    assert dropped["title"] == "Dropped VP" and dropped["reason"] == "excluded employer"


async def test_the_export_is_an_xlsx_in_the_trackers_column_order(client):
    async with db.session_factory()() as session:
        session.add(role("a", "Acme", "Lead", 4, 2))
        await session.commit()
    res = await client.get("/export.xlsx")
    assert res.status_code == 200 and "spreadsheetml" in res.headers["content-type"]
    assert "attachment" in res.headers["content-disposition"]
    sheet = openpyxl.load_workbook(io.BytesIO(res.content))["Tracker"]
    assert [c.value for c in sheet[1]][:4] == ["id", "company", "title", "status"]
    assert sheet[2][1].value == "Acme"


async def test_the_runners_heartbeat_needs_the_scouts_token(client):
    assert (await client.post("/api/v1/agent/heartbeat", json={"state": "alive"})).status_code == 401
