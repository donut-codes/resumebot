"""Reads that took a turn or many rows for nothing. Every name and address here is made up."""

import pytest
from sqlalchemy import select

from app import db
from app.jobsearch.browser import items as browser_items
from app.jobsearch.employers import follow
from app.jobsearch.fit import classifier
from app.jobsearch.gating import sift
from app.jobsearch.models import JobPosting
from app.sites import auto_watch, where
from app.sites.models import AVOID, SEARCH, Site
from app.timeutil import utc_now
from tests.builders import browser_world
from tests.jobsearch.test_board_loads import statements
from tests.jobsearch.test_browser_code import CODE, _role, no_browser  # noqa: F401 (autouse)
from tests.jobsearch.test_browser_code import NOW as CODE_NOW
from tests.jobsearch.test_sift_slices import NOW, _a_hundred_postings
from tests.sites.test_auto_watch import _employer, _setting, _site
from tests.sites.test_auto_watch import _role as _watch_role
from tests.support import DEFAULT_OWNER, OWNER, load_profile

BOARDONE = "https://www.boardone.test/jobs/view/{n}"  # a made-up board's posting address


def _a_site(host: str, kind: str, *, path: str = "/", owner: str = OWNER, list_name: str = SEARCH) -> Site:
    return Site(owner=owner, url=f"https://{host}.example.test{path}", kind=kind, list_name=list_name,
                created_at=utc_now())


async def test_the_job_board_hosts_come_back_in_one_row(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        session.add_all([
            _a_site("boards", "job_board", path="/jobs"),
            _a_site("assoc", "association", path="/jobs"),
            _a_site("acme", "employer"),
            _a_site("hooli", "employer"),
            _a_site("hooli", "job_board", path="/other"),  # a host that is also an employer's own: never a board
            _a_site("elsewhere", "job_board", list_name=AVOID),
            _a_site("theirs", "job_board", owner="someone-else"),
        ])
        await session.commit()
    async with db.session_factory()() as session:
        with statements() as seen:
            hosts = await follow.job_board_hosts(session, OWNER)
        await session.rollback()
    assert hosts == frozenset({"boards.example.test", "assoc.example.test"})
    assert len([q for q in seen if "FROM js_sites" in q]) == 1
    assert any("json_group_array" in q for q in seen)


async def test_a_filter_trains_the_classifier_without_holding_the_writer_turn(js, monkeypatch):
    profile = await _a_hundred_postings()
    classifier.clear_cache()
    real = classifier.examples
    got: list[bool] = []

    async def examples(session, owner):
        async with db.session_factory()() as other:  # NestedWriter while this task still holds the writer's turn
            await other.execute(select(1))
            await other.rollback()
        got.append(True)
        return await real(session, owner)

    monkeypatch.setattr(classifier, "examples", examples)
    async with db.session_factory()() as session:
        result = await sift.sift(session, OWNER, profile, now=NOW)
        await session.commit()
    assert got, "the model was never trained"
    assert result["looked_at"] == 100
    async with db.session_factory()() as session:
        assert len((await session.scalars(select(JobPosting).where(JobPosting.filtered_at.is_not(None)))).all()) == 100


@pytest.mark.usefixtures("the_queue_is_made_before_a_hand_out")
async def test_the_code_workers_look_for_an_item_is_a_readers_and_the_item_is_still_leased(js, monkeypatch):
    await browser_world(extra_sites=False)
    await _role(1, url=BOARDONE.format(n=4001))
    real = browser_items._look_for_code
    seen: list[tuple[bool, bool]] = []  # (a reader's session, the writer's turn held)

    async def look(session, owner, now):
        result = await real(session, owner, now)
        seen.append((db.is_reader(session), db._HOLDS in session.info))
        return result

    monkeypatch.setattr(browser_items, "_look_for_code", look)
    async with db.session_factory()() as session:
        held = await browser_items.code_take(session, DEFAULT_OWNER, CODE, CODE_NOW)
        await session.commit()
    assert seen == [(True, False)]
    assert held is not None and (held.state, held.leased_by) == ("leased", CODE)


async def _an_employer_with_a_role_in_play_and_an_unwatched_site(*, setting_on: bool) -> None:
    async with db.session_factory()() as session:
        await _setting(session, False)
        acme = await _employer(session, "Acme")
        await _watch_role(session, acme)
        await where.seed_employer_sites(session, OWNER)  # the site is made, and not watched: the setting is off
        if setting_on:
            await _setting(session, True)
        await session.commit()


async def test_settling_the_watch_with_nothing_to_switch_takes_no_turn(js):
    await _an_employer_with_a_role_in_play_and_an_unwatched_site(setting_on=False)
    async with db.session_factory()() as session:
        await where.settle_watch(session, OWNER)
        assert not session.in_transaction(), "nothing to switch: the plan was read apart and the session untouched"


async def test_settling_the_watch_still_switches_on_an_unwatched_site_of_an_employer_in_play(js):
    await _an_employer_with_a_role_in_play_and_an_unwatched_site(setting_on=True)
    async with db.session_factory()() as session:
        assert [name for _, name, wanted in await auto_watch.plan(session, OWNER) if wanted] == ["Acme"]
        await session.rollback()
    async with db.session_factory()() as session:
        await where.settle_watch(session, OWNER)
        await session.commit()
    async with db.session_factory()() as session:
        acme = await _employer(session, "Acme")
        assert (await _site(session, acme)).on is True
        assert await auto_watch.plan(session, OWNER) == []
