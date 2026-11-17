"""The writes the page program answers on its own: a write with no follow-through
the engine must do before it answers. Each is called on the page's app with no engine running and no socket (the
engine's door is the page's real `Remote`, and every bell it would ring is kept instead of sent). Four things are
checked: the route is the page's and not the engine's; no module of `forbidden.FORBIDDEN` is imported while it
answers; the reply is read on a reader after the commit (the writer's turn is for writing); and the to-do it leaves,
where it leaves one, is a row the engine finds. No row or assertion here depends on the date or the time, so the clock
is not pinned. Everything is made up."""

import builtins
import contextlib

import pytest
from httpx import ASGITransport, AsyncClient
from process_engine import versions
from sqlalchemy import select

from app import db, notice
from app.agents import edit_process, roster, shipped_rule
from app.inbox import service as inbox
from app.jobsearch.factory import build_app
from app.jobsearch.jobs import fits_none
from app.jobsearch.mail import login as mail_login
from app.jobsearch.mail import reader as go_mail
from app.jobsearch.models import FitsNoneOk, GapAnswerRow, Opportunity, Proposal
from app.jobsearch.pipeline import live, live_sink
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import engine_calls
from app.jobsearch.work.queue import topics as t
from app.settings import settings
from tests.builders import MINUTE, criteria, parse_runner, save_documents, tab
from tests.jobsearch.test_gap_clarify import ANSWER, JOB, LINE, _asked_run, _card_of, _master_version, _one_proposal
from tests.jobsearch.work_queue.support import posted_now
from tests.split import forbidden
from tests.split.test_page_reads_take_no_write_lock import turns_recorded
from tests.split.test_route_programs import _keys
from tests.support import OWNER, load_profile

AGENT_WRITES = {
    "PATCH /api/v1/agents/{connection_id}",
    "POST /api/v1/harness/processes/{key}/edits",
    "POST /api/v1/harness/processes/{key}/rollback",
}
ROUND = "jobsearch_round"


@contextlib.contextmanager
def imports_recorded():
    """Every module of `forbidden.FORBIDDEN` that an `import` statement names while the block runs (a statement is
    recorded when it runs, even when the module is already loaded, so an import made inside a function is seen)."""
    reached: set[str] = set()
    wanted = set(forbidden.FORBIDDEN)
    real = builtins.__import__

    def recording(name, globals=None, locals=None, fromlist=(), level=0):
        if level:
            package = (globals or {}).get("__package__") or (globals or {}).get("__name__", "?")
            base = package.rsplit(".", level - 1)[0] if level > 1 else package
            full = f"{base}.{name}" if name else base
        else:
            full = name
        for candidate in [full, *(f"{full}.{item}" for item in (fromlist or ()) if isinstance(item, str))]:
            if candidate in wanted:
                reached.add(candidate)
        return real(name, globals, locals, fromlist, level)

    builtins.__import__ = recording
    try:
        yield reached
    finally:
        builtins.__import__ = real


@pytest.fixture
def rang(monkeypatch):
    """The engine's door is the page's real one (`Remote`); each bell it rings is kept by kind instead of being sent, so
    no socket is used and no engine is needed."""
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


def readers_of(monkeypatch, module, name: str) -> list[bool]:
    """Wrap `module.name` (an async function whose first argument is a session) so that each call records whether its
    session is a reader's (`db.is_reader`). The list is filled as the test goes on."""
    real = getattr(module, name)
    seen: list[bool] = []

    async def spy(session, *args, **kwargs):
        seen.append(db.is_reader(session))
        return await real(session, *args, **kwargs)

    monkeypatch.setattr(module, name, spy)
    return seen


def test_the_page_serves_the_agent_and_how_they_work_writes_and_the_engine_does_not():
    page, engine = (set(_keys(build_app(role))) for role in ("page", "engine"))
    assert AGENT_WRITES <= page, sorted(AGENT_WRITES - page)
    assert not AGENT_WRITES & engine, sorted(AGENT_WRITES & engine)


def test_the_recorder_sees_a_forbidden_import_made_inside_a_function():
    with imports_recorded() as reached:
        from app.jobsearch.work import wake  # noqa: F401
    assert reached == {"app.jobsearch.work.wake"}


async def test_pausing_and_resuming_an_agent_on_the_page_leaves_a_to_do_for_the_engine(page_http, rang, monkeypatch):
    detail_on_a_reader = readers_of(monkeypatch, roster, "detail")
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        await session.commit()
    listed = (await page_http.get("/api/v1/agents")).json()
    path = f"/api/v1/agents/{listed[0]['connection_id']}"
    paused, imported_a = await write(page_http, "PATCH", path, json={"paused": True})
    rang.clear()
    resumed, imported_b = await write(page_http, "PATCH", path, json={"paused": False})
    assert (paused.status_code, paused.json()["paused"]) == (200, True)
    assert (resumed.status_code, resumed.json()["paused"]) == (200, False)
    assert imported_a == [] and imported_b == []
    assert detail_on_a_reader == [True, True], "the reply is read on a reader, after the commit"
    assert t.POOL_OWED in {topic for topic, _ in await posted_now()}
    assert set(rang) == {"look"}, "the resume left a to-do and rang the engine's look"


async def test_a_process_edit_and_a_rollback_on_the_page_only_save(page_http, rang, monkeypatch):
    async with db.session_factory()() as session:
        await shipped_rule.settle(session)
        await session.commit()
    xml = versions.shipped_xml(ROUND)
    assert 'name="Read the gates"' in xml
    renamed = xml.replace('name="Read the gates"', 'name="Read the owner\'s gates"')
    history_on_a_reader = readers_of(monkeypatch, edit_process, "history")
    edited, imported_a = await write(
        page_http, "POST", f"/api/v1/harness/processes/{ROUND}/edits", json={"xml": renamed, "why": "Say it"})
    rolled, imported_b = await write(
        page_http, "POST", f"/api/v1/harness/processes/{ROUND}/rollback", json={"to": 1})
    assert edited.status_code == 200, edited.text
    assert rolled.status_code == 200, rolled.text
    assert edited.json()["versions"][0]["why"] == "Say it"
    assert rolled.json()["versions"][0]["why"] == "Went back to version 1."
    assert imported_a == [] and imported_b == []
    assert history_on_a_reader == [True, True], "each reply is read on a reader, after the commit"


INBOX_WRITES = {
    "POST /api/v1/tasks/{task_id}/done",
    "POST /api/v1/tasks/{task_id}/skip",
    "POST /api/v1/tasks/{task_id}/snooze",
    "POST /api/v1/notifications/read-all",
    "POST /api/v1/notifications/{note_id}/read",
    "POST /api/v1/profile/fits-none/{subject_id}/dismiss",
}


async def _task(key: str) -> str:
    async with db.session_factory()() as session:
        task = await inbox.raise_task(session, OWNER, key=key, title=f"Check {key}", kind="note")
        await session.commit()
        return str(task.id)


async def _note(key: str) -> str:
    async with db.session_factory()() as session:
        note = await inbox.notify(session, OWNER, key=key, kind="note", title=f"Heard {key}")
        await session.commit()
        return str(note.id)


def test_the_page_serves_the_inbox_and_fits_none_writes_and_the_engine_does_not():
    page, engine = (set(_keys(build_app(role))) for role in ("page", "engine"))
    assert INBOX_WRITES <= page, sorted(INBOX_WRITES - page)
    assert not INBOX_WRITES & engine, sorted(INBOX_WRITES & engine)


async def test_done_skip_and_snooze_on_the_page_change_the_task_and_ring_nothing_but_a_look(page_http, rang):
    done_id, skip_id, snooze_id = await _task("w-done"), await _task("w-skip"), await _task("w-snooze")
    done, imported_a = await write(page_http, "POST", f"/api/v1/tasks/{done_id}/done", json={"note": "paid"})
    skipped, imported_b = await write(page_http, "POST", f"/api/v1/tasks/{skip_id}/skip", json={"reason": "not now"})
    snoozed, imported_c = await write(
        page_http, "POST", f"/api/v1/tasks/{snooze_id}/snooze", json={"until": "2026-11-20"})
    assert [r.status_code for r in (done, skipped, snoozed)] == [200, 200, 200]
    assert (done.json()["state"], done.json()["note"]) == ("done", "paid")
    assert (skipped.json()["state"], skipped.json()["note"]) == ("skipped", "not now")
    assert (snoozed.json()["state"], snoozed.json()["snoozed_until"]) == ("open", "2026-11-20")
    assert imported_a == [] and imported_b == [] and imported_c == []
    assert set(rang) <= {"look"}


async def test_reading_one_notification_and_all_of_them_on_the_page_changes_only_the_unread(page_http, rang):
    ids = [await _note(f"w-note-{n}") for n in range(3)]
    one, imported_a = await write(page_http, "POST", f"/api/v1/notifications/{ids[0]}/read")
    everything, imported_b = await write(page_http, "POST", "/api/v1/notifications/read-all")
    assert one.status_code == 200 and one.json() == {"changed": 1}
    assert everything.status_code == 200 and everything.json() == {"changed": 2}
    assert imported_a == [] and imported_b == []
    assert set(rang) <= {"look"}


async def test_a_write_on_the_page_reaches_the_open_stream_with_no_engine_and_no_socket(page_http, monkeypatch):
    monkeypatch.setattr(live_sink, "_sink", None)  # the local sink, as in the page
    task_id = await _task("w-live")
    sub = live.broker.subscribe(OWNER, frozenset({"task"}))
    try:
        reply, imported = await write(page_http, "POST", f"/api/v1/tasks/{task_id}/done", json={})
        await live_sink.settle()
        sent = []
        while not sub.queue.empty():
            sent.append(sub.queue.get_nowait())
    finally:
        live.broker.unsubscribe(sub)
    assert reply.status_code == 200 and imported == []
    assert ("task", task_id) in [(kind, data.get("id")) for _, kind, data in sent]


async def test_marking_a_role_fine_on_the_page_stores_it_and_counts_on_a_reader(page_http, rang, monkeypatch):
    async with db.session_factory()() as session:
        role = Opportunity(owner=OWNER, slug="fits-1", company="Company 1", title="Role Title 1",
                           status="ready_to_launch", fit=3, match_key="company 1|role title 1", origin="scout")
        session.add(role)
        await session.commit()
        role_id = str(role.id)
    before = (await page_http.get("/api/v1/profile/fits-none")).json()
    assert before["pursued"]["count"] == 1
    pursued_on_a_reader = readers_of(monkeypatch, fits_none, "counts")
    done, imported = await write(page_http, "POST", f"/api/v1/profile/fits-none/{role_id}/dismiss")
    assert done.status_code == 200, done.text
    assert done.json() == {"id": role_id, "kind": "role", "dismissed": True, "pursued": 0, "this_week": 0}
    assert imported == []
    assert pursued_on_a_reader == [True], "the new counts are read on a reader, after the commit"
    async with db.session_factory()() as session:
        marks = (await session.scalars(select(FitsNoneOk))).all()
    assert [(m.owner, str(m.subject_id), m.kind) for m in marks] == [(OWNER, role_id, "role")]
    assert set(rang) <= {"look"}


MAIL_WRITES = {
    "PUT /api/v1/mail-login",
    "POST /api/v1/mail-login/test",
    "DELETE /api/v1/mail-login",
}
PASSWORD = "abcd efgh ijkl mnop"


class FakeConn:
    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
                      b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"']


class FakeBox:
    """A mailbox that accepts the login and lists three folders, read-only: the stand-in for `go_mail.open_inbox`."""

    def __init__(self):
        self.opened = []
        self.conn = FakeConn()

    def __call__(self, login):
        self.opened.append(login)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


@pytest.fixture
def mail_key(tmp_path, monkeypatch):
    """Where the sealing key of the mailbox password is kept: in the test's own folder."""
    monkeypatch.setattr(settings, "mail_key_file", str(tmp_path / "secrets" / "mail-key"))


def test_the_page_serves_the_mail_login_writes_and_the_engine_does_not():
    page, engine = (set(_keys(build_app(role))) for role in ("page", "engine"))
    assert MAIL_WRITES <= page, sorted(MAIL_WRITES - page)
    assert not MAIL_WRITES & engine, sorted(MAIL_WRITES & engine)


async def test_saving_testing_and_forgetting_the_mail_login_on_the_page(page_http, rang, mail_key, monkeypatch):
    view_on_a_reader = readers_of(monkeypatch, mail_login, "view")
    box = FakeBox()
    monkeypatch.setattr(go_mail, "open_inbox", box)
    saved, imported_a = await write(
        page_http, "PUT", "/api/v1/mail-login", json={"username": "me@gmail.com", "password": PASSWORD})
    assert saved.status_code == 200, saved.text
    assert saved.json()["connected"] is True and saved.json()["username"] == "me@gmail.com"
    with turns_recorded() as turns:
        tested, imported_b = await write(page_http, "POST", "/api/v1/mail-login/test")
    assert tested.status_code == 200, tested.text
    assert tested.json()["ok"] is True and tested.json()["folders"] == ["INBOX", "[Gmail]/All Mail"]
    assert not turns.waits and not turns.ends, "the login test takes no writer's turn: it only reads"
    assert [(w.host, w.port, w.username) for w in box.opened] == [("imap.gmail.com", 993, "me@gmail.com")]
    forgotten, imported_c = await write(page_http, "DELETE", "/api/v1/mail-login")
    assert forgotten.status_code == 200 and forgotten.json()["connected"] is False
    assert imported_a == [] and imported_b == [] and imported_c == []
    assert view_on_a_reader == [True, True], "the save's and the delete's replies are read on a reader"
    assert set(rang) <= {"look"}


GAP_WRITES = {
    "PUT /api/v1/find-gaps/runs/{run_id}/themes/{theme_id}/answer",
    "DELETE /api/v1/find-gaps/answers/{answer_id}",
    "PUT /api/v1/find-gaps/cards/{card_id}",
    "POST /api/v1/find-gaps/proposals/{proposal_id}/accept",
    "POST /api/v1/find-gaps/proposals/{proposal_id}/reject",
}


@pytest.fixture
async def gaps_world(js, monkeypatch, knocks):
    """The world the Find Gaps tests of the Coach start from (`client` in tests/jobsearch/test_gap_clarify.py), without
    its HTTP client: the owner, a profile with two target roles, the made-up master resume and a runner that was seen a
    minute ago, so that the helpers of that module (`_asked_run`, `_one_proposal`) can build a run, its cards and its
    proposals through the services."""
    monkeypatch.setattr(settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=criteria(tab("builders"), tab("planners")))
        await save_documents(session)
        await session.commit()
    await parse_runner(seen=MINUTE)


def test_the_page_serves_the_find_gaps_answers_cards_and_proposals_and_the_engine_does_not():
    page, engine = (set(_keys(build_app(role))) for role in ("page", "engine"))
    assert GAP_WRITES <= page, sorted(GAP_WRITES - page)
    assert not GAP_WRITES & engine, sorted(GAP_WRITES & engine)


async def test_a_theme_answer_on_the_page_is_kept_and_taken_back(gaps_world, page_http, rang):
    run_id, _ = await _asked_run()
    rang.clear()  # what the set-up rang is not what this call rings
    said, imported_a = await write(
        page_http, "PUT", f"/api/v1/find-gaps/runs/{run_id}/themes/data-catalog-tooling/answer",
        json={"answer": "not_done"})
    assert said.status_code == 200, said.text
    assert said.json()["answer"] == "not_done"
    async with db.session_factory()() as session:
        kept = (await session.scalars(select(GapAnswerRow))).all()
    assert [row.key for row in kept] == ["data-catalog-tooling"]
    cleared, imported_b = await write(page_http, "DELETE", f"/api/v1/find-gaps/answers/{said.json()['id']}")
    assert cleared.status_code == 200 and cleared.json() == {"cleared": True}
    async with db.session_factory()() as session:
        assert (await session.scalars(select(GapAnswerRow))).all() == []
    assert imported_a == [] and imported_b == []
    assert set(rang) <= {"look"}


async def test_an_answer_to_a_coach_card_on_the_page_is_kept_word_for_word(gaps_world, page_http, rang):
    run_id, _ = await _asked_run()
    card = await _card_of(run_id, "governance-and-risk")
    rang.clear()  # what the set-up rang is not what this call rings
    answered, imported = await write(
        page_http, "PUT", f"/api/v1/find-gaps/cards/{card.id}", json={"action": "answer", "text": ANSWER})
    assert answered.status_code == 200, answered.text
    assert answered.json() == {"id": str(card.id), "state": "answered"}
    assert (await _card_of(run_id, "governance-and-risk")).answer == ANSWER
    assert imported == []
    assert set(rang) <= {"look"}


async def test_rejecting_a_proposal_on_the_page_keeps_it_as_rejected_and_saves_nothing(gaps_world, page_http, rang):
    _, proposal_id, _ = await _one_proposal()
    before = await _master_version()
    rang.clear()  # what the set-up rang is not what this call rings
    rejected, imported = await write(page_http, "POST", f"/api/v1/find-gaps/proposals/{proposal_id}/reject")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json() == {"id": proposal_id, "status": "rejected"}
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Proposal))).one().status == "rejected"
    assert await _master_version() == before
    assert imported == []
    assert set(rang) <= {"look"}


async def test_accepting_a_proposal_on_the_page_saves_a_master_version_and_shows_the_master_from_a_reader(
        gaps_world, page_http, rang, monkeypatch):
    _, proposal_id, _ = await _one_proposal()
    before = await _master_version()
    documents_on_a_reader = readers_of(monkeypatch, profile_store, "owner_documents")
    rang.clear()
    accepted, imported = await write(
        page_http, "POST", f"/api/v1/find-gaps/proposals/{proposal_id}/accept", json={})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["version"] == before + 1
    role = next(r for r in accepted.json()["resume"]["roles"] if r["id"] == JOB)
    assert role["bullets"][-1]["text"] == LINE
    assert imported == []
    assert documents_on_a_reader and documents_on_a_reader[-1] is True, "the master is read on a reader at the end"
    assert documents_on_a_reader.count(True) == 1
    assert set(rang) == {"look"}, "the new master version left to-dos and rang the engine's look"
