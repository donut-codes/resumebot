"""Small processes and builders the runtime and race tests share."""

from sqlalchemy import select
from support import CATALOG, FakeClock

from process_engine.engine import Engine
from process_engine.loader import ProcessLibrary
from process_engine.models import ProcWait
from process_engine.registry import ActionRegistry

LOOP = (
    '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:porter="http://porter.local/bpmn/ext">'
    '<bpmn:process id="P" porter:key="again"><bpmn:extensionElements><porter:dataModel>'
    '<porter:field path="answer" type="object"/><porter:field path="answer.again" type="boolean"/>'
    '</porter:dataModel></bpmn:extensionElements>'
    '<bpmn:startEvent id="S"/><bpmn:userTask id="Ask" name="Ask" porter:card="pick_goal" porter:output="answer"/>'
    '<bpmn:exclusiveGateway id="G" default="F_done"/><bpmn:endEvent id="E"/>'
    '<bpmn:sequenceFlow id="a" sourceRef="S" targetRef="Ask"/><bpmn:sequenceFlow id="b" sourceRef="Ask" targetRef="G"/>'
    '<bpmn:sequenceFlow id="F_again" sourceRef="G" targetRef="Ask"><bpmn:conditionExpression '
    'xsi:type="bpmn:tFormalExpression">answer.again = true</bpmn:conditionExpression></bpmn:sequenceFlow>'
    '<bpmn:sequenceFlow id="F_done" sourceRef="G" targetRef="E"/></bpmn:process></bpmn:definitions>'
)

#: Work that has a timer: after an hour the late way out runs a step that breaks when the data says so.
TIMED = (
    '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
    'xmlns:porter="http://porter.local/bpmn/ext"><bpmn:process id="P" porter:key="timed">'
    '<bpmn:extensionElements><porter:dataModel><porter:field path="boom" type="boolean"/></porter:dataModel>'
    '</bpmn:extensionElements>'
    '<bpmn:startEvent id="S"/><bpmn:userTask id="Work" name="Work" porter:card="pick_goal"/>'
    '<bpmn:boundaryEvent id="B" attachedToRef="Work"><bpmn:timerEventDefinition><bpmn:timeDuration>PT1H'
    '</bpmn:timeDuration></bpmn:timerEventDefinition></bpmn:boundaryEvent>'
    '<bpmn:serviceTask id="Late" name="Late" porter:action="late.act"/>'
    '<bpmn:endEvent id="E_ok"/><bpmn:endEvent id="E_late"/>'
    '<bpmn:sequenceFlow id="a" sourceRef="S" targetRef="Work"/><bpmn:sequenceFlow id="b" sourceRef="Work" '
    'targetRef="E_ok"/><bpmn:sequenceFlow id="c" sourceRef="B" targetRef="Late"/>'
    '<bpmn:sequenceFlow id="d" sourceRef="Late" targetRef="E_late"/></bpmn:process></bpmn:definitions>'
)


def engine_for(xml: str, registry: ActionRegistry | None = None, clock: FakeClock | None = None) -> Engine:
    registry = registry or ActionRegistry()
    library = ProcessLibrary()
    library.load(xml, catalog=CATALOG, registry=registry)
    return Engine(library, registry, clock=clock or FakeClock())


async def open_waits(session, instance):
    return list((await session.scalars(
        select(ProcWait).where(ProcWait.instance_id == instance.id, ProcWait.status == "open")
        .order_by(ProcWait.created_at, ProcWait.id)
    )).all())


def timed_engine(clock: FakeClock) -> Engine:
    registry = ActionRegistry()

    async def late(session, instance, data):
        if data.get("boom"):
            raise KeyError("the late step broke")
        return {}

    registry.register("late.act", late, description="Runs late.", source="tests")
    return engine_for(TIMED, registry, clock)
