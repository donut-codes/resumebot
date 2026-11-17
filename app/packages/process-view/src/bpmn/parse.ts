// BPMN XML text -> the model (./model.ts). Reads everything the view and the editor use, and leaves the rest in the
// tree the model points at.
import {
  BpmnParseError, BPMN_NS, emptyRefs, flowSnapshot, nodeSnapshot, NODE_TYPES, PA_NS, PORTER_NS,
  type BpmnFlow, type BpmnModel, type BpmnNode, type BpmnProcess, type EventTrigger, type Lane, type MultiInstance,
  type Refs,
} from './model'
import { hasSpecHeadings, readSpec } from './spec'
import { attr, elements, parseXml, textOf, XmlError, type XEl } from './xml'

/** What each event definition element means, by name. */
const TRIGGER_ELEMENTS: Record<string, string> = {
  timerEventDefinition: 'timer', errorEventDefinition: 'error', escalationEventDefinition: 'escalation',
  messageEventDefinition: 'message', signalEventDefinition: 'signal', conditionalEventDefinition: 'conditional',
  compensateEventDefinition: 'compensation', terminateEventDefinition: 'terminate', cancelEventDefinition: 'cancel',
  linkEventDefinition: 'link',
}
const TIMER_SUBTYPES: Record<string, string> = { timeDuration: 'duration', timeDate: 'date', timeCycle: 'cycle' }
const TRIGGER_REF_ATTRS = ['errorRef', 'escalationRef', 'messageRef', 'signalRef', 'activityRef', 'name']

/** The prefixes a file uses for the namespaces the editor writes, found on its root element. */
export interface Prefixes {
  bpmn: string
  porter: string | null
  pa: string | null
}

export function prefixesOf(root: XEl): Prefixes {
  const found: Record<string, string> = {}
  for (const a of root.attrs) {
    if (a.name.startsWith('xmlns:')) found[a.value] = a.name.slice(6)
  }
  const def = root.attrs.find((a) => a.name === 'xmlns')?.value
  return {
    bpmn: found[BPMN_NS] ?? (def === BPMN_NS ? '' : root.prefix),
    porter: found[PORTER_NS] ?? null,
    pa: found[PA_NS] ?? null,
  }
}

function porterAttrs(el: XEl, prefix: string | null): Record<string, string> {
  const out: Record<string, string> = {}
  if (prefix === null) return out
  for (const a of el.attrs) {
    if (a.name.startsWith(`${prefix}:`)) out[a.name.slice(prefix.length + 1)] = a.value
  }
  return out
}

function triggerOf(el: XEl, messages: ReadonlyMap<string, string>): EventTrigger | null {
  for (const child of elements(el)) {
    const kind = TRIGGER_ELEMENTS[child.local]
    if (!kind) continue
    let detail: string | null = null
    let subtype: string | null = null
    // A message is shown by its name (its id when it has none), as the engine names it.
    const ref = kind === 'message' ? attr(child, 'messageRef') : undefined
    if (ref) return { kind, detail: messages.get(ref) ?? ref, subtype }
    for (const g of elements(child)) {
      const text = textOf(g).trim()
      if (!text) continue
      if (TIMER_SUBTYPES[g.local]) {
        detail = text
        subtype = TIMER_SUBTYPES[g.local]
        break
      }
      if (detail === null) detail = text
    }
    if (detail === null) {
      for (const a of TRIGGER_REF_ATTRS) {
        const v = attr(child, a)
        if (v) {
          detail = v
          break
        }
      }
    }
    return { kind, detail, subtype }
  }
  return null
}

function refsOf(el: XEl): Refs {
  const refs = emptyRefs()
  for (const ext of elements(el, 'extensionElements')) {
    for (const c of elements(ext)) {
      if (c.ns !== PA_NS) continue
      const name = (attr(c, 'name') ?? '').trim()
      if (c.local === 'owner' && name) refs.owner = name
      else if (c.local === 'role' && name) refs.roles.push(name)
      else if (c.local === 'person' && name) refs.people.push(name)
      else if (c.local === 'software' && name) refs.software.push(name)
      else if (c.local === 'document' && name) refs.documents.push(name)
      else if (c.local === 'control') {
        const reference = (attr(c, 'reference') ?? '').trim()
        if (reference) refs.controls.push({ reference, framework: (attr(c, 'framework') ?? '').trim() })
      }
    }
  }
  return refs
}

const TAGGED = ['instructions', 'criteria', 'commands', 'prompts'] as const

/** The documentation elements of a step, sorted by what they hold. */
export interface DocSlots {
  spec: XEl | null
  instructions: XEl | null
  criteria: XEl | null
  commands: XEl | null
  prompts: XEl | null
}

/** Which kind of tagged documentation this is (by its id), or null for one with no tag. */
export function docTag(d: XEl): (typeof TAGGED)[number] | null {
  const id = attr(d, 'id') ?? ''
  if (id.endsWith('_criteria')) return 'criteria'
  if (id.endsWith('_commands')) return 'commands'
  if (id.endsWith('_prompts')) return 'prompts'
  if (id.endsWith('_instructions')) return 'instructions'
  return null
}

export function docSlots(el: XEl): DocSlots {
  const slots: DocSlots = { spec: null, instructions: null, criteria: null, commands: null, prompts: null }
  let untagged: XEl | null = null
  for (const d of elements(el, 'documentation')) {
    const text = textOf(d)
    if (!text.trim()) continue
    const id = attr(d, 'id') ?? ''
    if (id.endsWith('_criteria')) slots.criteria ??= d
    else if (id.endsWith('_commands')) slots.commands ??= d
    else if (id.endsWith('_prompts')) slots.prompts ??= d
    else if (id.endsWith('_instructions')) slots.instructions ??= d
    else if (hasSpecHeadings(text) && !slots.spec) slots.spec = d
    else untagged ??= d
  }
  // the upstream project reads an untagged documentation as the instructions.
  slots.instructions ??= untagged
  return slots
}

/** Tagged documentation is the only kind that carries a suffix: the one a field is written to when it is new. */
export const TAG_SUFFIX: Record<(typeof TAGGED)[number], string> = {
  instructions: '_instructions', criteria: '_criteria', commands: '_commands', prompts: '_prompts',
}

function lanesOf(process: XEl): { lanes: Lane[]; laneOf: Map<string, string> } {
  const lanes: Lane[] = []
  const laneOf = new Map<string, string>()
  const walk = (set: XEl) => {
    for (const lane of elements(set, 'lane')) {
      const name = (attr(lane, 'name') ?? '').trim()
      lanes.push({ id: attr(lane, 'id') ?? '', name })
      for (const c of elements(lane)) {
        if (c.local === 'flowNodeRef') {
          // The last lane to name a step in file order is its lane, which is how the engine reads them: a step
          // listed in a lane and in a lane inside it sits in the inner one.
          const id = textOf(c).trim()
          if (id && name) laneOf.set(id, name)
        } else if (c.local === 'childLaneSet') {
          walk(c)
        }
      }
    }
  }
  for (const set of elements(process, 'laneSet')) walk(set)
  return { lanes, laneOf }
}

function multiInstanceOf(el: XEl, prefix: string | null): MultiInstance | null {
  const mi = elements(el, 'multiInstanceLoopCharacteristics')[0]
  if (!mi) return null
  const p = porterAttrs(mi, prefix)
  return {
    sequential: attr(mi, 'isSequential') === 'true',
    collection: p.collection ?? '',
    elementVariable: p.elementVariable ?? '',
  }
}

export function parseNode(
  el: XEl, prefixes: Prefixes, laneOf: Map<string, string>, messages: ReadonlyMap<string, string> = new Map(),
): BpmnNode {
  const id = attr(el, 'id') ?? ''
  const slots = docSlots(el)
  const node: BpmnNode = {
    id,
    type: el.local,
    name: attr(el, 'name') ?? '',
    lane: laneOf.get(id) ?? '',
    porter: porterAttrs(el, prefixes.porter),
    spec: slots.spec ? readSpec(textOf(slots.spec)) : readSpec(''),
    instructions: slots.instructions ? textOf(slots.instructions).trim() : '',
    decisionCriteria: slots.criteria ? textOf(slots.criteria).trim() : '',
    commands: slots.commands ? textOf(slots.commands).trim() : '',
    prompts: slots.prompts ? textOf(slots.prompts).trim() : '',
    refs: refsOf(el),
    attachedTo: attr(el, 'attachedToRef') ?? null,
    // BPMN's cancelActivity is true when it is not written.
    interrupting: attr(el, 'cancelActivity') !== 'false',
    calledElement: attr(el, 'calledElement') ?? null,
    defaultFlow: attr(el, 'default') ?? null,
    trigger: triggerOf(el, messages),
    multiInstance: multiInstanceOf(el, prefixes.porter),
    el,
  }
  node.base = nodeSnapshot(node)
  return node
}

export function parseFlow(el: XEl, prefixes: Prefixes): BpmnFlow {
  const doc = elements(el, 'documentation')[0]
  const cond = elements(el, 'conditionExpression')[0]
  const flow: BpmnFlow = {
    id: attr(el, 'id') ?? '',
    name: attr(el, 'name') ?? '',
    source: attr(el, 'sourceRef') ?? '',
    target: attr(el, 'targetRef') ?? '',
    condition: cond ? textOf(cond).trim() : '',
    meaning: doc ? textOf(doc).trim() : '',
    porter: porterAttrs(el, prefixes.porter),
    el,
  }
  flow.base = flowSnapshot(flow)
  return flow
}

function parseProcess(pe: XEl, prefixes: Prefixes, messages: ReadonlyMap<string, string>): BpmnProcess {
  const { lanes, laneOf } = lanesOf(pe)
  const nodes: BpmnNode[] = []
  const flows: BpmnFlow[] = []
  const seen = new Set<string>()
  for (const child of elements(pe)) {
    if (child.ns !== BPMN_NS) continue
    if (child.local === 'sequenceFlow') {
      if (attr(child, 'sourceRef') && attr(child, 'targetRef')) flows.push(parseFlow(child, prefixes))
    } else if (NODE_TYPES.has(child.local)) {
      const id = attr(child, 'id')
      if (!id || seen.has(id)) continue
      seen.add(id)
      nodes.push(parseNode(child, prefixes, laneOf, messages))
    }
  }
  const doc = elements(pe, 'documentation')[0]
  const id = attr(pe, 'id') ?? ''
  return {
    id,
    name: attr(pe, 'name') ?? '',
    key: porterAttrs(pe, prefixes.porter).key ?? id,
    documentation: doc ? textOf(doc).trim() : '',
    lanes,
    nodes,
    flows,
    el: pe,
    origNodes: [...nodes],
    origFlows: [...flows],
  }
}

/** Reads a BPMN file. Throws `BpmnParseError` (with the reason) for text that isn't one. */
export function parseBpmn(xml: string): BpmnModel {
  let doc
  try {
    doc = parseXml(xml)
  } catch (e) {
    if (e instanceof XmlError) throw new BpmnParseError(`Not valid XML: ${e.message}`)
    // Nested so deep that the reader ran out of room (the depth limit stops this first).
    if (e instanceof RangeError) throw new BpmnParseError('Not valid XML: the elements are nested too deep')
    throw e
  }
  const root = doc.nodes.find((n): n is XEl => n.kind === 'el')!
  if (root.local !== 'definitions') {
    throw new BpmnParseError(`Expected a <bpmn:definitions> root, found <${root.local}>`)
  }
  const prefixes = prefixesOf(root)
  // The messages a file declares, by id: a message start or wait is shown by the message's name, as the engine names it.
  const messages = new Map<string, string>()
  for (const m of elements(root, 'message')) {
    const id = attr(m, 'id')
    if (id) messages.set(id, attr(m, 'name') || id)
  }
  const processes = elements(root, 'process').filter((p) => p.ns === BPMN_NS).map((p) => parseProcess(p, prefixes, messages))
  if (processes.length === 0) throw new BpmnParseError('No <bpmn:process> found')
  return { processes, doc, source: xml }
}
