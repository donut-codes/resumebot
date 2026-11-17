"""The tab register begins its close tasks through the engine door by name (`tabs_close` for a list of tabs,
`tabs_roles` for roles that left Ready to Launch), not with a `create_task` of its own (`browser/tabs.py`). Where the
local door is installed the tabs close as they always did (tests/jobsearch/test_tab_register.py pins that in full)."""

from app import db
from app.jobsearch import stages
from app.jobsearch.browser import tabs as tab_register
from app.jobsearch.models import Opportunity
from tests.support import OWNER


async def test_close_soon_closes_the_tabs_through_the_local_door(tests_never_reach_the_browser_bridge):
    tab_register.close_soon(["T1", "T2"])
    await tab_register.settle()
    closes = [body["targets"] for path, body in tests_never_reach_the_browser_bridge if path == "/tabs/close"]
    assert closes == [["T1", "T2"]]


async def test_close_soon_with_a_recording_door_keeps_one_start_and_closes_nothing(
        tests_never_reach_the_browser_bridge, fake_starts):
    tab_register.close_soon(["T1", "", "T2"])
    await tab_register.settle()
    assert fake_starts.starts == [("tabs_close", ["T1", "T2"])]
    assert tests_never_reach_the_browser_bridge == []


async def test_close_soon_with_no_tab_asks_for_nothing(fake_starts):
    tab_register.close_soon([""])
    assert fake_starts.starts == []


async def test_a_role_leaving_ready_to_launch_asks_by_name_for_its_tabs_to_close(js, monkeypatch, fake_starts):
    # the departures hook looks only once a Launch tab was registered
    monkeypatch.setattr(tab_register, "_go_rows", True)
    async with db.session_factory()() as session:
        role = Opportunity(owner=OWNER, slug="role-1", company="Fabrikam 1", title="Lead",
                           status=stages.READY_TO_LAUNCH, match_key="fabrikam1|lead")
        session.add(role)
        await session.commit()
        role_id = role.id
    async with db.session_factory()() as session:
        (await session.get(Opportunity, role_id)).status = stages.APPLIED
        await session.commit()
    assert fake_starts.starts == [("tabs_roles", (OWNER, {role_id}))]
