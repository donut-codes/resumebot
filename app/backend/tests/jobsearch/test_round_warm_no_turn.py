"""A finished round's warm-up read of the owner's roles index runs on a reader, after the report's own save (the owner,
2026-11-10: reading must not lock everything). The scout's last report staged its roles in the writer's session and went
straight on to finish the round, so the warm-up, which is there to keep the index's seconds out of the writer's turn,
ran under the turn the staging had just taken. The report now saves what it staged first, where its transaction is its
own to end; inside a caller's savepoint it reads in the caller's session, as before. Every name here is made up, and no
test reaches the network."""

from app import db
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import SearchRun
from app.jobsearch.postings import same_role
from tests.support import OWNER, load_profile, reported


def _spy_on_the_index(monkeypatch) -> list:
    """Every session `same_role.known_roles` is asked with from here on, in order: the first is the warm-up's."""
    asked: list = []
    real = same_role.known_roles

    async def known_roles(session, *a, **k):
        asked.append(session)
        return await real(session, *a, **k)

    monkeypatch.setattr(same_role, "known_roles", known_roles)
    return asked


async def test_the_warm_up_of_a_finished_round_reads_on_a_reader_once_the_report_is_saved(js, monkeypatch):
    asked = _spy_on_the_index(monkeypatch)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        started = await run.start_round(session, OWNER)
        await session.commit()
        run_id = started.id
        reply = await run.stage_report(session, started, [reported("Fabrikam", "Head of Delivery")], done=True)
        assert asked and db.is_reader(asked[0]), "the warm-up read took no writer's turn"
        assert asked[0] is not session
        await session.commit()
    assert (reply["staged"], reply["done"]) == (1, True)
    async with db.session_factory()() as session:
        assert (await session.get(SearchRun, run_id)).status == "done"


async def test_the_warm_up_inside_a_callers_savepoint_reads_in_the_callers_session(js, monkeypatch):
    asked = _spy_on_the_index(monkeypatch)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        started = await run.start_round(session, OWNER)
        await session.commit()
        run_id = started.id
        async with session.begin_nested():
            await run.stage_report(session, started, [reported("Fabrikam", "Head of Delivery")], done=True)
            assert asked and asked[0] is session, "a savepoint is part of the caller's whole: nothing is saved by half"
        await session.rollback()
    async with db.session_factory()() as session:
        row = await session.get(SearchRun, run_id)
        assert (row.status, row.reported) == ("open", []), "the caller's rollback still undoes the report whole"


async def test_a_round_another_writer_finished_during_the_warm_up_is_not_finished_or_failed_again(js, monkeypatch):
    """The warm-up reads with the writer's turn free, so the finish time can wind the same search down meanwhile
    (`wind_down`). The report then looks at the run again and returns what is stored: it completes nothing a second
    time and marks nothing failed."""
    real = same_role.known_roles
    others: list = []

    async def known_roles(session, *a, **k):
        if not others:  # the warm-up's read: another writer finishes the same search and saves, as the finish time does
            others.append(None)
            async with db.session_factory()() as other:
                others[0] = await run.wind_down(other, await other.get(SearchRun, run_id), "finish", "the finish time")
                await other.commit()
        return await real(session, *a, **k)

    monkeypatch.setattr(same_role, "known_roles", known_roles)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        started = await run.start_round(session, OWNER)
        await session.commit()
        run_id = started.id
        reply = await run.stage_report(session, started, [reported("Fabrikam", "Head of Delivery")], done=True)
        await session.commit()
    assert others == ["kept"], "the other writer finished the search during the warm-up"
    async with db.session_factory()() as session:
        row = await session.get(SearchRun, run_id)
        assert row.status == "done", "the search the other writer finished is not marked failed"
        assert (reply["staged"], reply["done"]) == (1, True)
        assert (reply["summary"], reply["counts"]) == (row.summary, row.counts)
        assert row.counts["stopped"] == "finish" and "Stopped: the finish time" in row.summary
