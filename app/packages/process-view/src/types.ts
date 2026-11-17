// Minimal process-engine shapes for the frontend canvas kit (T3/T8).
//
// Deliberately smaller than the upstream project's frontend/src/api/types.ts: no
// team/software/document/control/role/person link fields, since the app's
// backend schemas (backend/app/process/schemas.py) don't have them either.
// See PROVENANCE.md.

export type StepType =
  | 'task'
  | 'gateway_exclusive'
  | 'gateway_parallel'
  | 'event_start'
  | 'event_end'
  | 'subprocess'
  | 'event_boundary'
  | 'event_intermediate'

export interface ProcessStep {
  id: string
  version_id: string
  step_type: StepType
  label?: string | null
  description: string
  position_x: number
  position_y: number
  performer?: string | null
  owner_member_id?: string | null
  subprocess_ref_id?: string | null
  evidence_pointer?: string | null
  input_description?: string | null
  output_description?: string | null
  expected_duration?: string | null
  decision_criteria?: string | null
  instructions?: string | null
  commands?: string | null
  prompts?: string | null
  business_objective?: string | null
  source_requirement?: string | null
  validity_conditions?: string | null
  decision_rights?: string | null
  alternate_flows?: string | null
  provenance?: string | null
  change_rationale?: string | null
  attached_to_step_id?: string | null
  interrupting: boolean
  event_trigger?: string | null
  event_trigger_detail?: string | null
  event_trigger_subtype?: string | null
  bpmn_type?: string | null
  bpmn_attrs?: Record<string, unknown> | null
  // the goal packages spec / the playbooks spec
  // (backend/app/playbooks/compile.py `_step_action`): `kind` is the
  // playbook step's `do` ("ask", "finish", "call", ...), `why` is the
  // step's own one-line rationale, `role`/`automatic` are what decides
  // whether the app or a person does it. A `call` step's `subprocess` node
  // also carries `workflow` (the called playbook's id) and `children` (its
  // inlined steps' namespaced ids).
  action?: {
    kind?: string
    params?: Record<string, unknown> | null
    role?: string | null
    automatic?: boolean | null
    why?: string | null
    workflow?: string | null
    children?: string[]
  } | null
}

export interface ProcessEdge {
  id: string
  version_id: string
  source_step_id: string
  target_step_id: string
  condition?: string | null
  label?: string | null
  is_default: boolean
  bpmn_type?: string | null
  bpmn_attrs?: Record<string, unknown> | null
}

export interface ProcessVersionGraph {
  id: string
  process_id: string
  version_number: number
  semver?: string | null
  status: string
  steps: ProcessStep[]
  edges: ProcessEdge[]
}
