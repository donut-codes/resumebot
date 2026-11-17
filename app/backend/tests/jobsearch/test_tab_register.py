"""The tab register (docs/PRD.md path 52): every tab the app's Chrome has open is a row, and cleanup follows the rows.
The bridge is never reached: `tests_never_reach_the_browser_bridge` (conftest) keeps what the app asks of it."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch import stages
from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser import tabs as tab_register
from app.jobsearch.models import OpenTab, Opportunity
from app.jobsearch.work import housekeeping, session_lifecycle
from tests.support import OWNER


@pytest.fixture(autouse=True)
def the_app_acts_for_the_test_owner(monkeypatch):
    from app.settings import settings

    monkeypatch.setattr(settings, "owner", OWNER)


@pytest.fixture
def asks(tests_never_reach_the_browser_bridge):
    return tests_never_reach_the_browser_bridge


async def _role(status: str = stages.READY_TO_LAUNCH, n: int = 1) -> uuid.UUID:
    async with db.session_factory()() as session:
        role = Opportunity(owner=OWNER, slug=f"role-{n}", company=f"Fabrikam {n}", title="Lead", status=status,
                           match_key=f"fabrikam{n}|lead")
        session.add(role)
        await session.commit()
        return role.id


async def _register(target: str, kind: str, owner_id: str, role_id=None, at: datetime | None = None) -> None:
    async with db.session_factory()() as session:
        await tab_register.add(session, OWNER, target, kind, owner_id, role_id, now=at)
        await session.commit()


async def _targets() -> set[str]:
    async with db.session_factory()() as session:
        return {t.target_id for t in await tab_register.tabs(session, OWNER)}


def _closed(asks) -> list[str]:
    return [t for path, body in asks if path == "/tabs/close" for t in body["targets"]]


# --- the rows ---------------------------------------------------------------------------------------------------


async def test_a_tab_is_a_row_with_its_owner_kind_and_time_and_a_second_write_of_it_keeps_one(js):
    await _register("T1", "session", "reader-aa")
    await _register("T1", "session", "reader-bb")
    async with db.session_factory()() as session:
        rows = await tab_register.tabs(session, OWNER)
    assert [(r.target_id, r.owner_id, r.kind) for r in rows] == [("T1", "reader-bb", "session")]
    assert rows[0].opened_at is not None


async def test_a_kind_that_is_not_a_kind_is_refused(js):
    with pytest.raises(ValueError):
        await _register("T1", "window", "x")


async def test_closing_takes_the_tab_off_the_register_and_a_tab_never_registered_is_no_error(js):
    await _register("T1", "code", "code:read")
    await tab_register.closed("T1")
    await tab_register.closed("never")
    assert await _targets() == set()


# --- the routes the bridge calls ----------------------------------------------------------------------------------


async def test_the_bridge_registers_a_session_tab_lists_the_register_and_removes_it(js, client, scout_token):
    posted = await client.post("/api/v1/agent/browser/tabs", headers=scout_token,
                               json={"target_id": "T9", "owner_id": "reader-1", "kind": "session"})
    assert posted.status_code == 200
    listed = (await client.get("/api/v1/agent/browser/tabs", headers=scout_token)).json()["tabs"]
    assert [(t["target_id"], t["owner_id"], t["kind"]) for t in listed] == [("T9", "reader-1", "session")]
    gone = await client.delete("/api/v1/agent/browser/tabs/T9", headers=scout_token)
    assert gone.json() == {"ok": True, "removed": True}
    assert await _targets() == set()


async def test_the_register_routes_need_the_agent_token_and_a_tab_needs_its_owner(js, client, scout_token):
    assert (await client.get("/api/v1/agent/browser/tabs")).status_code == 401
    unsigned = await client.post("/api/v1/agent/browser/tabs", json={"target_id": "T", "owner_id": "x"})
    assert unsigned.status_code == 401
    assert (await client.delete("/api/v1/agent/browser/tabs/T")).status_code == 401
    assert (await client.post("/api/v1/agent/browser/tabs/reconcile", json={})).status_code == 401
    no_owner = await client.post("/api/v1/agent/browser/tabs", headers=scout_token, json={"target_id": "T"})
    assert no_owner.status_code == 422
    assert (await client.post("/api/v1/agent/browser/tabs", headers=scout_token,
                              json={"target_id": "T", "owner_id": "x", "kind": "window"})).status_code == 422


async def test_a_sessions_connection_going_releases_its_rows_and_names_nothing_to_close(js, client, scout_token,
                                                                                    asks):
    await _register("A", "session", "reader-1")
    await _register("B", "session", "reader-1")
    await _register("C", "session", "reader-2")
    released = await client.post("/api/v1/agent/browser/tabs/release", headers=scout_token,
                                 json={"owner_id": "reader-1"})
    assert released.json() == {"ok": True, "released": 2}
    assert await _targets() == {"C"}
    assert asks == []


# --- a session ends ---------------------------------------------------------------------------------------------


async def test_a_session_that_ends_has_its_tabs_off_the_register_and_closed_and_no_one_elses(js, asks):
    await _register("A", "session", "reader-1")
    await _register("B", "session", "reader-1")
    await _register("C", "session", "reader-2")
    async with db.session_factory()() as session:
        await session_lifecycle.settle(session, OWNER, {"session_id": "reader-1", "state": "ended"})
        await session.commit()
    await tab_register.settle()
    assert await _targets() == {"C"}
    assert sorted(_closed(asks)) == ["A", "B"]


async def test_a_session_that_is_alive_keeps_its_tabs(js, asks):
    await _register("A", "session", "reader-1")
    async with db.session_factory()() as session:
        await session_lifecycle.settle(session, OWNER, {"session_id": "reader-1", "state": "alive"})
        await session.commit()
    await tab_register.settle()
    assert await _targets() == {"A"}
    assert _closed(asks) == []


# --- a Launch tab's role leaves Ready to Launch -------------------------------------------------------------------


async def _move(role_id: uuid.UUID, status: str) -> None:
    async with db.session_factory()() as session:
        (await session.get(Opportunity, role_id)).status = status
        await session.commit()
    await tab_register.settle()


@pytest.mark.parametrize("status", [stages.APPLIED, stages.GONE_QUIET, stages.FIRST_IMPRESSION, stages.GATED_OUT])
async def test_a_launch_tab_closes_when_its_role_leaves_ready_to_launch_whichever_way(js, asks, status):
    role = await _role()
    other = await _role(n=2)
    await _register("G1", "go", "go", role)
    await _register("G2", "go", "go", other)
    await _move(role, status)
    assert await _targets() == {"G2"}
    assert _closed(asks) == ["G1"]


async def test_a_role_that_moves_and_comes_back_in_one_transaction_keeps_its_tab(js, asks):
    role = await _role()
    await _register("G1", "go", "go", role)
    async with db.session_factory()() as session:
        held = await session.get(Opportunity, role)
        held.status = stages.APPLIED
        await session.flush()
        held.status = stages.READY_TO_LAUNCH
        await session.commit()
    await tab_register.settle()
    assert await _targets() == {"G1"}
    assert _closed(asks) == []


async def test_a_role_that_stays_in_ready_to_launch_keeps_its_tab(js, asks):
    role = await _role()
    await _register("G1", "go", "go", role)
    async with db.session_factory()() as session:
        (await session.get(Opportunity, role)).title = "Director"
        await session.commit()
    await tab_register.settle()
    assert await _targets() == {"G1"}


# --- what the register itself says is over (the housekeeping pass) -------------------------------------------------


async def test_a_launch_tab_a_day_old_goes_and_a_fresh_one_in_a_ready_role_stays(js, asks):
    now = datetime.now(UTC)
    old, fresh = await _role(n=1), await _role(n=2)
    await _register("OLD", "go", "go", old, at=now - timedelta(hours=25))
    await _register("NEW", "go", "go", fresh, at=now - timedelta(hours=23))
    async with db.session_factory()() as session:
        assert await tab_register.expired(session, OWNER, now) == 1
        await session.commit()
    await tab_register.settle()
    assert await _targets() == {"NEW"}
    assert _closed(asks) == ["OLD"]


async def test_a_launch_tab_whose_role_is_not_ready_is_closed_by_the_pass_when_the_move_was_missed(js, asks):
    role = await _role(status=stages.APPLIED)
    await _register("G1", "go", "go", role)
    async with db.session_factory()() as session:
        await housekeeping.settle(session, OWNER)
        await session.commit()
    await tab_register.settle()
    assert await _targets() == set()
    assert _closed(asks) == ["G1"]


async def test_a_session_or_code_tab_past_its_backstop_goes_and_a_young_one_stays(js, asks):
    now = datetime.now(UTC)
    await _register("S-OLD", "session", "reader-1", at=now - timedelta(hours=tab_register.TAB_MAX_HOURS + 1))
    await _register("C-OLD", "code", "code:read", at=now - timedelta(hours=tab_register.TAB_MAX_HOURS + 1))
    await _register("S-NEW", "session", "reader-2", at=now - timedelta(minutes=5))
    async with db.session_factory()() as session:
        await tab_register.expired(session, OWNER, now)
        await session.commit()
    await tab_register.settle()
    assert await _targets() == {"S-NEW"}
    assert sorted(_closed(asks)) == ["C-OLD", "S-OLD"]


# --- a start: the register is what stays ---------------------------------------------------------------------------


async def test_at_the_bridges_start_no_session_or_code_job_is_connected_so_only_launch_tabs_in_ready_roles_stay(js):
    ready, applied = await _role(n=1), await _role(status=stages.APPLIED, n=2)
    await _register("S", "session", "reader-1")
    await _register("C", "code", "code:read")
    await _register("G-OK", "go", "go", ready)
    await _register("G-OVER", "go", "go", applied)
    async with db.session_factory()() as session:
        keep = await tab_register.keep_list(session, OWNER, reason="bridge")
        await session.commit()
    assert keep == ["G-OK"]
    assert await _targets() == {"G-OK"}


async def test_at_the_apps_start_the_code_jobs_are_gone_but_live_sessions_keep_their_tabs(js, asks, monkeypatch):
    ready = await _role()
    await _register("S", "session", "reader-1")
    await _register("C", "code", "code:boardone")
    await _register("G", "go", "go", ready)
    await _register("GONE", "session", "reader-2")

    async def bridge_post(path, body):
        asks.append((path, body))
        return {"closed": 3, "gone": ["GONE"]}

    monkeypatch.setattr(tab_register, "bridge_post", bridge_post)
    await tab_register.reconcile_at_app_start(OWNER)
    reconcile = [body for path, body in asks if path == "/tabs/reconcile"]
    assert len(reconcile) == 1 and sorted(reconcile[0]["keep"]) == ["G", "GONE", "S"]
    assert await _targets() == {"S", "G"}, "the code job's row goes, and the row of the tab the bridge found gone"


async def test_a_bridge_that_is_not_there_at_the_apps_start_changes_nothing_but_the_code_rows(js, monkeypatch):
    await _register("S", "session", "reader-1")

    async def none(path, body):
        return None

    monkeypatch.setattr(tab_register, "bridge_post", none)
    assert await tab_register.reconcile_at_app_start(OWNER) is None
    assert await _targets() == {"S"}


async def test_the_bridges_start_route_answers_the_tabs_to_keep(js, client, scout_token):
    ready = await _role()
    await _register("S", "session", "reader-1")
    await _register("G", "go", "go", ready)
    answered = await client.post("/api/v1/agent/browser/tabs/reconcile", headers=scout_token, json={})
    assert answered.json() == {"keep": ["G"]}


# --- the app's own code registers the tabs it opens ---------------------------------------------------------------


class _Wire:
    """A `_DevTools` whose connection is a script: each command is answered the way the bridge would."""

    def __init__(self, tools: browser_code._DevTools, next_target: str = "T1") -> None:
        self.said: list[tuple[str, dict]] = []
        self.next_target = next_target

        async def call(method, params=None, session_id=None):
            self.said.append((method, params or {}))
            return {"targetId": self.next_target} if method == "Target.createTarget" else {}

        tools._call = call  # pyright: ignore[reportAttributeAccessIssue]


async def test_a_code_jobs_tab_is_registered_as_it_opens_and_removed_where_the_code_closes_it(js):
    tools = browser_code._DevTools(9333, "code:boardone")
    _Wire(tools)
    await tools.call("Target.createTarget", {"url": "about:blank"})
    async with db.session_factory()() as session:
        rows = await tab_register.tabs(session, OWNER)
    assert [(r.target_id, r.owner_id, r.kind, r.role_id) for r in rows] == [("T1", "code:boardone", "code", None)]
    await tools.call("Target.closeTarget", {"targetId": "T1"})
    assert await _targets() == set()


async def test_a_command_inside_a_tab_that_closes_nothing_leaves_the_register_alone(js):
    tools = browser_code._DevTools(9333, "code:read")
    _Wire(tools)
    await _register("T1", "code", "code:read")
    await tools.call("Runtime.evaluate", {"expression": "1"}, "SESSION")
    await tools.call("Target.closeTarget", {"targetId": "T1"}, "SESSION")
    assert await _targets() == {"T1"}


async def test_a_launch_tab_is_registered_with_its_role_in_one_write(js):
    role = await _role()
    window = browser_code.GoWindow()

    async def call(method, params=None, session_id=None):
        return {"targetId": "TG"} if method == "Target.createTarget" else {"sessionId": "S-TG"}

    window.tools._call = call  # pyright: ignore[reportAttributeAccessIssue]
    await window.tab("https://careers.example.test/jobs/1", role)
    async with db.session_factory()() as session:
        row = await session.scalar(select(OpenTab).where(OpenTab.target_id == "TG"))
    assert (row.kind, row.owner_id, row.role_id) == ("go", "go", role)
    assert window.tools.role_id is None, "the role is for that tab only"


async def test_a_register_that_cannot_be_written_never_stops_the_work(js, monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(tab_register, "add", broken)
    tools = browser_code._DevTools(9333, "code:read")
    _Wire(tools)
    assert await tools.call("Target.createTarget", {"url": "about:blank"}) == {"targetId": "T1"}


async def test_the_address_a_code_job_connects_to_names_it_so_the_bridge_leaves_its_registering_to_it(monkeypatch):
    sent: list[bytes] = []

    class Writer:
        def write(self, data):
            sent.append(data)

        async def drain(self):
            return None

    class Reader:
        async def readuntil(self, _sep):
            return b"HTTP/1.1 101 Switching Protocols\r\n\r\n"

    async def connect(host, port):
        return Reader(), Writer()

    monkeypatch.setattr(asyncio, "open_connection", connect)
    await browser_code._DevTools(9333, "code:boardone").open()
    await browser_code._DevTools(9333, "go", "go").open()
    assert sent[0].startswith(b"GET /devtools/browser/code.boardone HTTP/1.1")
    assert sent[1].startswith(b"GET /devtools/browser/go HTTP/1.1")
