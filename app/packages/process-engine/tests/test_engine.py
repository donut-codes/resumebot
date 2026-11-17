"""The engine runs the sample processes end to end: run-to-wait, waits, decisions, sub-processes, multi-instance,
timers, errors and history (path 15.1). Nothing here touches a host app's own actions or a household."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from support import CATALOG, T0, FakeClock, fixture_xml, sample_engine, sample_library, sample_registry

from process_engine import feel
from process_engine.engine import Engine
from process_engine.errors import ActionError, EngineError, RunOver
from process_engine.loader import ProcessLibrary
from process_engine.models import (
    CANCELLED,
    COMPLETED,
    FAILED,
    WAITING,
    ProcInstance,
    ProcToken,
    ProcWait,
)
from process_engine.registry import ActionRegistry, ActionSpec

RISKY = [{"name": "rename a file", "risk": "low"}, {"name": "restart a service", "risk": "high"}]
DELETING = [{"name": "delete the old backup", "risk": "low"}]
DEFAULT_ITEMS_AS_STORED = [{"name": "rename a file", "risk": "low"}, {"name": "restart a service", "risk": "medium"}]


async def children(session, instance):
    return list((await session.scalars(
        select(ProcInstance).where(ProcInstance.parent_id == instance.id).order_by(ProcInstance.item_index)
    )).all())


async def waits(session, instance, status="open"):
    return list((await session.scalars(
        select(ProcWait).where(ProcWait.instance_id == instance.id, ProcWait.status == status)
        .order_by(ProcWait.created_at, ProcWait.element_id)
    )).all())


def moves(history):
    return [(h.kind, h.from_element, h.to_element) for h in history]


# --- the plain path ---

async def test_a_process_runs_to_its_first_wait_and_says_where_it_is(session):
    calls: list = []
    engine = sample_engine(calls)
    instance = await engine.start(session, "intake", {"note": "hello"}, subject="goal:1", message="intake.requested")

    assert instance.status == WAITING and instance.mode == "live" and instance.subject == "goal:1"
    assert [name for name, _ in calls].count("plan.load") == 1
    assert [name for name, _ in calls].count("item.look") == 3  # two items, then the headline
    where = await engine.where_is(session, instance.id)
    assert (where["step"], where["token"], where["status"]) == ("Summary", "waiting", "waiting")
    agent_wait, timer_wait = where["waiting_for"]
    assert agent_wait["kind"] == "agent"
    assert agent_wait["payload"] == {
        "name": "Write the summary", "element": "Summary", "lane": "Chief of Staff", "role": "planner",
        "instructions": "summary.write", "template": "summary.response"}
    assert (timer_wait["kind"], timer_wait["step"]) == ("boundary_timer", "B_slow")
    assert timer_wait["due_at"] == (T0 + timedelta(hours=2)).isoformat()

    # what the steps produced is in the instance's data, for the conditions after them
    assert instance.data["note"] == "hello"
    assert instance.data["plan"]["total"] == 2
    assert instance.data["verdicts"] == ["ok", "ok"]
    assert instance.data["headline_verdict"] == "ok"
    assert instance.data["result"] == {"headline_verdict": "ok"}


async def test_the_whole_process_to_its_end(session):
    calls: list = []
    clock = FakeClock()
    engine = sample_engine(calls, clock)
    instance = await engine.start(session, "intake", {"note": "hi"}, message="intake.requested")

    await engine.complete(session, instance.id, "Summary", {"text": "All three looked fine."})
    assert instance.data["summary"] == {"text": "All three looked fine."}
    # finishing the step voided its timer
    assert [w.kind for w in await waits(session, instance)] == ["catch_message"]
    assert [w.kind for w in await waits(session, instance, "cancelled")] == ["boundary_timer"]

    await engine.deliver_message(session, instance.id, "intake.confirmed", {"by": "owner"})
    where = await engine.where_is(session, instance.id)
    assert where["step"] == "Settle" and where["waiting_for"][0]["kind"] == "catch_timer"

    assert await engine.tick(session) == 0  # not due yet
    clock.advance(minutes=59)
    assert await engine.tick(session) == 0
    clock.advance(minutes=2)
    assert await engine.tick(session) == 1

    assert instance.status == COMPLETED and instance.ended_at is not None
    assert [name for name, _ in calls][-1] == "intake.notify"
    where = await engine.where_is(session, instance.id)
    assert where["token"] == "done"

    trail = moves(await engine.history(session, instance.id))
    assert trail[0] == ("start", None, "Start_msg")
    assert trail[-1] == ("end", "End_done", None)
    assert ("move", "Notify", "End_done") in trail


async def test_history_is_a_numbered_record_of_every_move(session):
    clock = FakeClock()
    engine = sample_engine(clock=clock)
    instance = await engine.start(session, "intake", message="intake.requested")
    history = await engine.history(session, instance.id)
    assert [h.seq for h in history] == list(range(1, len(history) + 1))
    assert {h.at for h in history} == {T0}
    kinds = [h.kind for h in history]
    assert kinds[0] == "start" and "decision" in kinds and kinds[-1] == "wait"
    decision = next(h for h in history if h.kind == "decision")
    assert (decision.from_element, decision.to_element, decision.flow_id) == ("Gw_need", "Look_all", "F_go")
    assert decision.condition == "default"
    # the data the decision read: the first condition read the plan's items, summarised by value
    assert decision.reads["plan.items"] == DEFAULT_ITEMS_AS_STORED


# --- decisions ---

async def test_a_condition_picks_the_way_out_and_the_history_says_which(session):
    engine = sample_engine(items=DELETING)
    instance = await engine.start(session, "intake", message="intake.requested")
    where = await engine.where_is(session, instance.id)
    assert where["step"] == "Approve"
    assert where["waiting_for"][0]["kind"] == "user"
    assert where["waiting_for"][0]["payload"]["card"] == "plan.approval"
    decision = next(h for h in await engine.history(session, instance.id) if h.kind == "decision")
    assert decision.flow_id == "F_ask"
    assert decision.condition.startswith("some i in plan.items satisfies")


async def test_an_empty_plan_takes_the_nothing_to_do_way_out(session):
    engine = sample_engine(items=[])
    instance = await engine.start(session, "intake", message="intake.requested")
    assert instance.status == COMPLETED
    decision = next(h for h in await engine.history(session, instance.id) if h.kind == "decision")
    assert decision.flow_id == "F_empty" and decision.condition == "count(plan.items) = 0"
    assert decision.reads == {"plan.items": []}


async def test_the_household_says_no(session):
    engine = sample_engine(items=DELETING)
    instance = await engine.start(session, "intake", message="intake.requested")
    await engine.complete(session, instance.id, "Approve", {"approved": False})
    assert instance.status == COMPLETED
    trail = moves(await engine.history(session, instance.id))
    assert ("decision", "Gw_approved", "End_declined") in trail


async def test_the_household_says_yes_and_a_risky_item_gets_a_second_look(session):
    calls: list = []
    engine = sample_engine(calls, items=RISKY)
    instance = await engine.start(session, "intake", message="intake.requested")
    await engine.complete(session, instance.id, "Approve", {"approved": True})

    # the call activity started one child per item; the risky one is waiting for its second look
    kids = await children(session, instance)
    assert [k.item_index for k in kids] == [0, 1]
    assert [k.status for k in kids] == [COMPLETED, WAITING]
    assert kids[0].data["item"] == RISKY[0] and kids[0].data["look"] == {"verdict": "ok"}
    assert (await engine.where_is(session, instance.id))["step"] == "Look_all"
    assert (await engine.where_is(session, kids[1].id))["waiting_for"][0]["payload"]["role"] == "doer"
    assert kids[1].subject == instance.subject and kids[1].parent_element == "Look_all"

    await engine.complete(session, kids[1].id, "Second_look", {"agrees": True})
    assert kids[1].status == COMPLETED
    # the join: verdicts in item order, then the next step
    assert instance.data["verdicts"] == ["ok", "bad"]
    assert (await engine.where_is(session, instance.id))["step"] == "Summary"
    assert instance.data["headline_verdict"] == "ok"


# --- sub-processes ---

async def test_a_called_process_gets_only_what_the_call_hands_over_and_brings_back_only_what_it_names(session):
    engine = sample_engine()
    instance = await engine.start(session, "intake", message="intake.requested")
    head = [k for k in await session.scalars(
        select(ProcInstance).where(ProcInstance.parent_id == instance.id, ProcInstance.item_index.is_(None)))][0]
    assert set(head.data) == {"item", "look", "result"}
    assert head.data["item"] == {"name": "rename a file", "risk": "low"}
    assert instance.data["headline_verdict"] == "ok"
    assert "look" not in instance.data  # the child's own variable stays the child's


async def test_sequential_multi_instance_starts_one_child_at_a_time(session):
    calls: list = []
    registry = sample_registry(calls, RISKY)
    library = ProcessLibrary()
    library.load(fixture_xml("check_one.bpmn"), catalog=CATALOG, registry=registry)
    library.load(fixture_xml("intake.bpmn").replace('isSequential="false"', 'isSequential="true"'),
                 catalog=CATALOG, registry=registry)
    library.validate()
    engine = Engine(library, registry, clock=FakeClock())
    instance = await engine.start(session, "intake", message="intake.requested")
    await engine.complete(session, instance.id, "Approve", {"approved": True})

    # the first (low risk) child finished; the second is a risky one waiting for a second look
    kids = await children(session, instance)
    assert [k.item_index for k in kids] == [0, 1]
    assert [name for name, _ in calls].count("item.look") == 2
    await engine.complete(session, kids[1].id, "Second_look", {"agrees": False})
    assert instance.data["verdicts"] == ["ok", "bad"]


async def test_multi_instance_over_nothing_is_done_at_once_with_empty_results(session):
    registry = sample_registry(None, [])
    library = ProcessLibrary()
    library.load(fixture_xml("check_one.bpmn"), catalog=CATALOG, registry=registry)
    # an empty plan would end at "Nothing to do"; change that rule so it goes on to the per-item call instead
    xml = fixture_xml("intake.bpmn").replace("count(plan.items) = 0", "count(plan.items) = 99")
    library.load(xml, catalog=CATALOG, registry=registry)
    library.validate()
    engine = Engine(library, registry, clock=FakeClock())
    instance = await engine.start(session, "intake", message="intake.requested")
    assert instance.data["verdicts"] == []
    assert (await engine.where_is(session, instance.id))["step"] == "Summary"
    assert len(await children(session, instance)) == 1  # only the call for the headline item


async def test_a_failing_child_fails_its_parent_and_stops_its_siblings(session):
    registry = sample_registry(None, RISKY)

    async def look_fails(session, instance, data):
        raise ActionError("look_broke", "the look could not run")

    registry._actions["item.look"] = ActionSpec("item.look", look_fails, ("item",), ("verdict",), "Fails.", "tests")
    engine = Engine(sample_library(registry), registry, clock=FakeClock())
    instance = await engine.start(session, "intake", message="intake.requested")
    # the household is asked first because one item is risky; the children start after the yes
    await engine.complete(session, instance.id, "Approve", {"approved": True})
    assert instance.status == FAILED
    assert "look_broke" in instance.error and "check.one" in instance.error
    kids = await children(session, instance)
    assert FAILED in {k.status for k in kids}
    assert all(k.status in (FAILED, CANCELLED, COMPLETED) for k in kids)


# --- timers, messages, errors ---

async def test_a_timer_boundary_cancels_its_step_and_takes_its_way_out(session):
    clock = FakeClock()
    engine = sample_engine(clock=clock)
    instance = await engine.start(session, "intake", message="intake.requested")
    clock.advance(hours=1)
    assert await engine.tick(session) == 0
    clock.advance(hours=1, minutes=1)
    assert await engine.tick(session) == 1
    assert instance.status == COMPLETED
    trail = moves(await engine.history(session, instance.id))
    assert ("fire", "Summary", "End_slow") in trail
    assert [w.status for w in await waits(session, instance, "cancelled")] == ["cancelled"]
    # the agent's task is no longer open
    with pytest.raises(EngineError, match="already completed"):
        await engine.complete(session, instance.id, "Summary", {})


async def test_a_named_error_takes_the_matching_error_boundary(session):
    engine = sample_engine()
    instance = await engine.start(session, "intake", {"note": "boom"}, message="intake.requested")
    await engine.complete(session, instance.id, "Summary", {"text": "x"})
    await engine.deliver_message(session, instance.id, "intake.confirmed")
    await engine.fire_timer(session, instance.id, "Settle")
    assert instance.status == COMPLETED
    assert instance.data["error"] == {"code": "notify_failed", "message": "the household could not be told"}
    trail = moves(await engine.history(session, instance.id))
    assert ("error", "Notify", "End_notify_failed") in trail
    assert trail[-1] == ("end", "End_notify_failed", None)


async def test_an_error_nobody_catches_fails_the_instance_with_its_name(session):
    registry = sample_registry()

    async def always_fails(session, instance, data):
        raise ActionError("disk_full", "there is no room")

    registry.register("always.fails", always_fails, description="Fails.", source="tests")
    xml = ('<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
           'xmlns:porter="http://porter.local/bpmn/ext"><bpmn:process id="P" porter:key="boom">'
           '<bpmn:startEvent id="S"/><bpmn:serviceTask id="T" name="Write it" porter:action="always.fails"/>'
           '<bpmn:endEvent id="E"/><bpmn:sequenceFlow id="a" sourceRef="S" targetRef="T"/>'
           '<bpmn:sequenceFlow id="b" sourceRef="T" targetRef="E"/></bpmn:process></bpmn:definitions>')
    library = ProcessLibrary()
    library.load(xml, catalog=CATALOG, registry=registry)
    engine = Engine(library, registry, clock=FakeClock())
    instance = await engine.start(session, "boom")
    assert instance.status == FAILED
    assert instance.error == "'Write it' raised disk_full: there is no room"
    where = await engine.where_is(session, instance.id)
    assert where["status"] == "failed" and where["step"] == "T"


async def test_an_action_that_says_the_run_is_over_ends_it_cancelled_and_nothing_after_the_step_runs(session):
    registry = sample_registry()
    ran: list = []

    async def settled(session, instance, data):
        raise RunOver("what this run was for was settled before it got here")

    async def after(session, instance, data):
        ran.append("after")
        return {}

    registry.register("already.settled", settled, description="Says the run is over.", source="tests")
    registry.register("after.it", after, description="Runs after it.", source="tests")
    xml = ('<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
           'xmlns:porter="http://porter.local/bpmn/ext"><bpmn:process id="P" porter:key="settled">'
           '<bpmn:startEvent id="S"/><bpmn:serviceTask id="T" name="Settle it" porter:action="already.settled"/>'
           '<bpmn:serviceTask id="U" name="After it" porter:action="after.it"/>'
           '<bpmn:endEvent id="E"/><bpmn:sequenceFlow id="a" sourceRef="S" targetRef="T"/>'
           '<bpmn:sequenceFlow id="b" sourceRef="T" targetRef="U"/>'
           '<bpmn:sequenceFlow id="c" sourceRef="U" targetRef="E"/></bpmn:process></bpmn:definitions>')
    library = ProcessLibrary()
    library.load(xml, catalog=CATALOG, registry=registry)
    engine = Engine(library, registry, clock=FakeClock())
    instance = await engine.start(session, "settled")
    assert instance.status == CANCELLED and instance.error == "what this run was for was settled before it got here"
    assert ran == [], "the steps after it never run"
    assert [t.state for t in (await session.scalars(
        select(ProcToken).where(ProcToken.instance_id == instance.id))).all()] == ["done"]
    assert moves(await engine.history(session, instance.id))[-1] == (
        "cancel", None, None), "the history says it was cancelled, at the step"


async def test_a_bug_in_an_action_is_not_swallowed(session):
    registry = sample_registry()

    async def bug(session, instance, data):
        raise KeyError("oops")

    registry.register("has.bug", bug, description="Has a bug.", source="tests")
    xml = ('<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
           'xmlns:porter="http://porter.local/bpmn/ext"><bpmn:process id="P" porter:key="bug">'
           '<bpmn:startEvent id="S"/><bpmn:serviceTask id="T" porter:action="has.bug"/><bpmn:endEvent id="E"/>'
           '<bpmn:sequenceFlow id="a" sourceRef="S" targetRef="T"/><bpmn:sequenceFlow id="b" sourceRef="T" '
           'targetRef="E"/></bpmn:process></bpmn:definitions>')
    library = ProcessLibrary()
    library.load(xml, catalog=CATALOG, registry=registry)
    with pytest.raises(KeyError):
        await Engine(library, registry).start(session, "bug")


async def test_a_message_starts_the_process_waiting_for_it_and_a_timer_start_is_chosen_by_name(session):
    engine = sample_engine()
    started = await engine.start_by_message(session, "intake.requested", {"note": "n"}, subject="x")
    assert [i.process_key for i in started] == ["intake"] and started[0].subject == "x"
    assert await engine.start_by_message(session, "nothing.listens") == []
    by_timer = await engine.start(session, "intake", start_event="Start_timer")
    trail = moves(await engine.history(session, by_timer.id))
    assert trail[0] == ("start", None, "Start_timer")
    with pytest.raises(EngineError, match="no start event"):
        await engine.start(session, "intake", message="other.message")


async def test_the_engine_refuses_what_it_cannot_do(session):
    engine = sample_engine()
    with pytest.raises(EngineError, match="no process 'ghost'"):
        await engine.start(session, "ghost")
    with pytest.raises(EngineError, match="'secret' is not in intake's data model"):
        await engine.start(session, "intake", {"secret": 1}, message="intake.requested")
    instance = await engine.start(session, "intake", message="intake.requested")
    with pytest.raises(EngineError, match="nothing is waiting to be completed at 'Load'"):
        await engine.complete(session, instance.id, "Load", {})
    with pytest.raises(EngineError, match="isn't waiting for the message 'wrong'"):
        await engine.deliver_message(session, instance.id, "wrong")
    with pytest.raises(EngineError, match="no timer is waiting at 'Summary'"):
        await engine.fire_timer(session, instance.id, "Summary")
    with pytest.raises(EngineError, match="only a deferred service step"):
        await engine.complete(session, instance.id, "Summary", {}, error="x")
    # the refused calls changed nothing
    assert (await engine.where_is(session, instance.id))["step"] == "Summary"


async def test_a_process_that_loops_without_waiting_is_stopped(session):
    registry = ActionRegistry()

    async def noop(session, instance, data):
        return {}

    registry.register("noop", noop, description="Does nothing.", source="tests")
    xml = ('<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
           'xmlns:porter="http://porter.local/bpmn/ext"><bpmn:process id="P" porter:key="spin">'
           '<bpmn:extensionElements><porter:dataModel><porter:field path="flag" type="boolean"/>'
           '</porter:dataModel></bpmn:extensionElements>'
           '<bpmn:startEvent id="S"/><bpmn:serviceTask id="T" porter:action="noop"/>'
           '<bpmn:exclusiveGateway id="G" default="back"/><bpmn:endEvent id="E"/>'
           '<bpmn:sequenceFlow id="a" sourceRef="S" targetRef="T"/><bpmn:sequenceFlow id="b" sourceRef="T" '
           'targetRef="G"/><bpmn:sequenceFlow id="done" sourceRef="G" targetRef="E"><bpmn:conditionExpression>flag'
           '</bpmn:conditionExpression></bpmn:sequenceFlow>'
           '<bpmn:sequenceFlow id="back" sourceRef="G" targetRef="T"/></bpmn:process></bpmn:definitions>')
    library = ProcessLibrary()
    library.load(xml, catalog=CATALOG, registry=registry)
    instance = await Engine(library, registry, clock=FakeClock(), max_moves=30).start(session, "spin")
    assert instance.status == FAILED and "the process loops" in instance.error


async def test_a_decision_with_no_way_out_fails_plainly_and_a_non_yes_no_condition_fails_plainly(session):
    registry = ActionRegistry()
    xml = ('<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
           'xmlns:porter="http://porter.local/bpmn/ext"><bpmn:process id="P" porter:key="{key}">'
           '<bpmn:extensionElements><porter:dataModel><porter:field path="n" type="number"/>'
           '<porter:field path="t" type="any"/></porter:dataModel></bpmn:extensionElements>'
           '<bpmn:startEvent id="S"/><bpmn:exclusiveGateway id="G" name="Which?" default="hi"/>'
           '<bpmn:endEvent id="E"/><bpmn:endEvent id="E2"/>'
           '<bpmn:sequenceFlow id="a" sourceRef="S" targetRef="G"/>'
           '<bpmn:sequenceFlow id="lo" sourceRef="G" targetRef="E"><bpmn:conditionExpression>{one}'
           '</bpmn:conditionExpression></bpmn:sequenceFlow>'
           '<bpmn:sequenceFlow id="hi" sourceRef="G" targetRef="E2"/></bpmn:process></bpmn:definitions>')
    library = ProcessLibrary()
    library.load(xml.format(key="gap", one="n &lt; 5"), catalog=CATALOG, registry=registry)
    library.load(xml.format(key="odd", one="t"), catalog=CATALOG, registry=registry)
    # The loader refuses a decision whose conditions leave a case out and has no default, so this one is made by
    # hand: `hi` stops being the default and takes the condition `n >= 5`, which leaves out data with no `n`.
    hi = library.get("gap").flows["hi"]
    hi.is_default, hi.condition, hi.tree = False, "n >= 5", feel.parse("n >= 5")
    engine = Engine(library, registry, clock=FakeClock())
    gap = await engine.start(session, "gap")  # n is missing: both conditions are null, so neither holds
    assert gap.status == FAILED and "no way out for this data and no default" in gap.error
    odd = await engine.start(session, "odd", {"t": 7})
    assert odd.status == FAILED and "answered 7, not true or false" in odd.error
    fine = await engine.start(session, "gap", {"n": 9})
    assert fine.status == COMPLETED


# --- stopping, versions ---

async def test_cancelling_stops_the_instance_its_children_and_its_waits(session):
    engine = sample_engine(items=RISKY)
    instance = await engine.start(session, "intake", message="intake.requested")
    await engine.complete(session, instance.id, "Approve", {"approved": True})
    kids = await children(session, instance)
    assert WAITING in {k.status for k in kids}
    await engine.cancel(session, instance.id, "the household changed its mind")
    assert instance.status == CANCELLED and instance.error == "the household changed its mind"
    assert {k.status for k in await children(session, instance)} <= {CANCELLED, COMPLETED}
    assert await waits(session, instance) == []
    token = (await session.scalars(select(ProcToken).where(ProcToken.instance_id == instance.id))).one()
    assert token.state == "done"
    assert (await engine.cancel(session, instance.id)).status == CANCELLED  # cancelling twice is harmless


async def test_a_running_instance_finishes_under_the_version_it_started_with(session):
    registry = sample_registry()
    library = sample_library(registry)
    engine = Engine(library, registry, clock=FakeClock())
    instance = await engine.start(session, "intake", message="intake.requested")
    started_with = instance.file_hash
    changed = fixture_xml("intake.bpmn").replace("Write the summary", "Write the short summary")
    library.load(changed, catalog=CATALOG, registry=registry)
    assert library.get("intake").file_hash != started_with
    await engine.complete(session, instance.id, "Summary", {"text": "x"})
    assert instance.file_hash == started_with
    new = await engine.start(session, "intake", message="intake.requested")
    assert new.file_hash == library.get("intake").file_hash != started_with
    where = await engine.where_is(session, new.id)
    assert where["waiting_for"][0]["payload"]["name"] == "Write the short summary"


async def test_the_hand_out_hook_sees_each_wait_that_needs_a_person_or_an_agent(session):
    seen = []

    async def on_wait(session, instance, wait):
        who = wait.payload.get("role") or wait.payload.get("card")
        seen.append((instance.process_key, wait.element_id, wait.kind, who))

    engine = sample_engine(items=RISKY, on_wait=on_wait)
    instance = await engine.start(session, "intake", message="intake.requested")
    await engine.complete(session, instance.id, "Approve", {"approved": True})
    kids = await children(session, instance)
    await engine.complete(session, kids[1].id, "Second_look", {})
    assert seen == [
        ("intake", "Approve", "user", "plan.approval"),
        ("check.one", "Second_look", "agent", "doer"),
        ("intake", "Summary", "agent", "planner"),
    ]
