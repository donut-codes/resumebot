// What a person can do to a process in the Steps list. Each function takes a model and returns a new one (the
// model it was given is never changed), or throws `EditError` saying in plain words why it can't be done. Ids of
// what is already in the file never change; a new step or way out gets a new id from its name.
import {
  EditError, emptyRefs, emptySpec, isGateway,
  type BpmnFlow, type BpmnModel, type BpmnNode, type BpmnProcess,
} from './model'
import { nameFrom, slugWords, type XNode } from './xml'

function cloneNode(n: BpmnNode): BpmnNode {
  return {
    ...n, porter: { ...n.porter }, spec: { ...n.spec },
    refs: {
      ...n.refs, roles: [...n.refs.roles], people: [...n.refs.people], software: [...n.refs.software],
      documents: [...n.refs.documents], controls: n.refs.controls.map((c) => ({ ...c })),
    },
    trigger: n.trigger ? { ...n.trigger } : null,
    multiInstance: n.multiInstance ? { ...n.multiInstance } : null,
  }
}

const cloneFlow = (f: BpmnFlow): BpmnFlow => ({ ...f, porter: { ...f.porter } })

function cloneProcess(p: BpmnProcess): BpmnProcess {
  return { ...p, lanes: p.lanes.map((l) => ({ ...l })), nodes: p.nodes.map(cloneNode), flows: p.flows.map(cloneFlow) }
}

function mutate(model: BpmnModel, processId: string, fn: (p: BpmnProcess) => void): BpmnModel {
  let found = false
  const processes = model.processes.map((p) => {
    if (p.id !== processId) return p
    found = true
    const copy = cloneProcess(p)
    fn(copy)
    return copy
  })
  if (!found) throw new EditError('There is no such process.')
  return { ...model, processes }
}

function nodeIn(p: BpmnProcess, id: string): BpmnNode {
  const n = p.nodes.find((x) => x.id === id)
  if (!n) throw new EditError('That step is not in the process.')
  return n
}

const ins = (p: BpmnProcess, id: string) => p.flows.filter((f) => f.target === id)
const outs = (p: BpmnProcess, id: string) => p.flows.filter((f) => f.source === id)

const slug = slugWords

/** Every id in the file and the model, so a new one never collides. */
function usedIds(model: BpmnModel): Set<string> {
  const ids = new Set<string>()
  const walk = (n: XNode) => {
    if (n.kind !== 'el') return
    const id = n.attrs.find((a) => a.name === 'id')
    if (id) ids.add(id.value)
    n.children.forEach(walk)
  }
  model.doc.nodes.forEach(walk)
  for (const p of model.processes) {
    p.nodes.forEach((n) => ids.add(n.id))
    p.flows.forEach((f) => ids.add(f.id))
  }
  return ids
}

function freshId(model: BpmnModel, p: BpmnProcess, base: string): string {
  const used = usedIds(model)
  p.nodes.forEach((n) => used.add(n.id))
  p.flows.forEach((f) => used.add(f.id))
  let id = base
  for (let i = 2; used.has(id); i++) id = `${base}_${i}`
  return id
}

function newNode(id: string, type: string, name: string, lane: string): BpmnNode {
  return {
    id, type, name, lane, porter: {}, spec: emptySpec(), instructions: '', decisionCriteria: '', commands: '', prompts: '',
    refs: emptyRefs(), attachedTo: null, interrupting: true, calledElement: null, defaultFlow: null, trigger: null,
    multiInstance: null,
  }
}

function newFlow(id: string, source: string, target: string, name = ''): BpmnFlow {
  return { id, name, source, target, condition: '', meaning: '', porter: {} }
}

const isPlain = (f: BpmnFlow) => !f.name && !f.condition && !f.meaning && Object.keys(f.porter).length === 0

// --- editing fields ------------------------------------------------------------------------------------------------

export function updateNode(model: BpmnModel, processId: string, nodeId: string, fn: (n: BpmnNode) => void): BpmnModel {
  return mutate(model, processId, (p) => fn(nodeIn(p, nodeId)))
}

export function updateFlow(model: BpmnModel, processId: string, flowId: string, fn: (f: BpmnFlow) => void): BpmnModel {
  return mutate(model, processId, (p) => {
    const f = p.flows.find((x) => x.id === flowId)
    if (!f) throw new EditError('That way out is not in the process.')
    fn(f)
  })
}

// --- the list operations -------------------------------------------------------------------------------------------

/** A new step right after `nodeId`, on the same path: what led out of it now leads to the new step, which leads on. */
export function addStepAfter(
  model: BpmnModel, processId: string, nodeId: string, opts: { type?: string; name?: string } = {},
): { model: BpmnModel; id: string } {
  let id = ''
  const next = mutate(model, processId, (p) => {
    const x = nodeIn(p, nodeId)
    const out = outs(p, nodeId)
    if (x.type === 'endEvent') throw new EditError('Nothing comes after an end. Add the step before it instead.')
    if (isGateway(x) || out.length > 1) {
      throw new EditError('This leads to more than one place. Add a way out of it instead, or a step after one of them.')
    }
    if (out.length === 0) throw new EditError('Nothing leads out of this step, so there is nowhere to put one after it.')
    const flow = out[0]
    const name = opts.name ?? 'New step'
    id = freshId(model, p, nameFrom(name, 'Step'))
    const step = newNode(id, opts.type ?? 'task', name, x.lane)
    // Whoever does the step before it does this one until it says otherwise: the host's engine usually wants a role.
    if (x.porter.role) step.porter = { role: x.porter.role }
    const after = p.nodes.findIndex((n) => n.id === nodeId)
    p.nodes.splice(after + 1, 0, step)
    const tail = newFlow(freshId(model, p, `S_${id}__${flow.target}`), id, flow.target)
    flow.target = id
    p.flows.splice(p.flows.indexOf(flow) + 1, 0, tail)
  })
  return { model: next, id }
}

/** Takes a step out, joining what led into it to what led out of it. Only a step with one way in and one way out. */
export function removeStep(model: BpmnModel, processId: string, nodeId: string): BpmnModel {
  return mutate(model, processId, (p) => {
    nodeIn(p, nodeId)
    const into = ins(p, nodeId)
    const out = outs(p, nodeId)
    if (into.length !== 1 || out.length !== 1) {
      throw new EditError('Only a step with one way in and one way out can be removed. Remove the extra ways first.')
    }
    if (p.nodes.some((n) => n.attachedTo === nodeId)) {
      throw new EditError('Something is attached to this step (a way it can be interrupted). Remove that first.')
    }
    const [i] = into
    const [o] = out
    if (i.source === nodeId || o.target === nodeId) throw new EditError('This step leads back into itself.')
    // Keep the line that says something (a label, a rule), else the one coming in.
    const keepOut = isPlain(i) && !isPlain(o)
    if (keepOut) {
      const from = p.nodes.find((n) => n.id === i.source)
      if (from && from.defaultFlow === i.id) from.defaultFlow = o.id
      o.source = i.source
      p.flows = p.flows.filter((f) => f !== i)
    } else {
      i.target = o.target
      p.flows = p.flows.filter((f) => f !== o)
    }
    p.nodes = p.nodes.filter((n) => n.id !== nodeId)
  })
}

/** Swaps a step with the one after it (down) or before it (up), on the same path. */
export function moveStep(model: BpmnModel, processId: string, nodeId: string, dir: 'up' | 'down'): BpmnModel {
  return mutate(model, processId, (p) => {
    const plain = (n: BpmnNode) => {
      const i = ins(p, n.id)
      const o = outs(p, n.id)
      return i.length === 1 && o.length === 1 && !isGateway(n) && n.type !== 'boundaryEvent' &&
        !p.nodes.some((m) => m.attachedTo === n.id)
    }
    let first = nodeIn(p, nodeId) // the one that ends up second
    if (dir === 'up') {
      const into = ins(p, nodeId)
      if (into.length !== 1) throw new EditError('This step has no single step before it to swap with.')
      first = nodeIn(p, into[0].source)
    }
    if (!plain(first)) throw new EditError('This step can only move past a plain step on its own path.')
    const [i] = ins(p, first.id)
    const [o] = outs(p, first.id)
    const second = p.nodes.find((n) => n.id === o.target)
    if (!second || !plain(second) || ins(p, second.id).length !== 1) {
      throw new EditError(dir === 'down' ? 'There is no plain step after this one to swap with.' : 'There is no plain step before this one to swap with.')
    }
    const [o2] = outs(p, second.id)
    if (i.source === second.id || o2.target === first.id) throw new EditError('These steps loop; moving them would cut the loop.')
    // P -i-> first -o-> second -o2-> M   becomes   P -i-> second -o-> first -o2-> M
    i.target = second.id
    o.source = second.id
    o.target = first.id
    o2.source = first.id
    const a = p.nodes.indexOf(first)
    const b = p.nodes.indexOf(second)
    ;[p.nodes[a], p.nodes[b]] = [p.nodes[b], p.nodes[a]]
  })
}

/** A new way out of a decision, to `targetId`, or to a new end when none is named. */
export function addWayOut(
  model: BpmnModel, processId: string, gatewayId: string, opts: { name?: string; targetId?: string } = {},
): { model: BpmnModel; flowId: string } {
  let flowId = ''
  const next = mutate(model, processId, (p) => {
    const g = nodeIn(p, gatewayId)
    if (!isGateway(g)) throw new EditError('Only a decision has ways out to add.')
    const name = opts.name?.trim() || 'New way out'
    let target = opts.targetId
    if (target) {
      nodeIn(p, target)
      if (target === gatewayId) throw new EditError('A way out can not lead back to the decision itself.')
    } else {
      target = freshId(model, p, `End_${slug(name) || 'way_out'}`)
      p.nodes.push(newNode(target, 'endEvent', name, g.lane))
    }
    flowId = freshId(model, p, `F_${slug(name) || 'way_out'}`)
    const last = outs(p, gatewayId).pop()
    const flow = newFlow(flowId, gatewayId, target, name)
    p.flows.splice(last ? p.flows.indexOf(last) + 1 : p.flows.length, 0, flow)
  })
  return { model: next, flowId }
}

/** Takes a way out away. A decision keeps at least two; an end only it led to goes with it. */
export function removeWayOut(model: BpmnModel, processId: string, flowId: string): BpmnModel {
  return mutate(model, processId, (p) => {
    const f = p.flows.find((x) => x.id === flowId)
    if (!f) throw new EditError('That way out is not in the process.')
    const g = nodeIn(p, f.source)
    if (!isGateway(g)) throw new EditError('Only a way out of a decision can be removed this way.')
    if (outs(p, g.id).length <= 2) throw new EditError('A decision keeps at least two ways out.')
    p.flows = p.flows.filter((x) => x !== f)
    if (g.defaultFlow === flowId) g.defaultFlow = null
    const target = p.nodes.find((n) => n.id === f.target)
    if (target && target.type === 'endEvent' && ins(p, target.id).length === 0) {
      p.nodes = p.nodes.filter((n) => n !== target)
    }
  })
}

/** Why an operation can't be done right now (in plain words), or null when it can. For greying out a button. */
export function whyNot(operation: () => unknown): string | null {
  try {
    operation()
    return null
  } catch (e) {
    if (e instanceof EditError) return e.message
    throw e
  }
}

