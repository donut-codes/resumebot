// The model a BPMN file is read into, and edited as. It holds the fields a person edits as text; everything else
// stays in the XML tree the model points at (`el`), untouched, so unknown elements and attributes are never lost.
import { canonical } from './canon'
import type { XDoc, XEl } from './xml'

export const BPMN_NS = 'http://www.omg.org/spec/BPMN/20100524/MODEL'
export const BPMNDI_NS = 'http://www.omg.org/spec/BPMN/20100524/DI'
export const DC_NS = 'http://www.omg.org/spec/DD/20100524/DC'
export const DI_NS = 'http://www.omg.org/spec/DD/20100524/DI'
export const XSI_NS = 'http://www.w3.org/2001/XMLSchema-instance'
/** The app's own attributes (`porter:role`, `porter:action` ...). */
export const PORTER_NS = 'http://porter.local/bpmn/ext'
/** the upstream project's extension elements: who owns a step, which software and documents it uses, which controls. */
export const PA_NS = 'http://processatlas.local/bpmn/extensions'

/** The parts of a step the specification keeps apart, each one text. */
export interface SpecFields {
  actor: string
  trigger: string
  preconditions: string
  inputs: string
  /** The numbered rules, one per line (`1. ...`), as written. */
  rules: string
  exceptions: string
  /** For a start event: what fires it, and where it enters. */
  firedBy: string
  entersAt: string
  outputs: string
  householdSees: string
  source: string
}

export const SPEC_KEYS: (keyof SpecFields)[] = [
  'actor', 'trigger', 'preconditions', 'inputs', 'rules', 'exceptions', 'firedBy', 'entersAt', 'outputs',
  'householdSees', 'source',
]

export interface ControlRef {
  reference: string
  framework: string
}

/** the upstream project's references, by name (a file travels between installs and holds no ids). */
export interface Refs {
  owner: string
  roles: string[]
  people: string[]
  software: string[]
  documents: string[]
  controls: ControlRef[]
}

export interface EventTrigger {
  kind: string
  detail: string | null
  subtype: string | null
}

export interface MultiInstance {
  sequential: boolean
  collection: string
  elementVariable: string
}

export interface BpmnNode {
  id: string
  /** The element's local name: `task`, `serviceTask`, `exclusiveGateway`, `startEvent`, `callActivity` ... */
  type: string
  name: string
  /** The lane the step sits in: who does it. '' when the process has no lanes. */
  lane: string
  /** `porter:` attributes, key without the prefix, in file order. */
  porter: Record<string, string>
  spec: SpecFields
  /** the upstream project's tagged documentation: what to do, how to choose, what to run, what to ask an agent. */
  instructions: string
  decisionCriteria: string
  commands: string
  prompts: string
  refs: Refs
  /** On a boundary event: the step it is attached to, and whether it interrupts it. */
  attachedTo: string | null
  interrupting: boolean
  /** On a call activity: the process it calls. */
  calledElement: string | null
  /** On a gateway: its default way out. */
  defaultFlow: string | null
  trigger: EventTrigger | null
  multiInstance: MultiInstance | null
  /** Where this came from in the file; absent on a step added in the editor. */
  el?: XEl
  /** What the editable fields were when the file was read (JSON), to write back only what changed. */
  base?: string
}

export interface BpmnFlow {
  id: string
  /** The way out's label. */
  name: string
  source: string
  target: string
  /** The FEEL condition. */
  condition: string
  /** The plain meaning (the flow's documentation). */
  meaning: string
  porter: Record<string, string>
  el?: XEl
  base?: string
}

export interface Lane {
  id: string
  name: string
}

export interface BpmnProcess {
  id: string
  name: string
  /** `porter:key`, or the id. */
  key: string
  documentation: string
  lanes: Lane[]
  nodes: BpmnNode[]
  flows: BpmnFlow[]
  el: XEl
  /** The elements of the file's own nodes and flows, to find what was removed. */
  origNodes: BpmnNode[]
  origFlows: BpmnFlow[]
}

export interface BpmnModel {
  processes: BpmnProcess[]
  /** The tree the model was read from. Never changed: a write works on a copy. */
  doc: XDoc
  source: string
}

export class BpmnParseError extends Error {}

/** A problem with an edit the model can't carry out (not with the file). */
export class EditError extends Error {}

export const NODE_TYPES = new Set([
  'task', 'userTask', 'manualTask', 'serviceTask', 'scriptTask', 'businessRuleTask', 'sendTask', 'receiveTask',
  'exclusiveGateway', 'inclusiveGateway', 'complexGateway', 'eventBasedGateway', 'parallelGateway',
  'startEvent', 'endEvent', 'intermediateCatchEvent', 'intermediateThrowEvent', 'boundaryEvent',
  'subProcess', 'transaction', 'adHocSubProcess', 'callActivity',
])

export const GATEWAY_TYPES = new Set([
  'exclusiveGateway', 'inclusiveGateway', 'complexGateway', 'eventBasedGateway', 'parallelGateway',
])

export const emptySpec = (): SpecFields => ({
  actor: '', trigger: '', preconditions: '', inputs: '', rules: '', exceptions: '', firedBy: '', entersAt: '',
  outputs: '', householdSees: '', source: '',
})

export const emptyRefs = (): Refs => ({ owner: '', roles: [], people: [], software: [], documents: [], controls: [] })

export function isGateway(node: BpmnNode): boolean {
  return GATEWAY_TYPES.has(node.type)
}

/** The editable fields of a node, as one string to compare against what the file had (key order does not matter). */
export function nodeSnapshot(n: BpmnNode): string {
  return canonical([
    n.name, n.lane, n.porter, n.spec, n.instructions, n.decisionCriteria, n.commands, n.prompts, n.refs, n.defaultFlow,
    n.calledElement, n.attachedTo, n.interrupting,
  ])
}

export function flowSnapshot(f: BpmnFlow): string {
  return canonical([f.name, f.source, f.target, f.condition, f.meaning, f.porter])
}
