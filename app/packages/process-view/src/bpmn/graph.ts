// A process of the model as the steps and edges the process view draws.
import type { ProcessEdge, ProcessStep, StepType } from '../types'
import { isGateway, type BpmnNode, type BpmnProcess } from './model'

export const START_ID = 'start'

const STEP_TYPE: Record<string, StepType> = {
  startEvent: 'event_start',
  endEvent: 'event_end',
  boundaryEvent: 'event_boundary',
  intermediateCatchEvent: 'event_intermediate',
  intermediateThrowEvent: 'event_intermediate',
  exclusiveGateway: 'gateway_exclusive',
  inclusiveGateway: 'gateway_exclusive',
  complexGateway: 'gateway_exclusive',
  eventBasedGateway: 'gateway_exclusive',
  parallelGateway: 'gateway_parallel',
  subProcess: 'subprocess',
  transaction: 'subprocess',
  adHocSubProcess: 'subprocess',
  callActivity: 'subprocess',
}

/** The BPMN kinds the view says something about beside the step kind (a call to another process, "any that apply"). */
const NOTED = new Set(['callActivity', 'inclusiveGateway', 'eventBasedGateway', 'transaction', 'adHocSubProcess'])

const plain = (text: string) => text.replace(/`/g, '')

/** Whether `flowId` is the way out `from` (a decision) takes when no other does. */
const isDefaultOf = (from: BpmnNode | undefined, flowId: string): boolean =>
  !!from && isGateway(from) && from.defaultFlow === flowId

function performerOf(n: BpmnNode): string | null {
  const names: string[] = []
  for (const name of [n.lane, ...n.refs.roles]) {
    const key = name.trim().toLowerCase().replace(/[-_]/g, ' ')
    if (name.trim() && !names.some((x) => x.toLowerCase().replace(/[-_]/g, ' ') === key)) names.push(name.trim())
  }
  return names.length ? names.join(', ') : null
}

export function stepOf(n: BpmnNode, versionId: string, index: number): ProcessStep {
  const type = STEP_TYPE[n.type] ?? 'task'
  // A message that starts a process says what fires it in the step's own Trigger or Fired by, so the generic
  // "A message arrives" line is left out where those are written.
  const saysItself = n.type === 'startEvent' && n.trigger?.kind === 'message' && !!(n.spec.trigger || n.spec.firedBy)
  return {
    id: n.id,
    version_id: versionId,
    step_type: type,
    // A step with no name is not called by its BPMN kind ("serviceTask"): that is not a word the household uses.
    label: plain(n.name.trim() ? n.name : 'Unnamed step'),
    description: '',
    position_x: 0,
    position_y: (index + 1) * 100,
    performer: performerOf(n),
    ...(NOTED.has(n.type) ? { bpmn_type: `bpmn:${n.type}` } : {}),
    decision_criteria: n.decisionCriteria ? plain(n.decisionCriteria) : null,
    instructions: n.instructions || null,
    commands: n.commands || null,
    prompts: n.prompts || null,
    attached_to_step_id: n.attachedTo,
    interrupting: n.interrupting,
    event_trigger: saysItself ? null : (n.trigger?.kind ?? null),
    event_trigger_detail: saysItself ? null : (n.trigger?.detail ?? null),
    event_trigger_subtype: saysItself ? null : (n.trigger?.subtype ?? null),
  }
}

export function processToGraph(process: BpmnProcess): { steps: ProcessStep[]; edges: ProcessEdge[] } {
  const versionId = `bpmn-${process.id}`
  const steps: ProcessStep[] = []
  const edges: ProcessEdge[] = []
  const known = new Set(process.nodes.map((n) => n.id))
  const byId = new Map(process.nodes.map((n) => [n.id, n]))
  const hasStart = process.nodes.some((n) => n.type === 'startEvent')
  if (!hasStart && process.nodes.length > 0) {
    steps.push({
      id: START_ID, version_id: versionId, step_type: 'event_start', label: 'Start', description: '',
      position_x: 0, position_y: 0, interrupting: true,
    })
    edges.push({
      id: `${START_ID}->${process.nodes[0].id}`, version_id: versionId, source_step_id: START_ID,
      target_step_id: process.nodes[0].id, label: null, is_default: false,
    })
  }
  process.nodes.forEach((n, i) => steps.push(stepOf(n, versionId, i)))
  // Edges are listed step by step (each step's ways out in file order), which is the order the view numbers
  // the branches of a decision in.
  for (const n of process.nodes) {
    for (const f of process.flows) {
      if (f.source !== n.id) continue
      // A step that loops to itself is not drawn as a line; a line to something the file doesn't have is not either.
      if (f.source === f.target || !known.has(f.target)) continue
      edges.push({
        id: f.id, version_id: versionId, source_step_id: f.source, target_step_id: f.target, label: f.name || null,
        // The way out a decision takes when no other does: the one its `default` names.
        is_default: isDefaultOf(byId.get(f.source), f.id),
      })
    }
  }
  return { steps, edges }
}
