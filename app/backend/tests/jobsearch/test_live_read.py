"""Browser live messages built from rows (live_read.build): for each kind, the message built from the ids and the tags
equals the message the commit itself made (`live._collect`), which is what a page that did not make the change would
need. Synthetic rows only."""

import uuid
from datetime import UTC, date, datetime

import pytest

from app import db
from app.jobsearch import stages
from app.jobsearch.models import Employer, Opportunity, Packet, ProfileVersion
from app.jobsearch.pipeline import live, live_read, live_sink
from app.jobsearch.research import role_summary
from app.models.work import Notification
from app.sites.models import Site
from tests.support import OWNER, pin_clock

DAY = date(2030, 1, 1)


def _role(slug, status=stages.FIRST_IMPRESSION, **extra):
    return Opportunity(owner=OWNER, slug=slug, company=f"Co {slug}", title=f"Role {slug}", status=status,
                       origin="scout", match_key=f"co {slug}|role {slug}", found=DAY, **extra)


async def _add(*rows) -> list[uuid.UUID]:
    async with db.session_factory()() as session:
        session.add_all(rows)
        await session.commit()
        return [r.id for r in rows]


async def _sent(sub: live.Subscriber, kind: str) -> list[dict]:
    """What is queued now, of one kind (the queue is emptied)."""
    await live_sink.settle()
    out = []
    while not sub.queue.empty():
        _, got, data = sub.queue.get_nowait()
        if got == kind:
            out.append(data)
    return out


async def _built(kind: str, ids: list, tags: list) -> list[dict]:
    async with db.read_session_factory()() as session:
        return await live_read.build(session, OWNER, kind, [str(i) for i in ids], tags)


@pytest.fixture
def listening(js):
    sub = live.broker.subscribe(OWNER)
    yield sub
    live.broker.unsubscribe(sub)


def test_the_kinds_built_are_the_topics_a_stream_can_ask_for():
    assert live_read.KINDS == live.TOPICS


async def test_a_new_role_built_from_its_row_is_the_message_the_commit_made(listening):
    (role_id,) = await _add(_role("a"))
    (sent,) = await _sent(listening, "role")
    assert sent["changed"] == ["new"] and sent["from"] is None
    assert await _built("role", [role_id], [("new",)]) == [sent]


async def test_a_role_moved_and_a_role_starred_built_from_its_row_are_the_messages_the_commits_made(listening):
    (role_id,) = await _add(_role("b"))
    await _sent(listening, "role")
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        assert role is not None
        role.status, role.screened_out_reason = stages.GATED_OUT, "Pay below your floor"
        await session.commit()
    (moved,) = await _sent(listening, "role")
    assert moved["from"] == stages.FIRST_IMPRESSION and moved["to"] == stages.GATED_OUT
    tags = ("changed:status", f"was:{stages.FIRST_IMPRESSION}")
    assert await _built("role", [role_id], [tags]) == [moved]
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        assert role is not None
        role.favorited_at = datetime(2030, 1, 3, tzinfo=UTC)
        await session.commit()
    (starred,) = await _sent(listening, "role")
    assert starred["changed"] == ["favorite"] and starred["from"] == starred["to"]
    assert await _built("role", [role_id], [("changed:favorite",)]) == [starred]


async def test_a_summary_built_from_ids_is_the_message_the_commit_made(listening):
    (role_id,) = await _add(_role("c"))
    await _sent(listening, "summary")
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        assert role is not None
        role.analysis ={role_summary.SUMMARY_KEY: "A made-up summary."}
        await session.commit()
    (sent,) = await _sent(listening, "summary")
    assert await _built("summary", [role_id], [()]) == [sent]


async def test_a_packet_asked_and_moved_on_built_from_its_row_are_the_messages_the_commits_made(listening):
    (role_id,) = await _add(_role("d", stages.READY_TO_LAUNCH))
    async with db.session_factory()() as session:
        packet = Packet(owner=OWNER, role_id=role_id, version=1, status="writing", feedback="", brief={}, resume={},
                        letter={}, comp={}, reasoning={}, checks=[])
        session.add(packet)
        await session.commit()
        packet_id = packet.id
    (asked,) = await _sent(listening, "packet")
    assert asked["from"] is None and asked["progress"]["step"] == "asked"
    assert await _built("packet", [packet_id], [("new",)]) == [asked]
    async with db.session_factory()() as session:
        packet = await session.get(Packet, packet_id)
        assert packet is not None
        packet.status = "ready"
        await session.commit()
    (done,) = await _sent(listening, "packet")
    assert done["from"] == "writing" and done["progress"]["step"] == "done"
    assert await _built("packet", [packet_id], [("was:writing",)]) == [done]


async def test_the_counts_built_are_the_counts_of_the_rows(js):
    await _add(_role("e"), _role("f", stages.GATED_OUT))
    async with db.read_session_factory()() as session:
        counts = await live.counts(session, OWNER)
        assert await live_read.build(session, OWNER, "counts", [], []) == [counts]
    assert counts["lanes"]["closed"] == 1


async def test_a_row_that_is_gone_or_is_another_owners_builds_no_message(js):
    (role_id,) = await _add(_role("g"))
    assert await _built("role", [uuid.uuid4()], [("new",)]) == []
    async with db.read_session_factory()() as session:
        assert await live_read.build(session, "someone-else", "role", [str(role_id)], [("new",)]) == []


async def test_a_kind_that_is_no_topic_and_ids_without_tags_are_refused(js):
    async with db.read_session_factory()() as session:
        with pytest.raises(ValueError, match="no such kind"):
            await live_read.build(session, OWNER, "nonsense", [], [])
        with pytest.raises(ValueError, match="one tags tuple for each id"):
            await live_read.build(session, OWNER, "role", [str(uuid.uuid4())], [])


def _employer(name):
    """Every column the employer message reads is set, so the message the commit made holds all of them."""
    return Employer(owner=OWNER, key=name.lower(), name=name, aliases=[], local=True, industry="Robotics", employees=12,
                    gated=False, gated_reason=None, ats=None, board={}, board_url=None,
                    fetched_at=datetime(2030, 1, 1, tzinfo=UTC))


def _site(url):
    return Site(owner=OWNER, url=url, kind="job_board", on=True, created_at=datetime(2030, 1, 1, tzinfo=UTC),
                label="A made-up board", list_name="search", read_by="app")


def _version(kind="criteria", version=1, why="a made-up reason"):
    return ProfileVersion(owner=OWNER, kind=kind, version=version, data={}, saved_by="owner", why=why,
                          saved_at=datetime(2030, 1, 1, tzinfo=UTC))


async def test_a_task_raised_answered_and_removed_built_from_its_row_are_the_messages_the_commits_made(
        listening, monkeypatch):
    from app.inbox import service as inbox

    pin_clock(monkeypatch, datetime(2030, 1, 2, 12, 0, tzinfo=UTC))
    async with db.session_factory()() as session:
        task = await inbox.raise_task(session, OWNER, key="k-merge", title="Merge A into B?", kind="decision",
                                      options=[{"id": "merge", "label": "Merge them"}, {"id": "keep", "label": "Keep"}],
                                      recommended="merge")
        await session.commit()
        task_id = task.id
    (raised,) = await _sent(listening, "task")
    assert await _built("task", [task_id], [("new",)]) == [raised]
    async with db.session_factory()() as session:
        await inbox.answer(session, OWNER, task_id, "keep")
        await session.commit()
    (answered,) = await _sent(listening, "task")
    assert answered["state"] == "done" and answered["done_at"].endswith("+00:00")
    assert await _built("task", [task_id], [()]) == [answered]
    async with db.session_factory()() as session:
        await session.delete(await inbox.get_task(session, OWNER, task_id))
        await session.commit()
    (removed,) = await _sent(listening, "task")
    assert removed == {"id": str(task_id), "deleted": True}
    assert await _built("task", [task_id], [("deleted",)]) == [removed]


async def test_a_notification_raised_read_and_removed_built_from_its_row_are_the_messages_the_commits_made(
        listening, monkeypatch):
    from app.inbox import service as inbox

    pin_clock(monkeypatch, datetime(2030, 1, 2, 12, 0, tzinfo=UTC))
    async with db.session_factory()() as session:
        note = await inbox.notify(session, OWNER, key="k-note", kind="note", title="Something to know")
        await session.commit()
        note_id = note.id
    (raised,) = await _sent(listening, "notification")
    assert raised["was_read"] is None and await _built("notification", [note_id], [("new",)]) == [raised]
    async with db.session_factory()() as session:
        await inbox.mark_read(session, OWNER, None)
        await session.commit()
    (read,) = await _sent(listening, "notification")
    assert read["was_read"] is False
    assert await _built("notification", [note_id], [("was_read:no",)]) == [read]
    async with db.session_factory()() as session:
        await session.delete(await session.get(Notification, note_id))
        await session.commit()
    (removed,) = await _sent(listening, "notification")
    assert removed == {"id": str(note_id), "deleted": True}
    assert await _built("notification", [note_id], [("deleted",)]) == [removed]


async def test_an_employer_made_changed_and_removed_built_from_its_row_are_the_messages_the_commits_made(listening):
    async with db.session_factory()() as session:
        employer = _employer("Initech")
        session.add(employer)
        await session.commit()
        employer_id = employer.id
    (made,) = await _sent(listening, "employer")
    assert await _built("employer", [employer_id], [("new",)]) == [made]
    async with db.session_factory()() as session:
        employer = await session.get(Employer, employer_id)
        assert employer is not None
        employer.industry = "Biotech"
        await session.commit()
    (changed,) = await _sent(listening, "employer")
    assert changed["industry"] == "Biotech" and changed["fetched_at"] == "2030-01-01T00:00:00+00:00"
    assert await _built("employer", [employer_id], [()]) == [changed]
    async with db.session_factory()() as session:
        await session.delete(await session.get(Employer, employer_id))
        await session.commit()
    (removed,) = await _sent(listening, "employer")
    assert removed == {"id": str(employer_id), "deleted": True}
    assert await _built("employer", [employer_id], [("deleted",)]) == [removed]


async def test_a_profile_version_saved_and_given_a_reason_built_from_its_row_are_the_messages_the_commits_made(
        listening):
    async with db.session_factory()() as session:
        version = _version()
        session.add(version)
        await session.commit()
        version_id = version.id
    (saved,) = await _sent(listening, "profile_version")
    assert await _built("profile_version", [version_id], [("new",)]) == [saved]
    async with db.session_factory()() as session:
        version = await session.get(ProfileVersion, version_id)
        assert version is not None
        version.why = "another made-up reason"
        await session.commit()
    (said,) = await _sent(listening, "profile_version")
    assert said["why"] == "another made-up reason" and said["saved_at"] == "2030-01-01T00:00:00+00:00"
    assert await _built("profile_version", [version_id], [()]) == [said]


async def test_a_version_of_a_document_that_is_no_profile_part_builds_no_message(listening):
    async with db.session_factory()() as session:
        version = _version(kind="not-a-part")
        session.add(version)
        await session.commit()
        version_id = version.id
    assert await _sent(listening, "profile_version") == []
    assert await _built("profile_version", [version_id], [("new",)]) == []


async def test_a_site_added_and_removed_built_from_its_row_are_the_messages_the_commits_made(listening):
    async with db.session_factory()() as session:
        site = _site("jobs.example.org")
        session.add(site)
        await session.commit()
        site_id = site.id
    (added,) = await _sent(listening, "site")
    assert added["list"] == "search" and await _built("site", [site_id], [("new",)]) == [added]
    async with db.session_factory()() as session:
        await session.delete(await session.get(Site, site_id))
        await session.commit()
    (removed,) = await _sent(listening, "site")
    assert removed == {"id": str(site_id), "deleted": True}
    assert await _built("site", [site_id], [("deleted",)]) == [removed]


async def test_several_ids_build_their_messages_in_the_order_given_and_skip_a_row_that_is_gone(js):
    first, second = await _add(_role("h"), _role("i"))
    built = await _built("role", [second, uuid.uuid4(), first], [("new",), ("new",), ("new",)])
    assert [message["title"] for message in built] == ["Role i", "Role h"]


def test_every_kind_has_a_builder():
    assert set(live_read.SHAPES) | {"counts", "summary", "changed"} == live_read.KINDS
    assert live_read.DELETABLE == {"task", "notification", "employer", "site"}
