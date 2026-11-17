"""Shared set-up for the package's tests: two sample process files, a registry with the sample actions, a catalog
naming the sample roles' files and cards, and a fake clock. Nothing here touches the default registry or the app's
config."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from process_engine.engine import Engine
from process_engine.errors import ActionError
from process_engine.loader import ProcessLibrary
from process_engine.registry import ActionRegistry, Catalog

FIXTURES = Path(__file__).parent / "fixtures"

CATALOG = Catalog(
    roles=frozenset({"planner", "doer", "teacher", "finder", "chief_of_staff", "specialist"}),
    cards=frozenset({"plan.approval", "pick_goal"}),
    instructions=frozenset({"summary.write", "second.look"}),
    templates=frozenset({"summary.response", "second.look.response"}),
)

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> datetime:
        self.now = self.now + timedelta(**kwargs)
        return self.now


def fixture_xml(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


DEFAULT_ITEMS = [{"name": "rename a file", "risk": "low"}, {"name": "restart a service", "risk": "medium"}]


def sample_registry(calls: list | None = None, items: list | None = None) -> ActionRegistry:
    """The actions the samples name. `calls` collects (action, data) so tests can see what ran; `items` is the
    plan `plan.load` returns."""
    registry = ActionRegistry()
    log = calls if calls is not None else []
    plan_items = DEFAULT_ITEMS if items is None else items

    async def plan_load(session, instance, data):
        log.append(("plan.load", data))
        return {"items": plan_items, "total": len(plan_items), "headline": plan_items[0] if plan_items else None}

    async def item_look(session, instance, data):
        log.append(("item.look", data))
        return {"verdict": "bad" if (data.get("item") or {}).get("risk") == "high" else "ok"}

    async def notify(session, instance, data):
        log.append(("intake.notify", data))
        if data.get("note") == "boom":
            raise ActionError("notify_failed", "the household could not be told")
        return {"sent": True}

    registry.register("plan.load", plan_load, inputs=["note"], outputs=["items", "total", "headline"],
                      description="Loads the plan the process works on.", source="tests/support")
    registry.register("item.look", item_look, inputs=["item"], outputs=["verdict"],
                      description="Looks at one item and gives a verdict.", source="tests/support")
    registry.register("intake.notify", notify, inputs=["summary"], outputs=["sent"],
                      description="Tells the household the summary.", source="tests/support")
    return registry


def sample_library(registry: ActionRegistry | None = None) -> ProcessLibrary:
    library = ProcessLibrary()
    registry = registry or sample_registry()
    for name in ("check_one.bpmn", "intake.bpmn"):
        library.load(fixture_xml(name), catalog=CATALOG, registry=registry)
    library.validate()
    return library


def sample_engine(calls: list | None = None, clock: FakeClock | None = None, items: list | None = None,
                  **kwargs) -> Engine:
    registry = sample_registry(calls, items)
    return Engine(sample_library(registry), registry, clock=clock or FakeClock(), **kwargs)
