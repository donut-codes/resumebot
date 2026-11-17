"""The process vocabulary the BPMN reader uses (`StepType`).

Copied from another project's code (`app/process/models.py`; see app/PROVENANCE.md). Only the enums: the
process, version, step and edge tables are not here.
"""

import enum


class StepType(enum.StrEnum):
    task = "task"
    gateway_exclusive = "gateway_exclusive"
    gateway_parallel = "gateway_parallel"
    event_start = "event_start"
    event_end = "event_end"
    subprocess = "subprocess"
    # Attached to an activity rather than sitting in the sequence flow.
    event_boundary = "event_boundary"
    # Waits for or throws something part-way through the flow.
    event_intermediate = "event_intermediate"
