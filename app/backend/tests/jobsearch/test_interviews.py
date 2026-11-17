"""Interviews on a role in Active Discussion (docs/PRD.md 15.2.1, 15.2.2): added, edited and removed by hand, each with
a line in the role's History; adding one whose time is ahead starts the prep, editing starts nothing and says what
changed,
a role in another stage takes no new one. Everything is made up."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch import stages
from app.jobsearch.models import Interview, InterviewPrep, Opportunity
from app.models.work import TrailEvent
from tests.builders import save_documents
from tests.support import OWNER


async def _role(status=stages.ACTIVE_DISCUSSION) -> str:
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        await save_documents(session)
        role = Opportunity(owner=OWNER, slug="acme-lead", company="Acme", title="Program Lead", fit=4, origin="scout",
                           match_key="acme|program lead", status=status, found=datetime.now(UTC).date())
        session.add(role)
        await session.commit()
        return str(role.id)


def _body(days=3, **more) -> dict:
    when = (datetime.now(UTC) + timedelta(days=days)).replace(microsecond=0)
    return {"when": when.isoformat(), "round_kind": "hiring_manager", "label": "Team chat",
            "people": [{"name": "Sam Ortiz", "title": "Director of Programs"}], "notes": "Bring the plan.", **more}


async def _add(client, role_id, **more):
    return await client.post(f"/api/v1/roles/{role_id}/interviews", json=_body(**more))


async def test_an_interview_is_added_to_a_role_in_active_discussion_and_prep_starts_by_itself(client):
    role_id = await _role()
    res = await _add(client, role_id)
    assert res.status_code == 200, res.text
    view = res.json()
    assert view["round_label"] == "Hiring manager" and view["label"] == "Team chat" and view["past"] is False
    assert view["people"][0]["name"] == "Sam Ortiz" and view["people"][0]["id"]
    assert view["prep"]["state"] == "waiting" and view["prep"]["step"] == "Waiting for the Coach"
    detail = (await client.get(f"/api/v1/roles/{role_id}")).json()
    assert [i["id"] for i in detail["interviews"]] == [view["id"]]
    async with db.session_factory()() as session:
        said = (await session.scalars(select(TrailEvent.sentence).where(TrailEvent.event == "interview_added"))).all()
    assert len(said) == 1 and said[0].startswith("You added a hiring manager interview on ")


async def test_a_role_in_another_stage_takes_no_new_interview(client):
    role_id = await _role(stages.APPLIED)
    res = await _add(client, role_id)
    assert res.status_code == 409
    assert "Active Discussion" in res.json()["detail"]


async def test_an_interview_whose_time_has_passed_is_kept_and_starts_no_report(client):
    role_id = await _role()
    view = (await _add(client, role_id, days=-2)).json()
    assert view["past"] is True and view["prep"] is None and view["can_prepare_again"] is False
    async with db.session_factory()() as session:
        assert (await session.scalars(select(InterviewPrep))).all() == []


async def test_an_interview_needs_a_time_with_its_zone_a_known_round_and_named_people(client):
    role_id = await _role()
    assert (await _add(client, role_id, when="2026-12-01T10:00")).status_code == 422
    assert (await _add(client, role_id, round_kind="chat")).status_code == 422
    assert (await _add(client, role_id, people=[{"name": " ", "title": "x"}])).status_code == 422


async def test_editing_starts_no_report_and_the_card_says_what_changed_since_the_report(client):
    role_id = await _role()
    view = (await _add(client, role_id)).json()
    people = [*view["people"], {"name": "Dana Reyes", "title": "VP Operations"}]
    edited = await client.patch(f"/api/v1/interviews/{view['id']}", json={"people": people, "notes": "New note."})
    assert edited.status_code == 200, edited.text
    after = edited.json()
    assert after["notes"] == "New note." and len(after["people"]) == 2
    assert after["people"][0]["id"] == view["people"][0]["id"], "a person keeps their id when the list is edited"
    assert any("Dana Reyes" in line for line in after["prep"]["changed"])
    async with db.session_factory()() as session:
        assert len((await session.scalars(select(InterviewPrep))).all()) == 1


async def test_removing_an_interview_takes_its_reports_and_says_so_in_the_history(client):
    role_id = await _role()
    view = (await _add(client, role_id)).json()
    assert (await client.delete(f"/api/v1/interviews/{view['id']}")).json() == {"ok": True}
    async with db.session_factory()() as session:
        assert (await session.scalars(select(Interview))).all() == []
        assert (await session.scalars(select(InterviewPrep))).all() == []
        said = (await session.scalars(select(TrailEvent.sentence).where(TrailEvent.event == "interview_removed"))).all()
    assert len(said) == 1 and said[0].startswith("You removed the interview on ")
    assert (await client.delete(f"/api/v1/interviews/{view['id']}")).status_code == 404


async def test_prepare_again_is_refused_while_a_version_is_open_and_makes_the_next_when_none_is(client):
    role_id = await _role()
    view = (await _add(client, role_id)).json()
    assert (await client.post(f"/api/v1/interviews/{view['id']}/prepare")).status_code == 409
    async with db.session_factory()() as session:
        prep = (await session.scalars(select(InterviewPrep))).one()
        prep.state = "failed"
        await session.commit()
    again = await client.post(f"/api/v1/interviews/{view['id']}/prepare")
    assert again.status_code == 200, again.text
    assert again.json()["prep"]["version"] == 2
    assert [v["version"] for v in again.json()["versions"]] == [2, 1]


async def test_no_master_resume_fails_the_prep_at_once_with_the_reason(client):
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        role = Opportunity(owner=OWNER, slug="acme-lead", company="Acme", title="Program Lead", fit=4, origin="scout",
                           match_key="acme|program lead", status=stages.ACTIVE_DISCUSSION,
                           found=datetime.now(UTC).date())
        session.add(role)
        await session.commit()
        role_id = str(role.id)
    view = (await _add(client, role_id)).json()
    assert view["prep"]["state"] == "failed"
    assert "no master resume" in view["prep"]["error"].lower()
