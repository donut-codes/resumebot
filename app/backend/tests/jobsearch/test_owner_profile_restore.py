"""`profile.restore`, the owner door's way to bring back an earlier saved version of a settings part (docs/PRD.md path
50.5 and 50.13): a dry run says in plain words what would change, a real run saves a new version equal to the old one
through the store's own restore (the one the page's History calls) and runs the after-save work a save on screen runs,
and the resume is never one of its kinds. Everything is made up; no network, no model calls, no sleeping."""

import copy
import uuid
from datetime import date

import pytest
from sqlalchemy import func, select, text

from app import db
from app.errors import Conflict, DomainError
from app.jobsearch import stages
from app.jobsearch.actors import CLAUDE
from app.jobsearch.models import ActionRun, Opportunity, ProfileVersion
from app.jobsearch.owner_actions import REGISTRY, runner
from app.jobsearch.owner_actions import writes as owner_writes
from app.jobsearch.profile import after_save
from app.jobsearch.profile import store as profile_store
from app.jobsearch.research import prioritize
from app.models.work import TrailEvent
from app.settings import settings
from tests.support import OWNER, PROFILE, load_profile

TODAY = date(2026, 10, 12)
KINDS = ["criteria", "gates", "scoring", "settings", "about", "writing"]


@pytest.fixture(autouse=True)
def acts_for_the_tester(monkeypatch):
    monkeypatch.setattr(settings, "owner", OWNER)


def gates(floor: int) -> dict:
    """Remote work only, and at least `floor` a year on the posted maximum."""
    return {"location": {"remote": {"on": True}}, "compensation": {"amount": floor, "against": "maximum"}}


def an_ask(ident: str, points: int) -> dict:
    """A line of Prioritization the app checks itself (an employer's fact): one the page's save takes."""
    return {"id": ident, "section": "company", "says": f"Made-up line {ident}", "points": points, "checked_by": "app",
            "fact": "ownership", "op": "in", "value": "public"}


#: A sentence rule of Hard pass and an ask of Prioritization that only a reader could judge: what the page's save has
#: refused since 2026-10-26 (docs/PRD.md 30, 46) and an old version may still hold.
READER_RULE = {"id": "words", "kind": "rule", "section": "description", "text": "A sentence a reader judges."}
READER_ASK = {"id": "team", "section": "description", "says": "Leads a team", "points": 1}

#: One version 1 and one version 2 of every kind that differ in one field, the second what the owner "changed to".
#: Each holds only lines the page's save takes, since a restore is refused what a save is.
FIRST = {"criteria": PROFILE["criteria"], "gates": gates(95_000), "scoring": {"asks": [an_ask("public", 2)]},
         "settings": PROFILE["settings"], "about": {"name": "Pat Example", "location": "Riverton"},
         "writing": {"voice_rules": [{"id": "plain", "says": "Write plainly."}]}}
SECOND = {"criteria": {**PROFILE["criteria"], "company": "A very different place."}, "gates": gates(50_000),
          "scoring": {"asks": [an_ask("public", 2), an_ask("extra", 1)]},
          "settings": {**PROFILE["settings"], "comp_round_to": 1_000},
          "about": {"name": "Pat Example", "location": "Elsewhere"},
          "writing": {"voice_rules": [{"id": "plain", "says": "Write plainly and briefly."}]}}


async def _two_versions(*kinds: str) -> None:
    """Version 1 and then version 2 of each kind, saved as the owner would (the store's save, no after-save work)."""
    async with db.session_factory()() as session:
        for kind in kinds:
            await profile_store.save(session, OWNER, kind, copy.deepcopy(FIRST[kind]), by="owner")
        for kind in kinds:
            assert await profile_store.save(session, OWNER, kind, copy.deepcopy(SECOND[kind]), by="owner") == 2
        await session.commit()


async def _rows(kind: str) -> list[ProfileVersion]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(ProfileVersion).where(
            ProfileVersion.owner == OWNER, ProfileVersion.kind == kind).order_by(ProfileVersion.version))).all())


async def _snapshot(skip: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """Every row of every table, as text: what a dry run must leave as it found it."""
    async with db.session_factory()() as session:
        listed = await session.execute(text(
            "select name from sqlite_master where type = 'table' and name not like 'sqlite_%' order by 1"))
        tables = listed.scalars().all()
        return {t: sorted(repr(tuple(r)) for r in (await session.execute(text(f'select * from "{t}"'))).all())
                for t in tables if t not in skip}


async def _claude_lines() -> list[TrailEvent]:
    async with db.session_factory()() as session:
        return list((await session.scalars(select(TrailEvent).where(TrailEvent.actor == "claude"))).all())


async def _do(args: dict, dry: bool = False) -> dict:
    return await runner.do(OWNER, "profile.restore", args, dry)


# --- what it takes ----------------------------------------------------------------------------------------------------


def test_it_takes_a_kind_and_a_version_and_the_kinds_are_the_six_the_owner_edits_and_never_the_resume():
    listed = REGISTRY["profile.restore"].listed()
    assert listed["writes"] is True and listed["dry_run"] is True and listed["long"] is True
    assert listed["required"] == ["kind", "version"]
    assert listed["inputs"]["kind"]["enum"] == KINDS and "resume" not in listed["inputs"]["kind"]["enum"]
    assert owner_writes.RESTORABLE == tuple(KINDS)


@pytest.mark.parametrize("args", [{"kind": "resume", "version": 1}, {"kind": "nonsense", "version": 1},
                                  {"kind": "gates", "version": 0}, {"kind": "gates", "version": -2},
                                  {"kind": "gates"}, {"version": 1}, {"kind": "gates", "version": 1, "why": "x"}])
def test_a_kind_that_is_not_one_of_the_six_or_a_version_that_cannot_be_one_is_refused(args):
    with pytest.raises(DomainError, match="profile.restore was sent arguments it does not take"):
        runner.check_args(REGISTRY["profile.restore"], args)


async def test_a_resume_is_never_brought_back_and_none_of_its_versions_move(js):
    async with db.session_factory()() as session:
        for words in ("first words", "second words"):
            await profile_store.save(session, OWNER, "resume", {"summary": words}, by="owner")
        await session.commit()
    before = await _rows("resume")
    for dry in (True, False):
        with pytest.raises(DomainError, match="kind"):
            await _do({"kind": "resume", "version": 1}, dry)
    assert [(r.version, r.data, r.why) for r in await _rows("resume")] == [(r.version, r.data, r.why) for r in before]
    assert len(before) == 2 and not await _claude_lines()


# --- the dry run ------------------------------------------------------------------------------------------------------


async def test_a_dry_run_says_in_plain_words_what_would_change_and_changes_nothing(js):
    await _two_versions("gates")
    before = await _snapshot(skip=("js_action_runs",))
    got = await _do({"kind": "gates", "version": 1}, dry=True)
    assert got["ok"] is True and got["dry_run"] is True and list(got)[1] == "said"
    assert got["said"].startswith("Nothing was changed (dry run). A real call would ")
    assert "version 1 of the gates" in got["said"] and "changed compensation" in got["said"]
    assert "version 3" in got["said"], "it says which version it would make"
    assert got["result"]["changes"] == ["changed compensation"]
    assert (got["result"]["kind"], got["result"]["restored"], got["result"]["in_force"]) == ("gates", 1, 2)
    assert got["result"]["version"] == 3, "the version a real call would make"
    assert await _snapshot(skip=("js_action_runs",)) == before
    assert not await _claude_lines(), "a dry run adds no Activity line, only its job row"


async def test_a_dry_run_names_what_is_added_removed_and_changed_by_the_lines_id(js):
    await _two_versions("scoring")
    got = await _do({"kind": "scoring", "version": 1}, dry=True)
    assert got["result"]["changes"] == ["removed asks: extra"]
    assert "removed asks: extra" in got["said"]


async def test_a_dry_run_of_the_version_in_force_says_nothing_would_change(js):
    await _two_versions("gates")
    got = await _do({"kind": "gates", "version": 2}, dry=True)
    assert "save nothing: version 2 of the gates is the version in force" in got["said"]
    assert got["result"]["changes"] == [] and got["result"]["version"] is None


async def test_an_earlier_version_with_the_same_words_as_the_one_in_force_would_save_nothing_either(js):
    async with db.session_factory()() as session:
        await profile_store.save(session, OWNER, "gates", gates(95_000), by="owner")
        await profile_store.save(session, OWNER, "gates", gates(50_000), by="owner")
        await profile_store.save(session, OWNER, "gates", gates(95_000), by="owner")
        await session.commit()
    got = await _do({"kind": "gates", "version": 1}, dry=True)
    assert "save nothing" in got["said"] and "the same as the version in force" in got["said"]
    assert got["result"]["version"] is None and got["result"]["in_force"] == 3


async def test_a_version_that_is_not_there_is_refused_in_the_stores_words_for_a_dry_run_and_a_real_one(js):
    await _two_versions("gates")
    dry = await _do({"kind": "gates", "version": 9}, dry=True)
    assert dry["ok"] is True and "would be refused: there is no version 9 of the gates" in dry["said"]
    real = await _do({"kind": "gates", "version": 9})
    assert real["ok"] is False and real["state"] == "failed" and "there is no version 9 of the gates" in real["reason"]
    assert len(await _rows("gates")) == 2 and not await _claude_lines()


# --- what a save refuses, a restore refuses (docs/PRD.md 50.13) -------------------------------------------------------


async def _save_as_before_the_rules(kind: str, *documents: dict) -> None:
    """Versions the owner saved before the rules of 2026-10-26 and 2026-10-27 (the store itself refuses nothing)."""
    async with db.session_factory()() as session:
        for data in documents:
            await profile_store.save(session, OWNER, kind, copy.deepcopy(data), by="owner")
        await session.commit()


async def test_a_version_with_a_reader_judged_line_is_refused_as_a_save_would_for_a_dry_run_and_a_real_one(js):
    await _save_as_before_the_rules("gates", {**gates(95_000), "other": [READER_RULE]}, gates(50_000))
    before = await _snapshot(skip=("js_action_runs",))
    dry = await _do({"kind": "gates", "version": 1}, dry=True)
    assert dry["ok"] is True and "would be refused: the gates take only what the app checks itself" in dry["said"]
    assert "no reader judges a line: words" in dry["said"]
    assert await _snapshot(skip=("js_action_runs",)) == before
    real = await _do({"kind": "gates", "version": 1})
    assert real["ok"] is False and real["state"] == "failed" and "no reader judges a line: words" in real["reason"]
    assert len(await _rows("gates")) == 2 and not await _claude_lines(), "nothing was saved, no Activity line"


async def test_a_prioritization_with_a_reader_ask_is_refused_and_the_version_in_force_stays(js):
    await _save_as_before_the_rules("scoring", {"asks": [an_ask("public", 2), READER_ASK]},
                                    {"asks": [an_ask("public", 2)]})
    dry = await _do({"kind": "scoring", "version": 1}, dry=True)
    assert "would be refused" in dry["said"] and "no reader judges a line: team" in dry["said"]
    real = await _do({"kind": "scoring", "version": 1})
    assert real["ok"] is False and "no reader judges a line: team" in real["reason"]
    assert len(await _rows("scoring")) == 2 and not await _claude_lines()


async def test_a_version_with_negative_points_is_refused_unless_the_version_in_force_holds_the_same_points(js):
    await _save_as_before_the_rules("scoring", {"asks": [an_ask("drag", -2), an_ask("fine", 1)]},
                                    {"asks": [an_ask("fine", 1)]})
    dry = await _do({"kind": "scoring", "version": 1}, dry=True)
    assert "would be refused: Prioritization only adds points" in dry["said"] and "drag" in dry["said"]
    real = await _do({"kind": "scoring", "version": 1})
    assert real["ok"] is False and "Prioritization only adds points" in real["reason"] and "drag" in real["reason"]
    assert len(await _rows("scoring")) == 2 and not await _claude_lines()
    # A line the owner has in force from before the rule, with the same points, is let through, as in a save.
    await _save_as_before_the_rules("scoring", {"asks": [an_ask("drag", -2), an_ask("fine", 3)]})
    allowed = await _do({"kind": "scoring", "version": 1})
    assert allowed["ok"] is True and allowed["result"]["version"] == 4
    assert (await _rows("scoring"))[3].data["asks"][1]["points"] == 1


# --- the real run -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_a_real_run_saves_a_new_version_equal_to_the_old_one_and_says_why(js, kind):
    await _two_versions(kind)
    got = await _do({"kind": kind, "version": 1})
    assert got["ok"] is True and got["result"]["version"] == 3 and got["result"]["restored"] == 1
    first, second, third = await _rows(kind)
    assert third.data == first.data != second.data, "the new version is the old one"
    assert third.why == "restored version 1 on the owner's ask" and third.saved_by == CLAUDE
    assert (second.why, second.saved_by) == ("", "owner"), "an earlier version is never changed"
    history = await _history(kind)
    assert history[0]["version"] == 3 and history[0]["why"] == "restored version 1 on the owner's ask"


async def _history(kind: str) -> list[dict]:
    async with db.session_factory()() as session:
        return await profile_store.history(session, OWNER, kind)


async def test_a_real_run_adds_one_activity_line_as_claude_naming_the_part_and_the_versions(js):
    await _two_versions("gates")
    got = await _do({"kind": "gates", "version": 1})
    [line] = await _claude_lines()
    assert (line.who, line.actor) == ("Claude (on demand)", "claude")
    assert line.sentence == "Claude (on demand) restored version 1 of the gates as version 3"
    assert got["job"] in (line.detail or "") and "profile.restore" in (line.detail or "")
    assert got["said"] == "Restored version 1 of the gates as version 3."
    async with db.session_factory()() as session:
        job = await session.get(ActionRun, uuid.UUID(got["job"]))
    assert job is not None and (job.by, job.state, job.dry_run) == (CLAUDE, "done", False)


async def test_a_real_run_of_the_version_in_force_saves_nothing_and_says_so(js):
    await _two_versions("gates")
    got = await _do({"kind": "gates", "version": 2})
    assert got["ok"] is True and got["result"]["version"] is None
    assert got["said"] == "Found version 2 of the gates already in force: nothing was saved."
    assert len(await _rows("gates")) == 2


async def test_it_calls_the_store_restore_the_page_calls_and_the_page_still_says_brought_back(js, client, monkeypatch):
    await _two_versions("gates")
    calls = []
    real = profile_store.restore

    async def spy(*a, **kw):
        calls.append((a[2], a[3], kw["by"], kw.get("why")))
        return await real(*a, **kw)

    monkeypatch.setattr(profile_store, "restore", spy)
    await _do({"kind": "gates", "version": 1})
    page = await client.post("/api/v1/profile/gates/versions/2/restore")
    assert page.status_code == 200 and page.json()["version"] == 4
    assert calls == [("gates", 1, CLAUDE, "restored version 1 on the owner's ask"), ("gates", 2, "owner", None)]
    rows = await _rows("gates")
    assert rows[2].why == "restored version 1 on the owner's ask" and rows[3].why == "brought back version 2"
    assert rows[3].data == rows[1].data


# --- the after-save work of a save on screen --------------------------------------------------------------------------


def _role(slug: str, top: int) -> Opportunity:
    return Opportunity(owner=OWNER, slug=slug, company="Fabrikam", title=f"Lead {slug}", fit=3, origin="scout",
                       status=stages.SECOND_IMPRESSION, match_key=f"fabrikam|lead {slug}", found=TODAY,
                       location="Remote", move_type="remote", comp=f"${top - 10_000:,}-${top:,}")


async def test_bringing_back_hard_pass_checks_every_role_again_as_a_save_on_screen_does(js):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER, gates=gates(95_000))
        await profile_store.save(session, OWNER, "gates", gates(50_000), by="owner")
        session.add_all([_role("low", 80_000), _role("ok", 200_000)])
        await session.commit()
    got = await _do({"kind": "gates", "version": 1})
    assert got["ok"] is True and got["result"]["after_save"] == "Re-checked 0 postings and 2 roles: 1 newly stopped."
    async with db.session_factory()() as session:
        roles = {r.slug: r for r in (await session.scalars(select(Opportunity))).all()}
        assert (roles["low"].status, roles["ok"].status) == (stages.GATED_OUT, stages.SECOND_IMPRESSION)
        said = [e for e in (await session.scalars(select(TrailEvent).where(
            TrailEvent.link_id == roles["low"].id, TrailEvent.event == "regate_stopped"))).all()]
        assert len(said) == 1 and "Hard pass version 3 was made by bringing version 1 back" in (said[0].detail or "")
        assert "on the owner's ask" in (said[0].detail or "")


async def test_bringing_back_prioritization_works_every_score_out_again(js, monkeypatch):
    await _two_versions("scoring")
    calls = []

    async def after(session, owner, *, before, after, version):
        calls.append((owner, version, [a.id for a in before.scoring.asks], [a.id for a in after.scoring.asks]))
        return "Worked out 4 scores again: 1 changed."

    monkeypatch.setattr(prioritize, "after_save", after)
    got = await _do({"kind": "scoring", "version": 1})
    [(owner, version, was, now)] = calls
    assert (owner, version) == (OWNER, 3) and "extra" in was and "extra" not in now
    assert got["result"]["after_save"] == "Worked out 4 scores again: 1 changed."


@pytest.mark.parametrize("kind", ["criteria", "settings", "about"])
async def test_a_part_whose_save_runs_nothing_again_says_no_line(js, kind):
    await _two_versions(kind)
    got = await _do({"kind": kind, "version": 1})
    assert got["ok"] is True and got["result"]["after_save"] == ""


async def test_the_page_and_the_door_run_the_one_after_save_function(js, client, monkeypatch):
    await _two_versions("gates")
    seen = []
    real = after_save.run

    async def spy(session, owner, kind, version, how, said):
        seen.append((kind, version, how))
        return await real(session, owner, kind, version, how, said)

    monkeypatch.setattr(after_save, "run", spy)
    await _do({"kind": "gates", "version": 1})
    await client.post("/api/v1/profile/gates/versions/2/restore")
    assert seen == [("gates", 3, "was made by bringing version 1 back on the owner's ask"),
                    ("gates", 4, "was made by bringing version 2 back")]


# --- one at a time ----------------------------------------------------------------------------------------------------


async def test_a_second_restore_is_refused_naming_the_job_that_runs_and_a_dry_run_says_so(js):
    await _two_versions("gates")
    from datetime import UTC, datetime

    async with db.session_factory()() as session:
        row = ActionRun(owner=OWNER, action="profile.restore", arguments={}, dry_run=False, by=CLAUDE,
                        state="running", started=datetime(2026, 10, 12, 5, 0, tzinfo=UTC))
        session.add(row)
        await session.commit()
        job = row.id
    with pytest.raises(Conflict) as refused:
        await _do({"kind": "gates", "version": 1})
    assert refused.value.status == 409 and str(job) in str(refused.value) and "running already" in str(refused.value)
    dry = await _do({"kind": "gates", "version": 1}, dry=True)
    assert "would be refused" in dry["said"] and str(job) in dry["said"]
    async with db.session_factory()() as session:
        assert await session.scalar(select(func.count()).select_from(ActionRun).where(
            ActionRun.action == "profile.restore", ActionRun.dry_run.is_(False))) == 1, "a refused call leaves no row"
    assert len(await _rows("gates")) == 2


async def _committed_versions(kind: str) -> list[int]:
    """The versions of `kind` another program would read at this moment (a reader sees only what is committed)."""
    async with db.read_session_factory()() as reader:
        return list((await reader.scalars(select(ProfileVersion.version).where(
            ProfileVersion.owner == OWNER, ProfileVersion.kind == kind).order_by(ProfileVersion.version))).all())


async def test_the_door_commits_a_restored_prioritization_before_it_works_the_scores_out(js, monkeypatch):
    await _two_versions("scoring")
    seen: list[list[int]] = []

    async def after(session, owner, *, before, after, version):
        seen.append(await _committed_versions("scoring"))
        return "Worked out 4 scores again: 1 changed."

    monkeypatch.setattr(prioritize, "after_save", after)
    got = await _do({"kind": "scoring", "version": 1})
    assert seen == [[1, 2, 3]], "the restored version was committed before the scores were worked out"
    assert got["ok"] is True and got["result"]["after_save"] == "Worked out 4 scores again: 1 changed."


async def test_the_door_commits_a_saved_prioritization_before_it_works_the_scores_out(js, monkeypatch):
    await _two_versions("scoring")
    seen: list[list[int]] = []

    async def after(session, owner, *, before, after, version):
        seen.append(await _committed_versions("scoring"))
        return "Worked out 4 scores again: 1 changed."

    monkeypatch.setattr(prioritize, "after_save", after)
    data = {"asks": [an_ask("public", 2), an_ask("more", 3)]}
    got = await runner.do(OWNER, "profile.save", {"kind": "scoring", "data": data}, False)
    assert seen == [[1, 2, 3]], "the saved version was committed before the scores were worked out"
    assert got["ok"] is True and got["result"]["version"] == 3
    assert got["result"]["after_save"] == "Worked out 4 scores again: 1 changed."


async def test_a_dry_run_of_a_save_works_no_score_out(js, monkeypatch):
    await _two_versions("scoring")
    seen: list = []

    async def after(session, owner, *, before, after, version):
        seen.append(version)
        return "never"

    monkeypatch.setattr(prioritize, "after_save", after)
    data = {"asks": [an_ask("public", 2), an_ask("more", 3)]}
    got = await runner.do(OWNER, "profile.save", {"kind": "scoring", "data": data}, True)
    assert got["ok"] is True and got["dry_run"] is True and seen == []
    assert await _committed_versions("scoring") == [1, 2]
