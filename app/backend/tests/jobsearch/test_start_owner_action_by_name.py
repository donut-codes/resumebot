"""An owner-door write job is started by name: `runner.do` asks the engine door, and the local door runs the one task
and forgets it when it ends (plan lane `engine-calls-starts-b`). Made-up action, no network, no model calls."""

import pytest

from app.jobsearch.owner_actions import REGISTRY, Action, runner
from app.jobsearch.owner_actions.registry import Args, Ctx, Outcome
from app.settings import settings
from tests.support import OWNER


class NoArgs(Args):
    pass


async def _say(ctx: Ctx, args: NoArgs) -> Outcome:
    return Outcome({"done": 1}, did="said something", would="say something")


@pytest.fixture(autouse=True)
def a_made_up_action(monkeypatch):
    monkeypatch.setitem(REGISTRY, "test.say", Action("test.say", "says", NoArgs, _say, writes=True))
    monkeypatch.setattr(settings, "owner", OWNER)
    monkeypatch.setattr(runner, "WAIT_SECONDS", 5.0)


async def test_a_job_is_started_by_asking_the_engine_door_by_name(js, fake_starts):
    answer = await runner.do(OWNER, "test.say", {}, False)
    assert answer["state"] == "running" and "not done yet" in answer["said"], "no task here: the row tells how it goes"
    assert len(fake_starts.starts) == 1 and fake_starts.starts[0][0] == "owner_action"
    owner, action, _args, dry_run, job_id, holder = fake_starts.starts[0][1]
    assert (owner, action.name, dry_run, holder) == (OWNER, "test.say", False, None)
    assert str(job_id) == answer["job"]
    assert not runner._tasks and not runner._by_job


async def test_the_local_door_runs_the_job_and_forgets_its_task(js):
    answer = await runner.do(OWNER, "test.say", {}, False)
    assert answer["state"] == "done" and answer["ok"] is True
    await runner.settle()
    assert not runner._tasks and not runner._by_job
