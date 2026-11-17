"""The steps of Launch in Browser's playbook (`playbooks/jobsearch/go.bpmn`) as the process engine's actions. Only the
registrations are here: each action loads `browser/go.py`, which opens Chrome, when it runs and not before, so
`process.load_actions` (wherever the library is built, the page program too) loads no runner."""

from process_engine.models import ProcInstance
from process_engine.registry import actions as registry
from sqlalchemy.ext.asyncio import AsyncSession

SOURCE = "backend/app/jobsearch/browser/go.py"


def _action(name: str):
    """A thin async function that runs `go.<name>` with the run's owner and the data it was given. The step opens its
    own sessions, as `go` does, so the one given here is not used."""

    async def run(session: AsyncSession, instance: ProcInstance, data: dict) -> dict:
        from app.jobsearch.browser import go

        return await getattr(go, name)(data["owner"], data)

    run.__name__ = name
    return run


def _register() -> None:
    for action, step, inputs, outputs, words in (
        ("jobs.go_begin", "step_begin", ["owner"], [],
         "Begins a run of Launch in Browser: its status starts fresh and is kept at once for the page."),
        ("jobs.go_turn", "step_turn", ["owner"], [],
         "Waits for a turn of the pipeline, so Launch sees the day's list rebalanced."),
        ("jobs.go_check", "step_check", ["owner"], [],
         "Runs Go's checks (mail for an application already sent, whether each posting is still open) and sorts the "
         "day's Ready to Launch roles into those whose packet is finished and those that need one."),
        ("jobs.go_open", "step_open", ["owner"], [],
         "Opens each role whose packet is finished as a tab in the app's own Chrome, before anything is written."),
        ("jobs.go_build", "step_build", ["owner", "poll", "wait"], ["fill"],
         "Asks for the packet of each role that has none, one after another up to the owner's session limit, opens "
         "each role as its packet is done, and answers whether a role dropped out while the writers worked."),
        ("jobs.go_refill", "step_refill", ["owner"], ["again", "round"],
         "Rebalances the day's list, checks and names the roles that moved up into Ready to Launch, and answers "
         "whether there is something more to open or build and which lap this was."),
        ("jobs.go_hand_over", "step_hand_over", ["owner"], [],
         "Hands the opened tabs to the owner, then waits for a turn of the pipeline."),
        ("jobs.go_finish", "step_finish", ["owner"], [],
         "Says so when the run opened nothing and has nothing to say why, and ends the run."),
    ):
        registry.register(action, _action(step), inputs=inputs, outputs=outputs, description=words, source=SOURCE)


_register()
