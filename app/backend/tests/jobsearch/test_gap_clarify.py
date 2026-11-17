"""Find Gaps started from a job and worked through with the Coach (docs/PRD.md 38.11 to 38.19, 8.3's minimal shape): a
run
for one role in the app (its own posting and up to nine similar ones, one from each employer; the web tops it up when
there are fewer than five), the Coach's questions as cards, the owner's answers (answer, skip, not true), the lines the
Coach proposes in the owner's own words, the owner's accept, edit and reject, and that nothing writes to the master but
an accept. Everything is made up."""

import random
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app import db
from app.harness import instructions as harness_instructions
from app.jobsearch import process
from app.jobsearch.gaps import coach as gap_coach
from app.jobsearch.gaps import runs as find_gaps
from app.jobsearch.gaps import tools as gap_tools
from app.jobsearch.interviews import coach
from app.jobsearch.main import app
from app.jobsearch.models import (
    GapAnswerRow,
    GapCard,
    GapCoachJob,
    GapNotTrue,
    GapRun,
    Opportunity,
    ProfileVersion,
    Proposal,
)
from app.jobsearch.pool import core as pool
from app.jobsearch.profile import store as profile_store
from app.jobsearch.work import hours as night
from app.jobsearch.work import session_kinds
from app.settings import settings
from tests.builders import MINUTE, criteria, parse_runner, save_documents, tab
from tests.support import OWNER, load_profile

NOW = datetime(2026, 10, 30, 22, 0, tzinfo=UTC)
JOB = "northwind-lead"  # the made-up master's one job
ANSWER = "I ran the quarterly risk review for the board and cut its prep time in half. It took two analysts."
LINE = "ran the quarterly risk review for the board and cut its prep time in half"


@pytest.fixture
async def client(js, monkeypatch, knocks):
    monkeypatch.setattr(settings, "owner", OWNER)
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=criteria(tab("builders"), tab("planners")))
        await save_documents(session)
        await session.commit()
    await parse_runner(seen=MINUTE)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _stock(employers: int, role: str = "builders", prefix: str = "Employer"):
    """Roles in the Pipeline that kept their text, matched to a target role, one for each of `employers`."""
    async with db.session_factory()() as session:
        for i in range(employers):
            company = f"{prefix} {i + 1}"
            session.add(Opportunity(
                owner=OWNER, slug=f"{prefix.lower()}-{i}-{role}", company=company, title="Programs Lead",
                origin="scout",
                match_key=f"{company.lower()}|programs lead {role}", status="second_impression", target_role=role,
                url=f"https://jobs.example.test/{prefix}/{i}",
                description=f"Required: programs. Posting {prefix} {i}."))
        await session.commit()


async def _job(company: str = "Anchor Co", target: str | None = "builders", text: str | None = "Own the programs.",
               slug: str = "anchor") -> uuid.UUID:
    """The role the owner picks."""
    async with db.session_factory()() as session:
        role = Opportunity(
            owner=OWNER, slug=slug, company=company, title="Programs Director", origin="scout",
            match_key=f"{company.lower()}|programs director", status="second_impression", target_role=target,
            url=f"https://jobs.example.test/{slug}", description=text)
        session.add(role)
        await session.commit()
        return role.id


async def _start(job: uuid.UUID, **more) -> str:
    async with db.session_factory()() as session:
        run = await find_gaps.start_job(session, OWNER, str(job), **more)
        await session.commit()
        return str(run.id)


async def _row(run_id: str) -> GapRun:
    async with db.session_factory()() as session:
        return await session.get(GapRun, uuid.UUID(run_id), populate_existing=True)


async def _view(run_id: str) -> dict:
    async with db.session_factory()() as session:
        run = await find_gaps.get(session, OWNER, run_id)
        out = await find_gaps.view(session, OWNER, run)
        await session.commit()
        return out


async def _take_run() -> dict:
    async with db.session_factory()() as session:
        reply = await gap_tools.task_reply(session, OWNER)
        await session.commit()
        return reply


def _theme(name: str, asked: list[str], grade: str = "P", kind: str = "required"):
    return {"name": name, "kind": kind, "asked_by": asked, "grade": grade, "from": ["b-risk"] if grade != "U" else [],
            "why": "close work" if grade == "T" else ""}


def _themes(n: int = 10) -> list[dict]:
    refs = [f"p{i}" for i in range(1, n + 1)]
    return [_theme("Programs at scale", refs), _theme("Governance and risk", refs[:7], "T"),
            _theme("Data catalog tooling", refs[:6], "U", "preferred"), _theme("A certification", refs[:3], "U",
                                                                                "preferred")]


async def _report(run_id: str, themes, postings=None) -> dict:
    async with db.session_factory()() as session:
        reply = await gap_tools.report_reply(session, OWNER, run_id, themes, postings)
        await session.commit()
        return reply


async def _finished_job_run(job: uuid.UUID | None = None, themes=None, **more) -> str:
    """A run for a job with ten stored examples, asked for, taken and reported: its id."""
    async with db.session_factory()() as session:
        stocked = await session.scalar(select(func.count()).select_from(Opportunity).where(
            Opportunity.slug.like("employer-%")))
    if not stocked:
        await _stock(12)
    job = job or await _job()
    run_id = await _start(job, **more)
    await _take_run()
    await _report(run_id, themes or _themes())
    return run_id


# --- the coach's side --------------------------------------------------------------------------------------------


async def _start_ask(run_id: str) -> str:
    async with db.session_factory()() as session:
        job = await gap_coach.start_ask(session, OWNER, run_id)
        await session.commit()
        return str(job.id)


async def _start_propose(run_id: str) -> str:
    async with db.session_factory()() as session:
        job = await gap_coach.start_propose(session, OWNER, run_id)
        await session.commit()
        return str(job.id)


async def _coach(part: str = "task", job_id: str | None = None) -> dict:
    async with db.session_factory()() as session:
        reply = await coach.task_reply(session, OWNER, part, job_id, session_id="s1")
        await session.commit()
        return reply


async def _submit(body: dict) -> dict:
    async with db.session_factory()() as session:
        reply = await coach.submit(session, OWNER, body)
        await session.commit()
        return reply


async def _job_row(job_id: str) -> GapCoachJob:
    async with db.session_factory()() as session:
        return await session.get(GapCoachJob, uuid.UUID(job_id), populate_existing=True)


async def _cards(run_id: str) -> list[GapCard]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(GapCard).where(GapCard.run_id == uuid.UUID(run_id))
                                           .order_by(GapCard.theme, GapCard.position))).all())


def _questions(*pairs) -> list[dict]:
    return [{"theme": t, "question": q} for t, q in pairs]


ASKED = _questions(("governance-and-risk", "Where did you run a risk review, and what did it change?"),
                   ("data-catalog-tooling", "Have you chosen or run a data catalog? Which one?"),
                   ("data-catalog-tooling", "Who used it, and for what?"),
                   ("a-certification", "Do you hold a certification in programs or risk?"))


async def _asked_run() -> tuple[str, str]:
    """A finished run for a job whose Transferable and Unproven themes the Coach asked about: (run id, ask job id)."""
    run_id = await _finished_job_run()
    job_id = await _start_ask(run_id)
    await _coach()
    sent = await _submit({"id": job_id, "questions": ASKED})
    assert sent["ok"] is True, sent
    return run_id, job_id


async def _answer(card: GapCard, action: str = "answer", text: str = ANSWER) -> None:
    async with db.session_factory()() as session:
        await gap_coach.answer_card(session, OWNER, str(card.id), action, text)
        await session.commit()


async def _card_of(run_id: str, theme: str, nth: int = 0) -> GapCard:
    return [c for c in await _cards(run_id) if c.theme_key == theme][nth]


async def _master_version() -> int:
    async with db.session_factory()() as session:
        return await profile_store.newest_version(session, OWNER, "resume")


# --- start from a job: the examples ------------------------------------------------------------------------------


async def test_the_picker_lists_the_roles_that_hold_a_posting_and_no_other(client):
    kept = await _job("Has Text Co", slug="has-text")
    await _job("No Text Co", text=None, slug="no-text")
    await _job("Blank Co", text="   ", slug="blank")
    jobs = (await client.get("/api/v1/find-gaps/jobs")).json()
    assert [(j["id"], j["company"], j["title"]) for j in jobs] == [(str(kept), "Has Text Co", "Programs Director")]


async def test_a_run_for_a_job_reads_its_own_posting_first_and_nine_similar_ones_from_other_employers(client):
    await _stock(12)
    await _stock(1, prefix="Anchor Co")  # a similar posting from the job's own employer is never read
    job = await _job("Anchor Co")
    run_id = await _start(job, rng=random.Random(1))
    run = await _row(run_id)
    assert (run.kind, run.role_id, run.opportunity_id, run.title) == ("role", "builders", job, "Programs Director")
    assert len(run.postings) == 10 and run.postings[0]["ref"] == "p1"
    assert (run.postings[0]["employer"], run.postings[0]["source_id"]) == ("Anchor Co", str(job))
    assert run.postings[0]["text"] == "Own the programs."
    employers = [p["employer"] for p in run.postings]
    assert len(set(employers)) == 10 and employers.count("Anchor Co") == 1
    assert [p["ref"] for p in run.postings] == [f"p{i}" for i in range(1, 11)]


async def test_a_role_with_no_posting_text_is_refused_in_words_and_starts_nothing(client, knocks):
    job = await _job(text=None)
    res = await client.post("/api/v1/find-gaps/runs", json={"opportunity_id": str(job)})
    assert res.status_code == 422 and "posting" in res.json()["detail"]
    missing = await client.post("/api/v1/find-gaps/runs", json={"opportunity_id": str(uuid.uuid4())})
    assert missing.status_code == 404 and missing.json()["detail"] == find_gaps.NO_SUCH_JOB
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(GapRun)) == 0
    assert knocks == []


async def test_with_fewer_than_five_postings_in_all_the_agent_tops_them_up_from_the_web_and_stores_no_pipeline_role(
        client, knocks):
    await _stock(2)
    job = await _job()
    run_id = (await client.post("/api/v1/find-gaps/runs", json={"opportunity_id": str(job)})).json()["id"]
    assert knocks == [(run_id, "gaps_asked")]
    run = await _row(run_id)
    assert (run.kind, run.opportunity_id) == ("new", job)
    assert run.postings[0]["employer"] == "Anchor Co"
    assert {p["employer"] for p in run.postings} == {"Anchor Co", "Employer 1", "Employer 2"}
    assert all(p["text"] for p in run.postings), "the examples already read are handed over with their text"
    task = await _take_run()
    assert task["mode"] == "search" and task["read_next"] == ["postings", "resume"]
    assert "p1 to p3" in task["how"] and "at least 2" in task["how"]
    finds = [{"employer": f"Web Co {i}", "title": "Programs Director", "url": f"https://careers.example.test/{i}"}
             for i in range(1, 4)]
    async with db.session_factory()() as session:
        before = await session.scalar(select(func.count()).select_from(Opportunity))
    with pytest.raises(find_gaps.GapError) as short_of:
        await _report(run_id, _themes(4), finds[:1])
    assert "at least 5" in str(short_of.value)
    with pytest.raises(find_gaps.GapError) as twice:
        await _report(run_id, _themes(5), [{**finds[0], "employer": "anchor co"}, *finds[1:]])
    assert "anchor co" in str(twice.value).lower() and "twice" in str(twice.value)
    assert (await _report(run_id, _themes(6), finds))["ok"] is True
    done = await _row(run_id)
    assert done.state == "done" and len(done.postings) == 6
    assert [p["ref"] for p in done.postings] == [f"p{i}" for i in range(1, 7)]
    assert all("text" not in p for p in done.postings), "a web run keeps who and where, not the text"
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(Opportunity)) == before


async def test_a_role_with_no_target_role_goes_to_the_web(client):
    job = await _job(target=None)
    run = await _row(await _start(job))
    assert (run.kind, run.role_id) == ("new", None)
    assert len(run.postings) == 1 and run.postings[0]["employer"] == "Anchor Co"


async def test_a_job_run_does_not_replace_its_target_roles_run_and_a_second_run_for_the_job_replaces_the_first(client):
    await _stock(12)
    async with db.session_factory()() as session:
        role_run = await find_gaps.start_role(session, OWNER, "builders", rng=random.Random(2))
        await session.commit()
        role_run_id = str(role_run.id)
    await _take_run()
    await _report(role_run_id, _themes())
    first = await _finished_job_run(rng=random.Random(3))
    dash = (await client.get("/api/v1/find-gaps")).json()
    builders = next(r for r in dash["roles"] if r["id"] == "builders")
    assert builders["last_run"]["id"] == role_run_id, "the dashboard's run for the target role is as it was"
    assert [(r["id"], r["company"]) for r in dash["job_runs"]] == [(first, "Anchor Co")]
    async with db.session_factory()() as session:
        job = (await session.scalars(select(GapRun.opportunity_id).where(GapRun.id == uuid.UUID(first)))).one()
    second = await _finished_job_run(job, rng=random.Random(4))
    assert (await _row(role_run_id)).state == "done"
    async with db.session_factory()() as session:
        assert await session.get(GapRun, uuid.UUID(first)) is None, "the job's earlier run is replaced"
    assert [r["id"] for r in (await client.get("/api/v1/find-gaps")).json()["job_runs"]] == [second]


async def test_a_job_run_has_no_check_again_or_new_sample(client):
    await _stock(12)
    run_id = await _finished_job_run()
    assert (await _view(run_id))["can_repeat"] is False
    for path in ("check-again", "new-sample"):
        res = await client.post(f"/api/v1/find-gaps/runs/{run_id}/{path}")
        assert res.status_code == 422 and "job" in res.json()["detail"], path


# --- the Coach asks ----------------------------------------------------------------------------------------------


async def test_the_coach_is_handed_the_transferable_and_unproven_themes_and_none_that_are_set_aside(client, knocks):
    await _stock(12)
    run_id = await _finished_job_run()
    async with db.session_factory()() as session:  # a theme hidden as not relevant, and one said not true earlier
        session.add(GapAnswerRow(owner=OWNER, scope="builders", key="a-certification", name="A certification",
                                 kind="preferred", answer="not_relevant"))
        session.add(GapNotTrue(owner=OWNER, scope="builders", key="data-catalog-tooling", name="Data catalog tooling"))
        await session.commit()
    res = await client.post(f"/api/v1/find-gaps/runs/{run_id}/questions")
    assert res.status_code == 200 and knocks[-1] == (res.json()["id"], "coach_asked")
    task = await _coach()
    assert task["mode"] == "clarify_ask" and task["id"] == res.json()["id"]
    assert [t["id"] for t in task["themes"]] == ["governance-and-risk"], "not the Proven one, nor the set-aside ones"
    assert task["themes"][0]["grade"] == "T" and task["themes"][0]["lines"][0]["id"] == "b-risk"
    assert "Clarifying the gaps" in task["instructions"] and "questions:" in task["response_template"]
    assert task["read_next"] == ["posting", "resume"]
    assert "Own the programs." in (await _coach("posting", task["id"]))["posting"]["text"]
    assert (await _coach("resume", task["id"]))["master"]["roles"][0]["id"] == JOB
    row = await _job_row(task["id"])
    assert row.state == "working" and row.mode == "ask"


async def test_nothing_to_ask_is_said_in_words(client):
    run_id = await _finished_job_run(themes=[_theme("Programs at scale", [f"p{i}" for i in range(1, 11)])])
    res = await client.post(f"/api/v1/find-gaps/runs/{run_id}/questions")
    assert res.status_code == 422 and "nothing" in res.json()["detail"].lower()


async def test_questions_that_miss_a_theme_or_name_a_stranger_or_run_to_three_are_sent_back_with_each_problem(client):
    run_id = await _finished_job_run()
    job_id = await _start_ask(run_id)
    await _coach()
    sent = await _submit({"id": job_id, "questions": _questions(
        ("governance-and-risk", "One?"), ("governance-and-risk", "Two?"), ("governance-and-risk", "Three?"),
        ("programs-at-scale", "A Proven theme?"), ("nonesuch", "?"), ("a-certification", "   "))})
    assert sent["ok"] is False
    said = " | ".join(sent["problems"])
    assert "governance-and-risk" in said and "at most 2" in said
    assert "programs-at-scale" in said and "nonesuch" in said
    assert "data-catalog-tooling" in said, "a theme left without a question is named"
    assert "a-certification" in said, "an empty question is named"
    assert await _cards(run_id) == [], "a refusal stores nothing"
    assert (await _job_row(job_id)).attempts == 1


async def test_three_refused_sets_fail_the_job_with_a_sentence(client):
    run_id = await _finished_job_run()
    job_id = await _start_ask(run_id)
    await _coach()
    for _ in range(gap_coach.MAX_REFUSALS):
        sent = await _submit({"id": job_id, "questions": []})
        assert sent["ok"] is False
    assert sent.get("failed") is True
    row = await _job_row(job_id)
    assert row.state == "failed" and row.error == gap_coach.GAVE_UP


async def test_questions_that_pass_become_cards_in_the_runs_view_and_finish_the_process(client):
    run_id, job_id = await _asked_run()
    cards = await _cards(run_id)
    assert [(c.theme_key, c.grade, c.state) for c in cards] == [
        ("a-certification", "U", "open"), ("data-catalog-tooling", "U", "open"), ("data-catalog-tooling", "U", "open"),
        ("governance-and-risk", "T", "open")]
    row = await _job_row(job_id)
    assert row.state == "done" and row.instance_id is not None
    async with db.session_factory()() as session:
        assert (await process.engine().where_is(session, row.instance_id))["status"] == "completed"
    clarify = (await _view(run_id))["clarify"]
    assert [c["theme"] for c in clarify["cards"]] == ["Governance and risk", "Data catalog tooling",
                                                     "Data catalog tooling", "A certification"], "in the run's order"
    assert clarify["coach"] is None and clarify["can_ask"] is False and clarify["can_propose"] is False


async def test_a_theme_that_has_a_card_is_not_asked_again(client):
    run_id, _ = await _asked_run()
    async with db.session_factory()() as session:
        with pytest.raises(find_gaps.GapError):
            await gap_coach.start_ask(session, OWNER, run_id)


# --- the owner answers -------------------------------------------------------------------------------------------


async def test_an_answer_is_kept_word_for_word_and_a_short_one_is_refused(client):
    run_id, _ = await _asked_run()
    card = await _card_of(run_id, "governance-and-risk")
    res = await client.put(f"/api/v1/find-gaps/cards/{card.id}", json={"action": "answer", "text": "short"})
    assert res.status_code == 422 and "10" in res.json()["detail"]
    assert (await _card_of(run_id, "governance-and-risk")).state == "open"
    res = await client.put(f"/api/v1/find-gaps/cards/{card.id}", json={"action": "answer", "text": f"  {ANSWER}\n"})
    assert res.status_code == 200
    kept = await _card_of(run_id, "governance-and-risk")
    assert (kept.state, kept.answer) == ("answered", ANSWER)
    long = "I did it. " * 5000
    assert (await client.put(f"/api/v1/find-gaps/cards/{card.id}", json={"action": "answer", "text": long})
            ).status_code == 200, "no limit on the owner's words"
    assert (await _card_of(run_id, "governance-and-risk")).answer == long.strip()


async def test_skip_closes_the_card_and_the_theme_can_be_asked_on_a_later_run(client):
    run_id, _ = await _asked_run()
    card = await _card_of(run_id, "a-certification")
    await _answer(card, "skip", "")
    assert (await _card_of(run_id, "a-certification")).state == "skipped"
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(GapNotTrue)) == 0
    job = (await _row(run_id)).opportunity_id
    again = await _finished_job_run(job)
    async with db.session_factory()() as session:
        asked = await gap_coach.start_ask(session, OWNER, again)
        assert asked.items == ["governance-and-risk", "data-catalog-tooling", "a-certification"]


async def test_not_true_closes_every_card_of_the_theme_and_it_is_never_asked_again_on_this_job_or_another(client):
    run_id, _ = await _asked_run()
    first, second = [c for c in await _cards(run_id) if c.theme_key == "data-catalog-tooling"]
    await _answer(first, "not_true", "")
    states = {c.id: c.state for c in await _cards(run_id) if c.theme_key == "data-catalog-tooling"}
    assert states == {first.id: "not_true", second.id: "not_true"}, "the other card of the theme closed too"
    async with db.session_factory()() as session:
        (kept,) = (await session.scalars(select(GapNotTrue))).all()
        assert (kept.scope, kept.key, kept.name) == ("builders", "data-catalog-tooling", "Data catalog tooling")
    other = await _job("Second Co", slug="second")
    again = await _finished_job_run(other)
    async with db.session_factory()() as session:
        asked = await gap_coach.start_ask(session, OWNER, again)
        assert "data-catalog-tooling" not in asked.items and asked.items == ["governance-and-risk", "a-certification"]


async def test_a_second_run_for_the_job_keeps_the_cards_of_the_themes_that_are_still_there(client):
    run_id, _ = await _asked_run()
    card = await _card_of(run_id, "governance-and-risk")
    await _answer(card)
    job = (await _row(run_id)).opportunity_id
    later = await _finished_job_run(job, themes=[_theme("Governance and risk", ["p1", "p2"], "T"),
                                                _theme("A new ask", ["p1"], "U")])
    kept = await _cards(later)
    assert [(c.theme_key, c.state, c.answer) for c in kept] == [("governance-and-risk", "answered", ANSWER)]
    assert (await _view(later))["clarify"]["can_propose"] is True


# --- the Coach proposes ------------------------------------------------------------------------------------------


async def test_suggest_lines_needs_an_answer_that_no_suggestion_has_been_made_from(client):
    run_id, _ = await _asked_run()
    async with db.session_factory()() as session:
        with pytest.raises(find_gaps.GapError) as none:
            await gap_coach.start_propose(session, OWNER, run_id)
    assert "answer" in str(none.value)
    await _answer(await _card_of(run_id, "governance-and-risk"))
    assert (await _view(run_id))["clarify"]["can_propose"] is True


async def _proposed_run() -> tuple[str, str, str]:
    """An asked run with the governance card answered and the Coach taken the job: (run id, propose job id, card id)."""
    run_id, _ = await _asked_run()
    card = await _card_of(run_id, "governance-and-risk")
    await _answer(card)
    job_id = await _start_propose(run_id)
    return run_id, job_id, str(card.id)


def _proposal(card: str, text: str = LINE, place: dict | None = None, **more) -> dict:
    return {"card": card, "text": text, "place": place or {"kind": "new_line", "job": JOB}, "why": "shows the bridge",
            **more}


async def test_the_coach_is_handed_the_owners_words_and_the_master_and_sends_proposals_that_are_kept(client, knocks):
    run_id, job_id, card = await _proposed_run()
    task = await _coach()
    assert task["mode"] == "clarify_propose" and task["id"] == job_id
    assert task["answers"] == [{"card": card, "theme": "Governance and risk", "kind": "required",
                                "question": ASKED[0]["question"], "answer": ANSWER}]
    assert "Clarifying the gaps" in task["instructions"] and "proposals:" in task["response_template"]
    assert {r["id"] for r in (await _coach("resume", job_id))["master"]["roles"]} == {JOB}
    sent = await _submit({"id": job_id, "proposals": [_proposal(card)]})
    assert sent["ok"] is True and sent["proposals"] == 1 and sent["dropped"] == 0
    async with db.session_factory()() as session:
        (p,) = (await session.scalars(select(Proposal))).all()
        assert (p.status, p.after, p.document, p.source) == ("proposed", LINE, "resume", "coach.gaps")
        assert p.part == {"job_id": JOB, "kind": "new_line", "line_id": None}
        assert p.cites == {"card": card, "question": ASKED[0]["question"], "answer": ANSWER}
        assert p.run_id == uuid.UUID(run_id) and p.theme == "Governance and risk"
    assert (await _cards(run_id))[3].proposed_at is not None
    assert (await _job_row(job_id)).state == "done"
    view = (await _view(run_id))["clarify"]
    assert view["can_propose"] is False
    (shown,) = view["proposals"]
    assert shown["text"] == LINE and shown["status"] == "proposed" and shown["quote"] == ANSWER
    assert shown["place"] == {"kind": "new_line", "job": "Senior Manager, Northwind Labs", "line": None}


async def test_a_proposal_that_is_not_the_owners_words_cut_at_its_ends_is_refused_naming_the_answer(client):
    run_id, job_id, card = await _proposed_run()
    await _coach()
    for words in ("Led the quarterly risk review for the board and halved its prep time",  # reworded
                  "ran the risk review and cut its prep time in half",  # words dropped from the middle
                  "I ran the quarterly risk review for the board and cut its prep time in half. It took three analysts",
                  ):
        sent = await _submit({"id": job_id, "proposals": [_proposal(card, words)]})
        said = " ".join(sent["problems"])
        assert sent["ok"] is False and card in said and "words" in said, words
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(Proposal)) == 0


async def test_a_trimmed_cut_of_the_answer_passes_with_other_case_and_the_ends_cut_off(client):
    run_id, job_id, card = await _proposed_run()
    await _coach()
    sent = await _submit({"id": job_id, "proposals": [_proposal(card, "Ran the Quarterly risk review for the board.")]})
    assert sent["ok"] is True, sent


async def test_a_proposal_for_a_job_the_master_lacks_or_a_line_of_another_job_is_refused(client):
    run_id, job_id, card = await _proposed_run()
    await _coach()
    sent = await _submit({"id": job_id, "proposals": [
        _proposal(card, place={"kind": "new_line", "job": "no-such-job"}),
        _proposal(card, place={"kind": "evidence", "job": JOB, "line": "a-statewide"}),
        _proposal(card, place={"kind": "somewhere", "job": JOB}),
        _proposal("not-a-card")]})
    said = " | ".join(sent["problems"])
    assert sent["ok"] is False and "no-such-job" in said and "a-statewide" in said and "somewhere" in said
    assert "not-a-card" in said


async def test_a_rejected_proposal_is_kept_and_the_same_words_are_not_proposed_again(client):
    run_id, job_id, card = await _proposed_run()
    await _coach()
    await _submit({"id": job_id, "proposals": [_proposal(card)]})
    async with db.session_factory()() as session:
        p = (await session.scalars(select(Proposal))).one()
        proposal_id = str(p.id)
    before = await _master_version()
    res = await client.post(f"/api/v1/find-gaps/proposals/{proposal_id}/reject")
    assert res.status_code == 200
    async with db.session_factory()() as session:
        kept = (await session.scalars(select(Proposal))).one()
        assert kept.status == "rejected" and kept.decided_at is not None
    assert await _master_version() == before, "a rejection saves nothing"
    other = await _card_of(run_id, "a-certification")  # another answer, with the same words in it
    await _answer(other)
    again = await _start_propose(run_id)
    await _coach()
    sent = await _submit({"id": again, "proposals": [_proposal(str(other.id)), _proposal(
        str(other.id), "cut its prep time in half", {"kind": "evidence", "job": JOB, "line": "b-risk"})]})
    assert sent["ok"] is True and sent["dropped"] == 1 and sent["proposals"] == 1
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(Proposal)) == 2


async def _one_proposal(**more) -> tuple[str, str, str]:
    run_id, job_id, card = await _proposed_run()
    await _coach()
    sent = await _submit({"id": job_id, "proposals": [_proposal(card, **more)]})
    assert sent["ok"] is True, sent
    async with db.session_factory()() as session:
        return run_id, str((await session.scalars(select(Proposal))).one().id), card


async def _resume_now():
    async with db.session_factory()() as session:
        return (await profile_store.owner_documents(session, OWNER)).resume


async def _newest_version_row() -> ProfileVersion:
    async with db.session_factory()() as session:
        return (await session.scalars(select(ProfileVersion).where(
            ProfileVersion.owner == OWNER, ProfileVersion.kind == "resume")
            .order_by(ProfileVersion.version.desc()).limit(1))).one()


async def test_accept_saves_the_line_under_the_job_as_a_new_version_whose_history_names_all_three(client):
    run_id, proposal_id, _ = await _one_proposal()
    before = await _master_version()
    res = await client.post(f"/api/v1/find-gaps/proposals/{proposal_id}/accept", json={})
    assert res.status_code == 200
    body = res.json()
    assert body["version"] == before + 1
    role = next(r for r in body["resume"]["roles"] if r["id"] == JOB)
    assert role["bullets"][-1]["text"] == LINE and len(role["bullets"]) == 3
    assert role["bullets"][-1]["id"] not in {b["id"] for b in role["bullets"][:-1]}
    saved = await _newest_version_row()
    assert saved.version == before + 1 and "Coach" in saved.saved_by
    assert "Suggested by the Coach" in saved.why and "Governance and risk" in saved.why
    assert "accepted by you" in saved.why
    assert "edited" not in saved.why
    async with db.session_factory()() as session:
        p = (await session.scalars(select(Proposal))).one()
        assert (p.status, p.version, p.final_text) == ("accepted", before + 1, None)
    assert (await client.post(f"/api/v1/find-gaps/proposals/{proposal_id}/accept", json={})).status_code == 422, \
        "a decided proposal is not decided again"


async def test_accept_with_the_owners_edit_saves_their_words_and_says_so(client):
    _, proposal_id, _ = await _one_proposal()
    res = await client.post(f"/api/v1/find-gaps/proposals/{proposal_id}/accept",
                            json={"text": "Ran the board's quarterly risk review and halved its prep time"})
    assert res.status_code == 200
    role = next(r for r in res.json()["resume"]["roles"] if r["id"] == JOB)
    assert role["bullets"][-1]["text"] == "Ran the board's quarterly risk review and halved its prep time"
    assert "after you edited it" in (await _newest_version_row()).why
    async with db.session_factory()() as session:
        p = (await session.scalars(select(Proposal))).one()
        assert p.after == LINE and p.final_text == "Ran the board's quarterly risk review and halved its prep time"


async def test_evidence_on_an_existing_line_is_saved_as_evidence_on_the_job_titled_for_the_line(client):
    _, proposal_id, _ = await _one_proposal(place={"kind": "evidence", "job": JOB, "line": "b-risk"})
    res = await client.post(f"/api/v1/find-gaps/proposals/{proposal_id}/accept", json={})
    role = next(r for r in res.json()["resume"]["roles"] if r["id"] == JOB)
    assert len(role["bullets"]) == 2, "no new bullet"
    added = role["evidence"][-1]
    assert added["text"] == LINE
    assert added["title"] == "Evidence for: Led the risk working group across client engagements"


async def test_an_accept_whose_job_is_gone_from_the_master_is_refused_and_the_proposal_stays(client):
    _, proposal_id, _ = await _one_proposal()
    async with db.session_factory()() as session:
        docs = await profile_store.owner_documents(session, OWNER)
        gone = docs.resume.model_dump(mode="json")
        gone["roles"] = []
        await profile_store.save(session, OWNER, "resume", gone, by="test")
        await session.commit()
    before = await _master_version()
    res = await client.post(f"/api/v1/find-gaps/proposals/{proposal_id}/accept", json={})
    assert res.status_code == 422 and "job" in res.json()["detail"]
    assert await _master_version() == before
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Proposal))).one().status == "proposed"


async def test_nothing_but_an_accept_writes_to_the_master(client):
    await _stock(12)
    start = await _master_version()
    run_id, job_id, card = await _proposed_run()  # a run, questions, an answer and a skip are all behind this
    await _answer(await _card_of(run_id, "a-certification"), "skip", "")
    await _answer(await _card_of(run_id, "data-catalog-tooling"), "not_true", "")
    await _coach()
    await _submit({"id": job_id, "proposals": [_proposal(card)]})
    async with db.session_factory()() as session:
        proposal_id = str((await session.scalars(select(Proposal))).one().id)
    await client.post(f"/api/v1/find-gaps/proposals/{proposal_id}/reject")
    assert await _master_version() == start, "the master is as it was"
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(Proposal).where(
            Proposal.status == "accepted")) == 0


# --- peak hours, a busy agent, stuck jobs, the pool and the files ------------------------------------------------


async def test_in_peak_hours_the_questions_and_the_suggestions_are_refused_with_import_your_resumes_sentence(
        client, monkeypatch):
    run_id, _ = await _asked_run()
    await _answer(await _card_of(run_id, "governance-and-risk"))
    sentence = "Claude's peak hours: no Claude work until 5:00 PM."
    with monkeypatch.context() as peak:
        peak.setattr(night, "blackout", lambda s, now=None: sentence)
        for path in ("questions", "proposals"):
            res = await client.post(f"/api/v1/find-gaps/runs/{run_id}/{path}")
            assert res.status_code == 409 and res.json() == {"detail": sentence, "why": sentence}, path
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(GapCoachJob)) == 1, "only the first job ran"


async def test_a_busy_agent_queues_the_job_and_one_job_for_a_run_goes_at_a_time(client, knocks):
    run_id = await _finished_job_run()
    await parse_runner(seen=MINUTE, checked_in=MINUTE, kind="analyst")
    res = await client.post(f"/api/v1/find-gaps/runs/{run_id}/questions")
    assert res.status_code == 200
    again = await client.post(f"/api/v1/find-gaps/runs/{run_id}/questions")
    assert again.status_code == 409 and "already" in again.json()["detail"]
    coach_block = (await client.get(f"/api/v1/find-gaps/runs/{run_id}")).json()["clarify"]["coach"]
    assert coach_block["state"] == "waiting" and coach_block["step"].startswith("Queued: the agent is busy")
    assert knocks[-1] == (res.json()["id"], "coach_asked")


async def test_a_job_nobody_took_in_five_minutes_fails_with_a_sentence_and_can_be_asked_again(client):
    run_id = await _finished_job_run()
    job_id = await _start_ask(run_id)
    await parse_runner(seen=30 * MINUTE)  # the agent has been quiet for longer than a take
    async with db.session_factory()() as session:
        await gap_coach.expire(session, OWNER, datetime.now(UTC) + timedelta(minutes=6))
        await session.commit()
    row = await _job_row(job_id)
    assert row.state == "failed" and row.error == gap_coach.NOT_TAKEN
    clarify = (await _view(run_id))["clarify"]
    assert clarify["coach"] == {"id": job_id, "mode": "ask", "state": "failed", "step": "Stopped",
                                "error": gap_coach.NOT_TAKEN}
    await parse_runner(seen=MINUTE)
    assert await _start_ask(run_id) != job_id


async def test_a_job_a_session_took_and_never_finished_fails_and_so_does_one_whose_session_ended(client):
    run_id = await _finished_job_run()
    job_id = await _start_ask(run_id)
    await _coach()
    async with db.session_factory()() as session:
        await gap_coach.expire(session, OWNER, datetime.now(UTC) + gap_coach.FINISH_WITHIN + timedelta(minutes=1))
        await session.commit()
    assert (await _job_row(job_id)).error == gap_coach.NOT_FINISHED
    other = await _start_ask(run_id)
    await _coach()
    async with db.session_factory()() as session:
        assert await gap_coach.session_ended(session, OWNER) == 1
        await session.commit()
    assert (await _job_row(other)).error == gap_coach.SESSION_ENDED


async def test_the_next_work_is_a_coach_session_for_a_waiting_job_and_it_is_not_offered_twice(client):
    run_id = await _finished_job_run()
    await _start_ask(run_id)
    async with db.session_factory()() as session:
        assert await session_kinds.next_kind(session, OWNER) == session_kinds.COACH
        first = await session_kinds.hand_out(session, OWNER, runner="r1")
        second = await session_kinds.hand_out(session, OWNER, runner="r2")
        await session.commit()
    assert first["next"] == "coach" and first["item"]["ids"]
    assert second == {"next": None}, "the one job is claimed for the first runner"


async def test_the_coach_jobs_instructions_and_template_are_files_the_process_and_how_they_work_name(client):
    assert "jobsearch.coach.clarify" in harness_instructions.names("instructions")
    assert "jobsearch.coach.clarify" in harness_instructions.names("templates")
    researcher = next(a for a in session_kinds.AGENT_KINDS if a.key == pool.RESEARCHER)
    assert ("instructions", "jobsearch.coach.clarify") in researcher.files
    assert ("templates", "jobsearch.coach.clarify") in researcher.files
    assert process.library().get(process.CLARIFY_GAPS) is not None
    run_id = await _finished_job_run()
    job_id = await _start_ask(run_id)
    async with db.session_factory()() as session:
        job = await session.get(GapCoachJob, uuid.UUID(job_id))
        where = await process.engine().where_is(session, job.instance_id)
    assert where["status"] == "waiting" and where["step"] == process.CLARIFY_STEP
    (wait,) = where["waiting_for"]
    assert wait["payload"]["role"] == "coach" and wait["payload"]["instructions"] == "jobsearch.coach.clarify"


async def test_a_coach_prep_is_unaffected_when_no_clarify_job_waits(client):
    async with db.session_factory()() as session:
        assert await coach.free(session, OWNER) == []
    assert (await _coach())["done"] is True


async def test_a_waiting_interview_prep_is_handed_out_before_a_find_gaps_job(client):
    """An interview has a time, so its prep goes first, whichever was asked for first."""
    from tests.jobsearch.test_coach import _world

    run_id = await _finished_job_run()
    job_id = await _start_ask(run_id)
    _, _, prep_id = await _world()
    async with db.session_factory()() as session:
        assert (await coach.free(session, OWNER))[0] == prep_id
    first = await _coach()
    assert first["id"] == str(prep_id) and first["mode"] == "write"
    async with db.session_factory()() as session:
        assert (await coach.free(session, OWNER))[0] == uuid.UUID(job_id), "then the job"
    second = await _coach()
    assert second["id"] == job_id and second["mode"] == "clarify_ask"
