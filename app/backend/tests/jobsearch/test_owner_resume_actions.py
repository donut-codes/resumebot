"""The owner door's master-resume help actions (docs/PRD.md path 50.15): `resume.read`, `resume.propose`
(suggestions the owner accepts, edits or rejects on My Resume; nothing but their Accept saves a line) and
`postings.sample` (the text of a few recent open postings for one target role or title). Everything is made up;
no network, no model calls, no sleeping."""

import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app import db
from app.jobsearch import actors
from app.jobsearch.gaps import proposals
from app.jobsearch.main import app
from app.jobsearch.models import (
    ActionRun,
    CleansedPosting,
    JobPosting,
    Opportunity,
    ProfileVersion,
    Proposal,
    RawPosting,
)
from app.jobsearch.owner_actions import REGISTRY, resume_help, runner
from app.jobsearch.profile import store as profile_store
from app.models.work import TrailEvent
from app.planengine.budget import REPLY_CEILING_CHARS, payload_chars
from app.settings import settings
from tests.builders import RESUME, criteria, tab
from tests.support import EXAMPLE_SYSTEM, NOON, OWNER, load_profile, pin_clock

SAID = "At Northwind I ran the quarterly risk review for the board and cut its prep time in half. It took two analysts."
LINE = "ran the quarterly risk review for the board and cut its prep time in half"
JOB = "northwind-lead"
OTHER_JOB = "contoso-analyst"
WHEN = NOON - timedelta(days=1)

#: The made-up master with a second job, so a line of another job can be named.
MASTER = {**RESUME, "roles": [*RESUME["roles"], {
    "id": OTHER_JOB, "employer": "Contoso Ltd", "location": "Oakfield, ST", "title": "Analyst", "start": "2019",
    "end": "2022", "tools": [], "bullets": [{"id": "c-model", "text": "Built the forecasting model", "note": ""}],
    "evidence": []}]}


@pytest.fixture(autouse=True)
def acts_for_the_tester(monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)
    pin_clock(monkeypatch, NOON)


@pytest.fixture
async def master(js):
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "resume", MASTER, by="test")
        await session.commit()


@pytest.fixture
async def client(master):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _do(name: str, args: dict | None = None, dry: bool = False) -> dict:
    return await runner.do(OWNER, name, args, dry)


async def _read(name: str, args: dict | None = None) -> dict:
    async with db.session_factory()() as session:
        return (await runner.read(session, OWNER, name, args))["result"]


async def _count(model, *where) -> int:
    async with db.session_factory()() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*where)) or 0


async def _versions() -> int:
    return await _count(ProfileVersion, ProfileVersion.kind == "resume")


async def _proposals() -> list[Proposal]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(Proposal).order_by(Proposal.created_at, Proposal.key))).all())


def _line(**over) -> dict:
    return {"job": JOB, "kind": "new_line", "text": LINE, "said": SAID, "why": "shows the board work", **over}


async def _suggest(*lines: dict, dry: bool = False) -> dict:
    return await _do("resume.propose", {"lines": list(lines)}, dry)


async def _one_proposal(**over) -> str:
    got = await _suggest(_line(**over))
    assert got["ok"] is True, got
    return str((await _proposals())[0].id)


# --- the registry -----------------------------------------------------------------------------------------------------


def test_the_three_actions_are_registered_and_only_propose_writes():
    assert {"resume.read", "resume.propose", "postings.sample"} <= set(REGISTRY)
    assert [n for n in ("resume.read", "resume.propose", "postings.sample") if REGISTRY[n].writes] == ["resume.propose"]
    assert not [n for n in REGISTRY if "accept" in n or n == "resume.save"], "no action accepts or saves a master line"


# --- resume.read ------------------------------------------------------------------------------------------------------


async def test_resume_read_answers_the_saved_masters_jobs_line_ids_evidence_and_version(master):
    got = await _read("resume.read")
    assert got["version"] == 1
    job = next(r for r in got["resume"]["roles"] if r["id"] == JOB)
    assert [b["id"] for b in job["bullets"]] == ["b-risk", "b-kb"]
    assert job["evidence"][0]["id"] == "e-board" and got["resume"]["summary"] == RESUME["summary"]
    assert "how" not in got


async def test_resume_read_on_a_new_install_answers_version_zero_and_names_the_import(js):
    got = await _read("resume.read")
    assert got["version"] == 0 and got["resume"]["roles"] == []
    assert "resume.import" in got["how"]


async def test_resume_read_changes_nothing_and_leaves_no_activity_line(master):
    before = (await _versions(), await _count(Proposal), await _count(TrailEvent), await _count(ActionRun))
    await _read("resume.read")
    assert (await _versions(), await _count(Proposal), await _count(TrailEvent), await _count(ActionRun)) == before


# --- resume.propose ---------------------------------------------------------------------------------------------------


async def test_propose_stores_a_waiting_row_with_the_owners_words_and_the_master_gets_no_version(master):
    before = await _versions()
    got = await _suggest(_line())
    assert got["ok"] is True and got["result"]["suggested"] == 1 and got["result"]["skipped"] == []
    assert got["result"]["confirm_at"].endswith("#/me") and "never accept" in got["result"]["how"].lower()
    (row,) = await _proposals()
    assert (row.source, row.run_id, row.status, row.document) == ("owner.claude", None, "proposed", "resume")
    assert row.after == LINE and row.cites == {"question": "", "answer": SAID}
    assert row.part["job_id"] == JOB and row.part["kind"] == "new_line"
    assert row.theme == "Senior Manager, Northwind Labs"
    assert await _versions() == before, "the master is unchanged: only the owner's Accept saves a line"


async def test_propose_leaves_one_activity_line_that_says_the_owner_accepts_on_my_resume(master):
    await _suggest(_line())
    async with db.session_factory()() as session:
        line = (await session.scalars(select(TrailEvent).where(TrailEvent.actor == actors.CLAUDE_ACTOR))).one()
    assert line.sentence == ("Claude (on demand) suggested 1 lines for the master resume "
                             "(the owner accepts them on My Resume)")


@pytest.mark.parametrize("words", [
    LINE, "  Ran the quarterly risk review for the board and cut its prep time in half.  ",
    "the quarterly risk review for the board", "It took two analysts"])
async def test_propose_takes_words_that_are_a_cut_of_what_was_said_at_its_ends_or_in_its_middle(master, words):
    got = await _suggest(_line(text=words))
    assert got["ok"] is True and got["result"]["suggested"] == 1


@pytest.mark.parametrize("words", ["Ran the quarterly risk review for the board and halved its prep time",
                                   "cut its prep time in half and ran the quarterly risk review for the board"])
async def test_propose_refuses_reworded_words_in_words_and_stores_nothing(master, words):
    got = await _suggest(_line(text=words))
    assert got["ok"] is False and "not the owner's words" in got["reason"] and "no rewording" in got["reason"]
    assert await _count(Proposal) == 0


async def test_propose_refuses_words_that_are_not_found_in_what_was_said(master):
    got = await _suggest(_line(text="Managed a team of forty engineers"))
    assert got["ok"] is False and "lines[0]" in got["reason"] and await _count(Proposal) == 0


@pytest.mark.parametrize(("over", "says"), [
    ({"job": "no-such-job"}, "is not a job in the master"),
    ({"kind": "evidence", "line": "c-model"}, "is not a line of the job northwind-lead"),
    ({"kind": "evidence"}, "evidence goes on one of the job's lines"),
    ({"line": "b-risk"}, "a new line goes under a job, not a line"),
    ({"kind": "headline"}, "must be new_line or evidence"),
])
async def test_propose_refuses_an_unknown_job_a_line_of_another_job_and_a_line_where_none_goes(master, over, says):
    got = await _suggest(_line(**over))
    assert got["ok"] is False and says in got["reason"] and await _count(Proposal) == 0


async def test_propose_names_every_problem_at_once_and_stores_none_of_the_lines(master):
    got = await _suggest(_line(), _line(job="no-such-job"), _line(text=""), _line(said="  "),
                         _line(text="Something the owner never wrote"))
    assert got["ok"] is False and got["reason"].startswith("4 problems, nothing was stored")
    for n in (1, 2, 3, 4):
        assert f"lines[{n}]" in got["reason"]
    assert "lines[0]" not in got["reason"] and await _count(Proposal) == 0


async def test_propose_skips_words_already_waiting_accepted_or_rejected_and_says_which(master):
    waiting, accepted, rejected = "ran the quarterly risk review", "cut its prep time in half", "It took two analysts"
    for words in (accepted, rejected):
        assert (await _suggest(_line(text=words)))["ok"] is True
    ids = {p.after: str(p.id) for p in await _proposals()}
    async with db.session_factory()() as session:
        await proposals.accept(session, OWNER, ids[accepted])
        await proposals.reject(session, OWNER, ids[rejected])
        await session.commit()
    # one more a Find Gaps run made (the same words in the same place are the same suggestion, whoever made it)
    async with db.session_factory()() as session:
        session.add(Proposal(
            owner=OWNER, source=proposals.SOURCE, theme="Risk", after=waiting, status="proposed", created_at=WHEN,
            part={"job_id": JOB, "kind": "new_line", "line_id": None}, cites={"question": "q", "answer": SAID},
            key=proposals.key_of(JOB, "new_line", None, waiting)))
        await session.commit()
    before = await _count(Proposal)
    got = await _suggest(_line(text=waiting), _line(text=accepted), _line(text=rejected),
                         _line(text="the board and cut its prep"))
    assert got["result"]["suggested"] == 1 and await _count(Proposal) == before + 1
    assert got["result"]["skipped"] == [{"line": 0, "why": "waiting"}, {"line": 1, "why": "accepted"},
                                        {"line": 2, "why": "rejected"}]


async def test_propose_dry_run_changes_no_row_and_its_counts_equal_a_real_calls(master):
    lines = [_line(), _line(kind="evidence", line="b-risk", text="cut its prep time in half"), _line()]
    dry = await _suggest(*lines, dry=True)
    assert dry["said"].startswith("Nothing was changed")
    assert "would suggest 2 lines for the master resume" in dry["said"]
    assert (await _count(Proposal), await _count(TrailEvent)) == (0, 0)
    real = await _suggest(*lines)
    for key in ("suggested", "skipped", "places"):
        assert dry["result"][key] == real["result"][key]
    assert real["result"]["suggested"] == 2 and real["result"]["skipped"] == [{"line": 2, "why": "waiting"}]
    assert await _count(Proposal) == 2 and await _count(TrailEvent) == 1


# --- the owner's Accept and Reject on My Resume -----------------------------------------------------------------------


async def test_accepting_a_door_suggestion_saves_it_as_the_owners_save_and_says_who_suggested_it(master):
    proposal_id = await _one_proposal()
    before = await _versions()
    async with db.session_factory()() as session:
        version = await proposals.accept(session, OWNER, proposal_id)
        await session.commit()
    assert version == before + 1 == await _versions()
    async with db.session_factory()() as session:
        saved = (await session.scalars(select(ProfileVersion).where(
            ProfileVersion.kind == "resume").order_by(ProfileVersion.version.desc()).limit(1))).one()
        resume = (await profile_store.owner_documents(session, OWNER)).resume
    assert saved.saved_by == actors.BY_OWNER
    assert saved.why == "Suggested by your Claude from what you told it; accepted by you."
    assert next(r for r in resume.roles if r.id == JOB).bullets[-1].text == LINE


async def test_accepting_after_the_owners_edit_says_they_edited_it(master):
    proposal_id = await _one_proposal()
    async with db.session_factory()() as session:
        await proposals.accept(session, OWNER, proposal_id, "Ran the board's quarterly risk review")
        await session.commit()
        saved = (await session.scalars(select(ProfileVersion).where(
            ProfileVersion.kind == "resume").order_by(ProfileVersion.version.desc()).limit(1))).one()
    assert saved.why == "Suggested by your Claude from what you told it; accepted by you after you edited it."


async def test_an_accept_naming_an_older_master_version_is_refused_in_words_and_saves_nothing(master):
    proposal_id = await _one_proposal()
    async with db.session_factory()() as session:
        with pytest.raises(profile_store.StaleSave):
            await proposals.accept(session, OWNER, proposal_id, base_version=0)
    async with db.session_factory()() as session:
        await proposals.accept(session, OWNER, proposal_id, base_version=1)
        await session.commit()
    assert await _versions() == 2


async def test_evidence_whose_line_has_since_gone_is_titled_for_the_line_as_it_was_when_suggested(master):
    proposal_id = await _one_proposal(kind="evidence", line="b-risk", text="cut its prep time in half")
    async with db.session_factory()() as session:
        gone = (await profile_store.owner_documents(session, OWNER)).resume.model_dump(mode="json")
        gone["roles"][0]["bullets"] = [b for b in gone["roles"][0]["bullets"] if b["id"] != "b-risk"]
        await profile_store.save(session, OWNER, "resume", gone, by="test")
        await proposals.accept(session, OWNER, proposal_id)
        await session.commit()
        resume = (await profile_store.owner_documents(session, OWNER)).resume
    assert resume.roles[0].evidence[-1].title == "Evidence for: Led the risk working group across client engagements"


async def test_rejecting_a_door_suggestion_keeps_the_row_and_saves_nothing(master):
    proposal_id = await _one_proposal()
    before = await _versions()
    async with db.session_factory()() as session:
        await proposals.reject(session, OWNER, proposal_id)
        await session.commit()
    (row,) = await _proposals()
    assert row.status == "rejected" and await _versions() == before


async def test_the_open_list_holds_the_doors_waiting_suggestions_and_not_a_find_gaps_run_that_was_deleted(client):
    await _suggest(_line(), _line(text="It took two analysts"))
    async with db.session_factory()() as session:  # a Coach proposal whose run is gone has no run either
        session.add(Proposal(owner=OWNER, run_id=None, source=proposals.SOURCE, theme="Risk",
                             part={"job_id": JOB, "kind": "new_line", "line_id": None}, after="a coach line",
                             key="k" * 40, cites={"question": "q", "answer": "a"}, status="proposed", created_at=WHEN))
        await session.commit()
    first = next(p for p in await _proposals() if p.after == LINE)
    async with db.session_factory()() as session:
        await proposals.reject(session, OWNER, str(first.id))
        await session.commit()
    got = (await client.get("/api/v1/find-gaps/proposals/open")).json()
    assert got["version"] == 1
    assert [p["text"] for p in got["proposals"]] == ["It took two analysts"], "door rows only, still waiting"
    assert got["proposals"][0]["source"] == "owner.claude" and got["proposals"][0]["quote"] == SAID
    assert got["proposals"][0]["place"] == {"kind": "new_line", "job": "Senior Manager, Northwind Labs", "line": None}


async def test_the_accept_route_passes_the_loaded_version_and_a_newer_master_answers_in_words(client):
    proposal_id = await _one_proposal()
    stale = await client.post(f"/api/v1/find-gaps/proposals/{proposal_id}/accept", json={"base_version": 0})
    assert stale.status_code == 409 and await _versions() == 1
    ok = await client.post(f"/api/v1/find-gaps/proposals/{proposal_id}/accept", json={"base_version": 1})
    assert ok.status_code == 200 and ok.json()["version"] == 2


# --- postings.sample --------------------------------------------------------------------------------------------------

BODY = "Own the programs and report to the director. " * 12  # 540 characters


async def _posting(title: str, *, days: int = 0, text: str | None = BODY, company: str = "Fabrikam", closed=False,
                   not_checked=False, target: str | None = None, screened: str | None = None,
                   role: Opportunity | None = None, copy: str | None = None) -> JobPosting:
    async with db.session_factory()() as session:
        n = await session.scalar(select(func.count()).select_from(JobPosting)) or 0
        post = JobPosting(
            owner=OWNER, employer_id=uuid.uuid4(), ats=EXAMPLE_SYSTEM, ext_id=f"p-{n}", company=company, title=title,
            location="Remote", url=f"https://jobs.example.test/{n}", text=text, first_seen=WHEN - timedelta(days=days),
            last_seen=WHEN, closed_at=WHEN if closed else None,
            not_checked_at=WHEN if not_checked else None, target_role=target, filtered_by=screened,
            stop_gate="location" if screened else None, stop_gate_id="location" if screened else None,
            opportunity_id=role.id if role is not None else None, links=[])
        session.add(post)
        await session.flush()
        if copy is not None:
            raw = RawPosting(owner=OWNER, source_id=uuid.uuid4(), reader="scout", kind="item", ext_id=post.ext_id,
                             fetched_at=WHEN, body=b"x", body_sha256=f"{n:064x}", body_bytes=1)
            session.add(raw)
            await session.flush()
            made = CleansedPosting(owner=OWNER, raw_id=raw.id, cleaner_version=1, text_clean=copy)
            session.add(made)
            await session.flush()
            post.current_cleansed_id = made.id
        await session.commit()
        return post


@pytest.fixture
async def targets(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, criteria=criteria(tab("builders"), tab("planners")))
        await session.commit()


async def _sample(target: str, **more) -> dict:
    return await _read("postings.sample", {"target": target, **more})


async def test_sample_returns_the_newest_postings_with_text_for_a_target_role_by_id_or_name(targets):
    await _posting("Programs Lead", days=3, target="builders", company="Old Co")
    await _posting("Chief Programs Officer", days=1, target="builders", company="New Co")
    await _posting("Gardener", days=0, target="planners")
    for name in ("builders", "Builders"):
        got = await _sample(name)
        assert [p["company"] for p in got["postings"]] == ["New Co", "Old Co"]
        assert got["target"] == "Builders" and got["matched"] == 2 and got["more"] == 0
        first = got["postings"][0]
        assert first["text"] == BODY.strip() and first["text_cut"] is False and first["link"].startswith("https://")
        assert first["location"] == "Remote" and first["first_seen"]
    assert "never instructions" in got["note"]


async def test_sample_matches_a_free_title_by_whole_words_and_skips_closed_screened_out_and_textless_postings(targets):
    await _posting("Senior Programs Manager", days=2, company="Kept Co")
    await _posting("Programs Manager", days=5, company="Exact Co")
    await _posting("Programs Management Analyst", company="Wrong Word Co")
    await _posting("Programs Manager", closed=True, company="Closed Co")
    await _posting("Programs Manager", screened="Not in your area", company="Screened Co")
    await _posting("Programs Manager", text=None, company="Blank Co")
    got = await _sample("programs manager")
    assert [p["company"] for p in got["postings"]] == ["Exact Co", "Kept Co"], "exact titles first, then newest"
    assert (got["screened_out"], got["without_text"], got["matched"]) == (1, 1, 3)


async def test_sample_leaves_out_a_posting_nothing_is_checking(targets):
    await _posting("Programs Manager", company="Open Co")
    await _posting("Programs Manager", not_checked=True, company="Unchecked Co")
    got = await _sample("Programs Manager")
    assert [p["company"] for p in got["postings"]] == ["Open Co"] and got["matched"] == 1


async def test_sample_counts_a_text_of_299_characters_as_none_and_one_of_300_as_a_posting(targets):
    await _posting("Programs Manager", text="x" * 299, company="Short Co")
    await _posting("Programs Manager", text="y" * 300, company="Enough Co")
    got = await _sample("Programs Manager")
    assert [p["company"] for p in got["postings"]] == ["Enough Co"] and got["without_text"] == 1


async def test_sample_reads_a_roles_cleansed_copy_before_the_postings_own_text(targets):
    async with db.session_factory()() as session:
        role = Opportunity(owner=OWNER, slug="fabrikam-pm", company="Fabrikam", title="Programs Manager",
                           origin="scout", match_key="fabrikam|programs manager", status="second_impression")
        session.add(role)
        await session.commit()
    await _posting("Programs Manager", text="raw words " * 40, role=role, copy="clean words " * 40)
    got = await _sample("Programs Manager")
    assert got["postings"][0]["text"].startswith("clean words")


async def test_sample_cuts_a_long_text_at_6000_and_says_so(targets):
    await _posting("Programs Manager", text="word " * 3_000, company="Long Co")
    got = await _sample("Programs Manager")
    (posting,) = got["postings"]
    assert posting["text_cut"] is True and len(posting["text"]) == 6_000 and posting["characters"] == 14_999


async def test_sample_holds_the_limit_at_ten_and_counts_the_rest(targets):
    for n in range(12):
        await _posting("Programs Manager", days=n, company=f"Co {n}")
    got = await _sample("Programs Manager", limit=50)
    assert len(got["postings"]) == 10 and got["matched"] == 12 and got["more"] == 2
    assert len((await _sample("Programs Manager", limit=2))["postings"]) == 2


async def test_sample_holds_the_whole_reply_to_40000_characters_of_text_well_under_the_ceiling(targets):
    for n in range(10):
        await _posting("Programs Manager", days=n, text="word " * 3_000, company=f"Co {n}")
    got = await _sample("Programs Manager", limit=10)
    assert sum(len(p["text"]) for p in got["postings"]) <= resume_help.TOTAL_CHARS
    assert len(got["postings"]) == 7 and got["more"] == 3, "the 7th is cut short and the rest are counted, not sent"
    assert payload_chars(got) < REPLY_CEILING_CHARS


async def test_sample_with_no_match_answers_an_empty_list_and_says_so(targets):
    got = await _sample("Underwater Welder")
    assert got["postings"] == [] and got["matched"] == 0 and "No open posting" in got["note"]


async def test_the_sample_reads_only_what_the_app_holds_and_changes_no_row(targets):
    await _posting("Programs Manager")
    before = (await _count(JobPosting), await _count(TrailEvent), await _count(ActionRun))
    await _sample("Programs Manager")
    assert (await _count(JobPosting), await _count(TrailEvent), await _count(ActionRun)) == before
