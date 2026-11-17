"""What a role's row carries for the list (`role_row`): one `new` flag for a role in its first hours, whether Launch
opened it, and its packet's progress for a building bar. Synthetic roles only."""

from datetime import UTC, date, datetime, timedelta

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Opportunity, Packet
from tests.support import OWNER


def _role(slug, status=stages.READY_TO_LAUNCH, days_old=10, **extra):
    day = date.today() - timedelta(days=days_old)
    return Opportunity(owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}", status=status,
                       origin="tracker", match_key=f"co {slug}|role {slug}", posted=day, found=day, **extra)


async def test_a_row_says_if_it_is_new_and_launched(client):
    now = datetime.now(UTC)
    rows = {"opened": _role("opened", go_opened_at=now), "plain": _role("plain"), "fresh": _role("fresh", days_old=0)}
    async with db.session_factory()() as session:
        session.add_all(rows.values())
        await session.commit()

    board = (await client.get("/api/v1/board", params={"column": stages.READY_TO_LAUNCH})).json()
    cards = {c["title"]: c for c in board["columns"][0]["cards"]}
    assert cards["Role opened"]["opened_at"] is not None and cards["Role plain"]["opened_at"] is None
    assert cards["Role fresh"]["new"] is True and cards["Role plain"]["new"] is False


async def test_a_rows_packet_says_where_it_is_on_its_way(client):
    role = _role("p")
    async with db.session_factory()() as session:
        session.add(role)
        await session.flush()
        session.add(Packet(owner=OWNER, role_id=role.id, version=1, status="writing", feedback="", resume={},
                           letter={}, comp={}, reasoning={}, checks=[],
                           brief={"step": "checking"}))
        await session.commit()
    board = (await client.get("/api/v1/board", params={"column": stages.READY_TO_LAUNCH})).json()
    (card,) = board["columns"][0]["cards"]
    progress = card["packet"]["progress"]
    assert progress["step"] == "checking" and progress["label"] == "Its facts being checked"
    detail = (await client.get(f"/api/v1/roles/{role.id}")).json()
    assert detail["packets"][0]["progress"]["step"] == "checking"
