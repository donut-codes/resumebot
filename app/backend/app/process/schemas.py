"""The graph shapes the BPMN reader returns.

Trimmed from another project's process schemas (see PROVENANCE.md):
no company scoping, no software/document/control/role/person link ids, no
process relationships. `performer_names` (BPMN lane/candidateGroups names) is
still accepted on a graph save -- it is joined into the single `performer`
string column instead of being resolved against a Role/Team catalog, because
this app has none.
"""

import uuid

from pydantic import BaseModel

from app.process.models import StepType

# --- Graph batch save ---

class GraphStepInput(BaseModel):
    temp_id: str
    #: Who the file says does this step -- a lane name, a Camunda
    #: candidateGroup, an assignee. Joined into ProcessStep.performer on
    #: save; this app has no Role/Team catalog to resolve these against.
    performer_names: list[str] = []
    step_type: StepType = StepType.task
    description: str
    label: str | None = None
    position_x: float = 0
    position_y: float = 0
    owner_member_id: uuid.UUID | None = None
    subprocess_ref_id: uuid.UUID | None = None
    evidence_pointer: str | None = None
    input_description: str | None = None
    output_description: str | None = None
    expected_duration: str | None = None
    decision_criteria: str | None = None
    instructions: str | None = None
    commands: str | None = None
    prompts: str | None = None
    business_objective: str | None = None
    source_requirement: str | None = None
    validity_conditions: str | None = None
    decision_rights: str | None = None
    alternate_flows: str | None = None
    provenance: str | None = None
    change_rationale: str | None = None
    # The host may itself be new in this save, so it is addressed by temp id.
    attached_to_temp_id: str | None = None
    interrupting: bool = True
    event_trigger: str | None = None
    event_trigger_detail: str | None = None
    event_trigger_subtype: str | None = None
    bpmn_type: str | None = None
    bpmn_attrs: dict | None = None
    action: dict | None = None


class GraphEdgeInput(BaseModel):
    source_temp_id: str
    target_temp_id: str
    condition: str | None = None
    label: str | None = None
    is_default: bool = False
    bpmn_type: str | None = None
    bpmn_attrs: dict | None = None


class GraphSaveRequest(BaseModel):
    steps: list[GraphStepInput]
    edges: list[GraphEdgeInput]
    #: Serialised elements from inside <process> that we do not model -- lanes,
    #: data objects, annotations. Held so export can put them back.
    process_extras: list[str] | None = None
    #: T4 (playbooks): the compiled guide block, stored on ProcessVersion.guide.
    #: None leaves the version's guide untouched (BPMN import, canvas edits).
    guide: dict | None = None
    #: T4 (playbooks): the playbook's own semver, stored on ProcessVersion.semver.
    #: None leaves the version's semver untouched.
    semver: str | None = None
