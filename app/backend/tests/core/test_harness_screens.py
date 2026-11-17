"""The harness screens' API: the roster, Pause, How they work (the processes, what the scout is sent, a real hand-out),
the live view of a run, Activity, and the Inbox and task list a search fills. Synthetic data only."""

import uuid

import pytest
from sqlalchemy import select

from app import db
from app.agents import roster
from app.jobsearch.doors import tools
from app.jobsearch.missions import search_run as run
from app.jobsearch.models import SearchRun
from app.models.agent import AgentConnection, RoleDef
from app.models.handout import TaskHandout
from app.settings import VERSION
from tests.builders import api_url
from tests.support import OWNER, load_profile, reported


async def _start_search(focus="banks"):
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        started = await run.start_round(session, OWNER, started_by="manual", focus=focus)
        await session.commit()
        return started.id, started.instance_id


async def _finish_search(run_id, roles, done=True):
    async with db.session_factory()() as session:
        row = await session.get(SearchRun, run_id)
        await run.stage_report(session, row, roles, done=done)
        await session.commit()


# --- who is here -----------------------------------------------------------------------------------------------


async def test_me_names_the_app_version(client):
    assert (await client.get("/api/v1/me")).json()["version"] == VERSION


async def test_me_names_the_owners_time_zone_for_the_page_to_write_times_in(client, monkeypatch):
    monkeypatch.setattr("app.timeutil.settings.timezone", "Pacific/Kiritimati")
    assert (await client.get("/api/v1/me")).json()["timezone"] == "Pacific/Kiritimati"


# --- the roster ------------------------------------------------------------------------------------------------


async def _started() -> None:
    """The roster as the app's start leaves it (the Agents page only reads it)."""
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        await session.commit()


async def test_the_scout_is_on_the_roster_with_its_role_and_a_new_role_needs_no_code(client):
    await _started()
    agents = (await client.get(api_url("agents"))).json()
    assert len(agents) == 1
    scout = agents[0]
    assert scout["name"] == "Scout" and scout["roles"] == ["scout", "analyst", "writer"]
    assert scout["roles_words"].startswith("Scout") and "writer" in scout["roles_words"].lower()
    assert scout["status"] == "offline" and scout["availability"] == "Offline" and scout["paused"] is False
    assert scout["current"] is None and scout["capacity"]["free"] is False
    # a Reviewer is a new row, and a connection that holds it reads it with no code change
    async with db.session_factory()() as session:
        session.add(RoleDef(key="reviewer", title="Reviewer", about="Reads packets.", sort=30))
        conn = await roster.connection_by_slug(session, OWNER, "scout")
        conn.roles = ["scout", "writer", "reviewer"]
        await session.commit()
    await _started()  # the start mends the roster (`ensure_roster`); the read only reads
    words = (await client.get(api_url("agents"))).json()[0]["roles_words"]
    assert words == "Scout, writer, reviewer and analyst", "a runner always gains the analyst role"


async def test_what_the_runner_may_do_tells_each_kind_of_work_once_and_what_it_never_does_once(client):
    """The runner holds three roles whose words each end "Never ...": the Agents page shows them together, so each kind
    is told once under its own title and the "never" is said once, not three times."""
    async with db.session_factory()() as session:
        # An older install's roles carry the old words: starting the app brings them up to date.
        await roster.ensure_roster(session, OWNER)
        scout = await session.get(RoleDef, "scout")
        scout.about = "Learns how to read each site. Never applies or fills a form."
        await session.commit()
        await roster.ensure_roster(session, OWNER)
        await session.commit()
        assert (await session.get(RoleDef, "scout")).about == roster.SCOUT_ROLE.about
    listing = (await client.get(api_url("agents"))).json()
    detail = (await client.get(api_url(f"agents/{listing[0]['connection_id']}"))).json()["may_do"]
    assert [r["title"] for r in detail["rules"]] == ["Scout", "Analyst", "Writer"]
    assert [r["kind"] for r in detail["rules"]] == ["scout", "analyst", "writer"]
    assert detail["summary"] == roster.NEVER
    said = " ".join([detail["summary"], *(r["rule"] for r in detail["rules"])])
    assert said.count("Never") == 1 and said.count("applies") == 1, said
    # An agent with one kind of work tells it as its role does; one with none says so.
    async with db.session_factory()() as session:
        roles = await roster.role_titles(session)
    one = AgentConnection(owner=OWNER, slug="solo", name="Solo", roles=["scout"])
    assert roster.may_do(one, roles) == {"summary": roster.SCOUT_ROLE.about, "rules": []}
    none = AgentConnection(owner=OWNER, slug="none", name="None", roles=[])
    assert roster.may_do(none, roles) == {"summary": "Nothing has been set for it yet.", "rules": []}


async def test_a_check_in_makes_it_idle_and_an_open_search_makes_it_work(client):
    await client.get(api_url("agents"))
    async with db.session_factory()() as session:
        await roster.touch(session, OWNER, alive=True)
        await session.commit()
    scout = (await client.get(api_url("agents"))).json()[0]
    assert scout["status"] == "idle" and scout["availability"] == "Free" and scout["last_seen_at"]
    await _start_search("banks")
    scout = (await client.get(api_url("agents"))).json()[0]
    assert scout["status"] == "working" and scout["availability"] == "On a task"
    assert scout["current"]["task_title"] == "Searching for roles: banks" and "project_id" not in scout["current"]
    detail = (await client.get(api_url(f"agents/{scout['connection_id']}"))).json()
    assert detail["may_do"]["summary"] and detail["activity"][0]["text"].startswith("You started a search")


async def test_a_session_with_no_search_run_still_shows_the_agent_busy(client):
    """The analyst, writer, parser and checker hold a session, not a search run: the roster says busy, not Free."""
    await client.get(api_url("agents"))
    async with db.session_factory()() as session:
        await roster.touch(session, OWNER, alive=True, kind=roster.CHECKER)
        await session.commit()
    scout = (await client.get(api_url("agents"))).json()[0]
    assert scout["status"] == "working" and scout["availability"] == "On a task"
    assert scout["capacity"]["free"] is False and scout["capacity"]["busy"] == 1
    assert "queue" not in scout["capacity"] and "queue_words" not in scout["capacity"]


async def test_a_runner_declaration_shows_behind_the_scout(client):
    async with db.session_factory()() as session:
        await roster.ensure_roster(session, OWNER)
        await roster.declare(session, OWNER, "scout", {
            "model_name": "sonnet", "hosting": "hosted", "runtime": "Claude Code", "concurrency": 1,
            "usage_limit": "A usage limit", "token": "never kept", "context_window_tokens": 200000,
        })
        await session.commit()
    scout = (await client.get(api_url("agents"))).json()[0]
    assert scout["model_words"] is None and scout["declared_at"], "hosted says nothing about size"
    behind = scout["behind"]
    assert behind["model"] == "sonnet · 200k-token window · hosted" and behind["runtime"] == "Claude Code"
    assert "One task at a time" in scout["behind"]["limits"]
    async with db.session_factory()() as session:
        conn = await roster.connection_by_slug(session, OWNER, "scout")
        assert "token" not in conn.declared


async def test_pausing_the_scout_stops_a_search_from_starting_and_is_said_in_activity(client):
    await _started()
    agent = (await client.get(api_url("agents"))).json()[0]
    path = f"/api/v1/agents/{agent['connection_id']}"
    res = await client.patch(path, json={"paused": True})
    assert res.status_code == 200 and res.json()["paused"] is True
    async with db.session_factory()() as session:
        await load_profile(session, OWNER)
        with pytest.raises(run.RunError, match="paused"):
            await run.start_round(session, OWNER)
    trail = (await client.get(api_url(f"agents/{agent['connection_id']}/trail"))).json()
    assert trail["items"][0]["sentence"].startswith("Scout was paused") and trail["items"][0]["actor"] == "person"
    res = await client.patch(path, json={"paused": False})
    assert res.json()["paused"] is False


# --- How they work -----------------------------------------------------------------------------------------------


async def test_how_they_work_draws_the_search_round_with_screening_under_it_and_says_what_the_scout_is_sent(client):
    body = (await client.get(api_url("harness"))).json()
    by_id = {p["id"]: p for p in body["processes"]}
    assert set(by_id) == {"jobsearch_round", "jobsearch_screen_role", "jobsearch_analyse", "jobsearch_packet",
                          "jobsearch_library", "jobsearch_library_part", "jobsearch_find_gaps",
                          "jobsearch_clarify_gaps", "jobsearch_catch_up", "jobsearch_read_boards",
                          "jobsearch_research_employer", "jobsearch_prepare_interview", "jobsearch_read_mail",
                          "jobsearch_go"}
    assert by_id["jobsearch_read_boards"]["top_level"] is True
    assert by_id["jobsearch_packet"]["top_level"] is True
    top = by_id["jobsearch_round"]
    assert top["top_level"] is True and top["children"] == ["jobsearch_screen_role"]
    assert by_id["jobsearch_screen_role"]["top_level"] is False
    steps = {s["id"]: s for s in top["steps"]}
    scout = steps["Scout_searches"]
    assert scout["who"].startswith("Scout") and scout["role"] == "scout" and scout["harness_id"] == "Scout_searches"
    assert scout["owner_sees"] and "household_sees" not in scout
    sent = scout["sent"]
    assert sent["instruction"]["name"] == "jobsearch.scout" and sent["template"]["name"] == "jobsearch.scout.report"
    assert [f["key"] for f in sent["fields"]] == ["task", "note", "roles_already_known", "profile"]
    assert all(f["means"] for f in sent["fields"])
    assert steps["Open_run"]["sent"] is None and steps["Screen_each"]["calls"] == "jobsearch_screen_role"
    assert {r["who"] for r in body["roles"]} >= {"Automatic", "You", "A tool", "Scout"}
    # a decision shows its ways out with the engine's own conditions
    decisions = [s for s in by_id["jobsearch_screen_role"]["steps"] if s["type"] == "decision"]
    assert decisions and all(s["ways"] for s in decisions)
    assert top["data_model"] and any(f["path"] == "gates.comp_floor" for f in top["data_model"])
    assert not any("fit_min" in f["path"] for f in top["data_model"]), "no gate reads a role's fit"


async def test_every_part_of_what_the_scout_is_sent_is_described(js):
    from app.agents.how_they_work import CONTEXT_MEANS, SENT_FIELDS

    for key in SENT_FIELDS["scout"]:
        assert CONTEXT_MEANS[key]
    # and the real reply holds no key the page doesn't describe
    async with db.session_factory()() as session:
        await load_profile(session, "owner")
        reply = await tools.profile_reply(session, "owner")
    assert set(reply) <= set(CONTEXT_MEANS) and set(SENT_FIELDS["scout"]) >= set(reply) - {"ok"}


async def test_handing_the_scout_its_task_keeps_a_copy_the_page_shows(client):
    run_id, _instance = await _start_search("banks")
    async with db.session_factory()() as session:
        reply = await tools.profile_reply(session, OWNER)
        again = await tools.profile_reply(session, OWNER)  # the same words again: only the time moves
        await session.commit()
    assert reply["task"]["run_id"] == str(run_id) and again == reply
    async with db.session_factory()() as session:
        rows = (await session.scalars(select(TaskHandout))).all()
    assert len(rows) == 1 and rows[0].task_id == run_id and rows[0].step == "Scout_searches"
    assert rows[0].agent_name == "Scout" and {f["kind"] for f in rows[0].files} == {"instruction", "template"}

    example = (await client.get(api_url("harness/steps/Scout_searches/example"))).json()["example"]
    assert example["source"] == "sent" and example["label"] == "as sent to Scout" and example["task_id"] == str(run_id)
    assert example["payload"]["task"]["focus"] == "banks" and example["payload"]["profile"]["hard_gates"]
    assert example["chars"] <= example["budget_chars"] and example["changed_files"] == []
    sent = (await client.get(api_url(f"harness/tasks/{run_id}"))).json()
    assert sent["process"] == "jobsearch_round" and sent["step"]["id"] == "Scout_searches"
    assert sent["example"]["payload"] == example["payload"]
    assert (await client.get(api_url("harness/steps/Open_run/example"))).status_code == 404
    assert (await client.get(api_url(f"harness/tasks/{uuid.uuid4()}"))).status_code == 404

    trail = (await client.get(api_url("trail"))).json()["items"]
    handed = [e for e in trail if e["event"] == "scout_handed_task"]
    assert len(handed) == 1 and handed[0]["sent_task_id"] == str(run_id), "said once, with the link to what was sent"


async def test_a_paused_scout_is_told_to_stop_and_is_sent_nothing(client):
    await _start_search()
    async with db.session_factory()() as session:
        conn = await roster.ensure_roster(session, OWNER)
        conn.paused = True
        await session.commit()
        reply = await tools.profile_reply(session, OWNER)
    assert reply["task"] is None and "paused" in reply["note"]
    async with db.session_factory()() as session:
        assert (await session.scalars(select(TaskHandout))).all() == []


async def test_the_live_view_lists_the_runs_and_says_where_one_is_and_what_was_sent_at_its_agent_step(client):
    run_id, instance_id = await _start_search()
    async with db.session_factory()() as session:
        await tools.profile_reply(session, OWNER)
        await session.commit()
    runs = (await client.get(api_url("harness/runs"))).json()["runs"]
    assert [r["id"] for r in runs] == [str(instance_id)] and runs[0]["process"] == "jobsearch_round"
    assert runs[0]["step_title"] == "The scout finds job boards" and runs[0]["status"] == "waiting"
    detail = (await client.get(api_url(f"harness/runs/{instance_id}"))).json()
    assert detail["current"] == "Scout_searches" and detail["ended"] is False
    assert "Open_run" in [p["step"] for p in detail["passed"]]
    assert detail["waiting_for"][0]["step"] == "Scout_searches"
    assert detail["sent"]["Scout_searches"][0]["task_id"] == str(run_id)
    # finished: the run ends, passes the screening sub-runs, and still says what was sent
    await _finish_search(run_id, [reported("Fabrikam", "Head of Delivery", fit=6, location="Oakfield, ST")])
    done = (await client.get(api_url(f"harness/runs/{instance_id}"))).json()
    assert done["ended"] is True and done["status"] == "completed" and done["current"]
    assert "Scout_searches" in [p["step"] for p in done["passed"]] and done["sent"]["Scout_searches"]
    assert done["parts"], "the screening runs of each role are its parts"
    assert (await client.get(api_url(f"harness/runs/{uuid.uuid4()}"))).status_code == 404


# --- the Inbox and the task list ----------------------------------------------------------------------------------


async def test_a_search_that_keeps_new_roles_raises_no_notification_and_no_task(client):
    # The owner decided that finding new roles never triggers a notification. The roles are on the Pipeline.
    run_id, _ = await _start_search()
    await _finish_search(run_id, [
        reported("Fabrikam", "Head of Delivery", fit=6, location="Oakfield, ST", work_mode="onsite"),
        reported("Globex", "VP Operations", fit=7),
    ])
    inbox = (await client.get("/api/v1/inbox")).json()
    assert inbox["count"] == 0 and inbox["items"] == [] and inbox["later_count"] == 0
    assert inbox["notifications"] == [] and inbox["unread_count"] == 0
    trail = (await client.get(api_url("trail"))).json()["items"]
    assert {"search_started", "scout_reported", "search_finished"} <= {e["event"] for e in trail}


async def test_a_task_marked_done_leaves_the_list_and_a_note_marked_read_is_read(client):
    async with db.session_factory()() as session:
        from app.inbox import service

        task = await service.raise_task(session, OWNER, key="look", title="Look at the site", kind="note")
        note = await service.notify(session, OWNER, key="said", kind="note", title="A site was switched off")
        await session.commit()
        task_id, note_id = str(task.id), str(note.id)
    inbox = (await client.get("/api/v1/inbox")).json()
    assert inbox["count"] == 1 and inbox["unread_count"] == 1
    assert inbox["notifications"][0]["level"] == "info" and inbox["notifications"][0]["read_at"] is None

    done = (await client.post(f"/api/v1/tasks/{task_id}/done", json={"note": "looked"})).json()
    assert done["state"] == "done" and done["done_at"]
    assert (await client.post(f"/api/v1/tasks/{task_id}/done", json={})).status_code == 409, "done stays done"
    assert (await client.get("/api/v1/inbox")).json()["count"] == 0
    tasks = (await client.get("/api/v1/tasks")).json()
    assert tasks["for_you"] == [] and tasks["done"][0]["title"] == "Look at the site"
    history = (await client.get("/api/v1/inbox/history")).json()["items"]
    assert history[0]["title"] == "Look at the site" and history[0]["decision"] == "Done"
    assert history[0]["why"] == "looked"

    assert (await client.post(f"/api/v1/notifications/{note_id}/read")).json() == {"changed": 1}
    assert (await client.get("/api/v1/inbox")).json()["unread_count"] == 0


async def test_a_search_that_found_nothing_to_keep_raises_no_notification_and_no_task(client):
    run_id, _ = await _start_search()
    await _finish_search(run_id, [reported("Globex", "VP Operations", fit=7)])
    inbox = (await client.get("/api/v1/inbox")).json()
    assert inbox["items"] == [] and inbox["notifications"] == []


async def test_a_search_the_scout_never_finished_asks_the_one_decision_and_search_again_starts_one(client):
    run_id, _ = await _start_search()
    async with db.session_factory()() as session:
        row = await session.get(SearchRun, run_id)
        await run.fail_run(session, row, "The scout didn't finish this search, so it was closed. Search again.")
        await session.commit()
    inbox = (await client.get("/api/v1/inbox")).json()
    assert inbox["count"] == 1
    card = inbox["items"][0]
    assert card["title"] == "Search again?" and card["recommended"] == "search_again"
    assert [o["id"] for o in card["options"]] == ["search_again", "not_now"]
    assert inbox["notifications"][0]["level"] == "attention" and "couldn't finish" in inbox["notifications"][0]["title"]
    bad = await client.post(f"/api/v1/tasks/{card['id']}/answer", json={"option": "nonsense"})
    assert bad.status_code == 409
    ok = await client.post(f"/api/v1/tasks/{card['id']}/answer", json={"option": "search_again"})
    assert ok.status_code == 200 and ok.json()["state"] == "done" and ok.json()["answer"] == "search_again"
    async with db.session_factory()() as session:
        runs = (await session.scalars(select(SearchRun))).all()
    assert len(runs) == 2 and [r.status for r in runs].count("open") == 1, "the new search is the one open"
    assert (await client.get("/api/v1/inbox/history")).json()["items"][0]["decision"] == "Search again"


async def test_at_most_three_things_ask_for_the_owner_at_once_and_a_snoozed_one_waits(client):
    async with db.session_factory()() as session:
        from app.inbox import service

        made = [
            await service.raise_task(session, OWNER, key=f"t{n}", title=f"Thing {n}", kind="note") for n in range(5)
        ]
        await session.commit()
        first = made[0].id
    inbox = (await client.get("/api/v1/inbox")).json()
    assert len(inbox["items"]) == 3 and inbox["later_count"] == 2 and inbox["count"] == 3
    res = await client.post(f"/api/v1/tasks/{first}/snooze", json={"until": "2999-01-01"})
    assert res.json()["snoozed_until"] == "2999-01-01"
    inbox = (await client.get("/api/v1/inbox")).json()
    assert "Thing 0" not in [t["title"] for t in inbox["items"]] and inbox["later_count"] == 1
    assert (await client.post(f"/api/v1/tasks/{uuid.uuid4()}/done", json={})).status_code == 404


async def test_a_task_or_note_with_a_key_is_made_once(js):
    from app.inbox import service

    async with db.session_factory()() as session:
        a = await service.raise_task(session, OWNER, key="k", title="One", kind="note")
        b = await service.raise_task(session, OWNER, key="k", title="Two", kind="note")
        n1 = await service.notify(session, OWNER, key="n", kind="x", title="Hello")
        n2 = await service.notify(session, OWNER, key="n", kind="x", title="Again")
        assert a.id == b.id and n1.id == n2.id and b.title == "One"


async def test_a_task_set_aside_leaves_the_list_with_the_owners_reason_and_stays_set_aside(client):
    from app.inbox import service

    async with db.session_factory()() as session:
        task = await service.raise_task(session, OWNER, key="look", title="Look at the site", kind="note")
        other = await service.raise_task(session, OWNER, key="other", title="Another thing", kind="note")
        await session.commit()
        task_id, other_id = str(task.id), str(other.id)
    skipped = (await client.post(f"/api/v1/tasks/{task_id}/skip", json={"reason": "  not this week  "})).json()
    assert skipped["state"] == "skipped" and skipped["note"] == "not this week"
    assert (await client.get("/api/v1/inbox")).json()["count"] == 1, "only the other task is left"
    assert (await client.post(f"/api/v1/tasks/{task_id}/skip", json={})).status_code == 409, "once is enough"
    assert (await client.post(f"/api/v1/tasks/{task_id}/done", json={})).status_code == 409, "and it is not then done"
    plain = (await client.post(f"/api/v1/tasks/{other_id}/skip", json={})).json()
    assert plain["state"] == "skipped" and plain["note"] is None
    assert (await client.post(f"/api/v1/tasks/{uuid.uuid4()}/skip", json={})).status_code == 404
    kept = (await client.get(api_url("trail"))).json()["items"]
    assert [e["sentence"] for e in kept if e["event"] == "task_skipped"] == [
        "You set aside: Another thing", "You set aside: Look at the site"]


async def test_read_all_marks_every_unread_note_read_once_and_counts_what_it_changed(client):
    from app.inbox import service

    async with db.session_factory()() as session:
        for n in range(3):
            await service.notify(session, OWNER, key=f"n{n}", kind="note", title=f"Note {n}")
        await service.notify(session, "someone-else", key="theirs", kind="note", title="Not the owner's")
        await session.commit()
    assert (await client.get("/api/v1/inbox")).json()["unread_count"] == 3
    assert (await client.post("/api/v1/notifications/read-all")).json() == {"changed": 3}
    inbox = (await client.get("/api/v1/inbox")).json()
    assert inbox["unread_count"] == 0 and all(n["read_at"] for n in inbox["notifications"])
    assert (await client.post("/api/v1/notifications/read-all")).json() == {"changed": 0}
    async with db.session_factory()() as session:
        theirs = await service.mark_read(session, "someone-else", None)
    assert theirs == 1, "another owner's note was left unread"


async def test_the_trail_filtered_to_highlights_is_the_newest_highlights_a_few_at_a_time(client):
    from app.trail import service as trail

    for n, tier in enumerate(["result", "routine", "needs_you", "result", "routine", "result"]):
        async with db.session_factory()() as session:  # one commit each, so each line has a time of its own
            await trail.record(session, OWNER, event=f"e{n}", sentence=f"Line {n}", tier=tier)
            await session.commit()
    recent = (await client.get(api_url("trail?filter=highlights&limit=3"))).json()
    assert [e["sentence"] for e in recent["items"]] == ["Line 5", "Line 3", "Line 2"], "newest first, no routine ones"
    assert recent["next_before"], "there is more behind them"
    one = (await client.get(api_url("trail?filter=highlights&limit=1"))).json()["items"]
    assert [e["sentence"] for e in one] == ["Line 5"]
    assert len((await client.get(api_url("trail?filter=highlights&limit=0"))).json()["items"]) == 1, "at least one"
    every = (await client.get(api_url("trail?filter=highlights&limit=99"))).json()["items"]
    assert len(every) == 4, "all four highlights"
    everything = (await client.get(api_url("trail"))).json()["items"]
    assert len(everything) == 6, "the full trail keeps the routine lines"


async def test_the_runner_states_what_is_behind_the_scout_with_its_token_and_the_page_shows_it(client, scout_token):
    said = {"model_name": "sonnet", "hosting": "local", "concurrency": 1, "token": "never kept", "host": "a box"}
    assert (await client.post("/api/v1/agent/capabilities", json=said)).status_code == 401, "no token, no say"
    wrong = {"Authorization": "Bearer not-the-token"}
    assert (await client.post("/api/v1/agent/capabilities", json=said, headers=wrong)).status_code == 401
    assert (await client.post("/api/v1/agent/capabilities", json=said, headers=scout_token)).json() == {"ok": True}
    scout = (await client.get(api_url("agents"))).json()[0]
    assert scout["model_words"] == "small local model" and scout["declared_at"]
    assert scout["behind"]["machine"] == "a box" and "One task at a time" in scout["behind"]["limits"]
    async with db.session_factory()() as session:
        conn = await roster.connection_by_slug(session, OWNER, "scout")
        assert set(conn.declared) == {"model_name", "hosting", "concurrency", "host"}
    assert (await client.post("/api/v1/agent/capabilities", json=[], headers=scout_token)).status_code == 422
