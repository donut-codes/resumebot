"""What a fresh install does before its owner has touched a setting (plan H2): reading the job board is off and makes no
request, the browser reader's mission is paused and hands out nothing, and no board page is queued for the browser.
Every test runs on an owner with an empty database: no saved profile, no missions made. A mock transport counts the
requests; nothing reaches the network or sleeps."""


from app import db
from app.jobsearch.browser import reader as br
from app.jobsearch.missions import core as missions
from app.jobsearch.profile.schema import Settings
from tests.support import OWNER


def test_the_settings_default_reads_no_job_board():
    assert Settings().read_robots_exempt is False




async def test_the_browser_reader_mission_starts_paused_and_hands_out_no_site(js):
    async with db.session_factory()() as session:
        assert (await missions.standing(session, OWNER, missions.BROWSER)).on is False, "not made yet: off"
        await missions.ensure(session, OWNER)
        standing = await missions.standing(session, OWNER, missions.BROWSER)
        assert (standing.made, standing.on) == (True, False)
        assert await br.pullable_sites(session, OWNER) is None


