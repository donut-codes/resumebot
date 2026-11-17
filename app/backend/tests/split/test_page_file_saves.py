"""The page saves an instruction file and restores an earlier version: it works out the room
beside the file from the process versions the database holds, with no process library and no registration of the
engine's actions, so it imports nothing of the engine. The reply is read on a reader after the commit. Every name here
is made up."""

import pytest
from httpx import ASGITransport, AsyncClient
from process_engine import versions
from process_engine.models import ProcVersion

from app import db, notice, owner_keys
from app.agents import how_routes
from app.agents import how_they_work as how
from app.jobsearch.work import engine_calls
from app.settings import settings
from tests.split.test_page_writes import imports_recorded, readers_of
from tests.split.writes_support import on_programs
from tests.support import OWNER

ROWS = {"PUT /api/v1/harness/files/{kind}/{name}", "POST /api/v1/harness/files/{kind}/{name}/restore"}
FILE = "/api/v1/harness/files/instructions/jobsearch.fixer"
ANALYSE = "jobsearch_analyse"
ANALYST, ANALYSIS, PACKET = "jobsearch.analyst", "jobsearch.analysis", "jobsearch.packet"
OLD_PAIR = 'porter:instructions="jobsearch.analyst" porter:template="jobsearch.analysis"'
NEW_PAIR = 'porter:instructions="jobsearch.analyst" porter:template="jobsearch.packet"'


@pytest.fixture
def rang(monkeypatch):
    """The engine's door is the page's real one (`Remote`); each bell it rings is kept instead of sent."""
    kinds: list[str] = []
    monkeypatch.setattr(notice, "ring_engine", lambda kind, **fields: kinds.append(kind) or True)
    before = engine_calls._backend
    engine_calls.install(engine_calls.Remote())
    yield kinds
    engine_calls.install(before)


@pytest.fixture
async def page_http(js, monkeypatch, rang):
    """The page program's app over ASGI as `http://test`, acting for the owner `OWNER`."""
    from app.jobsearch import page

    monkeypatch.setattr(settings, "owner", OWNER)
    async with AsyncClient(transport=ASGITransport(app=page.app), base_url="http://test") as http:
        yield http


async def write(http, method: str, url: str, **request):
    """One call of the page app with the imports recorded: (the reply, the forbidden modules it imported)."""
    with imports_recorded() as reached:
        reply = await http.request(method, url, **request)
    return reply, sorted(reached)


async def _stored(xml: str, number: int) -> None:
    """A version of the analyse process saved in the database; this program's process library is not told."""
    async with db.session_factory()() as session:
        session.add(ProcVersion(
            entity_id=owner_keys.entity_id(), process_key=ANALYSE, number=number,
            file_hash=versions.file_hash_of(xml), xml=xml, kind="edit", wording="Say it plainly",
        ))
        await session.commit()


def test_the_page_serves_the_file_save_and_restore_and_the_engine_does_not():
    missing, still_there = on_programs(ROWS)
    assert missing == [], "rows the page does not serve"
    assert still_there == [], "rows the engine still serves"


async def test_saving_and_restoring_a_file_on_the_page_imports_nothing_of_the_engine_and_never_asks_the_library(
        page_http, monkeypatch):
    async def never(session, entity_id):
        raise AssertionError("the process library was asked for the owner's versions")

    monkeypatch.setattr(versions, "current_for", never)
    reply_on_a_reader = readers_of(monkeypatch, how_routes, "_file_reply")
    first, imported_a = await write(page_http, "PUT", FILE, json={"text": "Mine, short.", "why": "a test edit"})
    assert first.status_code == 200, first.text
    wanted = max(v["version"] for v in first.json()["history"]["versions"])
    second, imported_b = await write(page_http, "PUT", FILE, json={"text": "Mine, shorter.", "why": "again"})
    assert second.status_code == 200, second.text
    back, imported_c = await write(page_http, "POST", f"{FILE}/restore", json={"version": wanted})
    assert back.status_code == 200, back.text
    assert back.json()["file"]["text"] == "Mine, short."
    assert imported_a == [] and imported_b == [] and imported_c == []
    assert reply_on_a_reader == [True, True, True], "each reply is read on a reader, after the commit"


async def test_the_pair_is_read_from_the_newest_stored_version_and_not_from_the_process_library(js, monkeypatch):
    async def never(session, entity_id):
        raise AssertionError("the process library was asked for the owner's versions")

    monkeypatch.setattr(versions, "current_for", never)
    shipped = versions.shipped_xml(ANALYSE)
    assert OLD_PAIR in shipped
    async with db.read_session_factory()() as session:
        assert await how.paired_names_stored(session, "instructions", ANALYST) == [ANALYSIS], "nothing stored: shipped"
    await _stored(shipped.replace(OLD_PAIR, NEW_PAIR), number=2)
    async with db.read_session_factory()() as session:
        assert await how.paired_names_stored(session, "instructions", ANALYST) == [ANALYSIS, PACKET]
    await _stored(shipped, number=3)  # the newest version wins, not any version
    async with db.read_session_factory()() as session:
        assert await how.paired_names_stored(session, "instructions", ANALYST) == [ANALYSIS]


async def test_a_file_no_step_names_has_no_pair_and_a_name_no_file_has_is_left_out(js):
    async with db.read_session_factory()() as session:
        assert await how.paired_names_stored(session, "instructions", "jobsearch.scoring") == []
        assert await how.paired_names_stored(session, "instructions", "nothing") == []
        assert await how.paired_names_stored(session, "instructions", "jobsearch.fixer") == ["jobsearch.fix"]
        assert await how.paired_names_stored(session, "templates", "jobsearch.fix") == ["jobsearch.fixer"]
