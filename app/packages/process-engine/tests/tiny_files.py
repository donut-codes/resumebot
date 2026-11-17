"""Tiny process files for the tests of versions: one household step, and (when asked) a call."""


def process(key: str, words: str = "first", card: str = "ask.owner", calls: str | None = None) -> str:
    """A tiny process file: one household step, and (when `calls` names a process) a call to it."""
    call = (
        f'<bpmn:callActivity id="Call" name="Call" calledElement="{calls}"/>'
        '<bpmn:sequenceFlow id="F3" sourceRef="Call" targetRef="End"/>'
    ) if calls else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
    xmlns:porter="http://porter.local/bpmn/ext" targetNamespace="http://example.test/bpmn">
  <bpmn:process id="{key}" name="{key}" porter:key="{key}">
    <bpmn:startEvent id="Start"/>
    <bpmn:userTask id="Ask" name="Ask" porter:card="{card}"><bpmn:documentation>{words}</bpmn:documentation>
    </bpmn:userTask>
    {call}
    <bpmn:endEvent id="End"/>
    <bpmn:sequenceFlow id="F1" sourceRef="Start" targetRef="Ask"/>
    <bpmn:sequenceFlow id="F2" sourceRef="Ask" targetRef="{'Call' if calls else 'End'}"/>
  </bpmn:process>
</bpmn:definitions>
"""
