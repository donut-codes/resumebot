// Shared by the package's tests: the BPMN files the host app ships (../../../../playbooks) and a line diff.
import { parseBpmn } from '../bpmn/parse'
import { hasSpecHeadings } from '../bpmn/spec'
import type { BpmnModel, BpmnNode, BpmnProcess } from '../bpmn/model'

const shipped = import.meta.glob('../../../../playbooks/*.bpmn', { query: '?raw', import: 'default', eager: true }) as Record<string, string>

/** Every BPMN file in playbooks/, as [file name, text]. */
export const playbooks: [string, string][] = Object.entries(shipped)
  .map(([path, text]): [string, string] => [path.split('/').pop()!, text])
  .sort((a, b) => a[0].localeCompare(b[0]))

const fixtures = import.meta.glob('./fixtures/*.bpmn', { query: '?raw', import: 'default', eager: true }) as Record<string, string>
export const fixture = (name: string): string => {
  const text = fixtures[`./fixtures/${name}`]
  if (text === undefined) throw new Error(`no fixture ${name}`)
  return text
}

/** The lines that differ between two texts: what was taken out and what was put in, by longest common run. */
export function lineDiff(a: string, b: string): { removed: string[]; added: string[] } {
  const x = a.split('\n')
  const y = b.split('\n')
  // Trim what both share at the ends first: edits are local, so the table below stays small.
  let head = 0
  while (head < x.length && head < y.length && x[head] === y[head]) head++
  let tail = 0
  while (tail < x.length - head && tail < y.length - head && x[x.length - 1 - tail] === y[y.length - 1 - tail]) tail++
  const p = x.slice(head, x.length - tail)
  const q = y.slice(head, y.length - tail)
  const t: number[][] = Array.from({ length: p.length + 1 }, () => new Array<number>(q.length + 1).fill(0))
  for (let i = p.length - 1; i >= 0; i--) {
    for (let j = q.length - 1; j >= 0; j--) t[i][j] = p[i] === q[j] ? t[i + 1][j + 1] + 1 : Math.max(t[i + 1][j], t[i][j + 1])
  }
  const removed: string[] = []
  const added: string[] = []
  let i = 0
  let j = 0
  while (i < p.length && j < q.length) {
    if (p[i] === q[j]) {
      i++
      j++
    } else if (t[i + 1][j] >= t[i][j + 1]) removed.push(p[i++])
    else added.push(q[j++])
  }
  while (i < p.length) removed.push(p[i++])
  while (j < q.length) added.push(q[j++])
  return { removed, added }
}

export const firstProcess = (m: BpmnModel): BpmnProcess => m.processes[0]

export function load(text: string): { model: BpmnModel; pid: string; process: BpmnProcess } {
  const model = parseBpmn(text)
  return { model, pid: model.processes[0].id, process: model.processes[0] }
}

export const node = (p: BpmnProcess, id: string): BpmnNode => {
  const n = p.nodes.find((x) => x.id === id)
  if (!n) throw new Error(`no step ${id}`)
  return n
}

/** What the model means, for comparing two reads of a file: steps and ways out, with every field. */
export function meaning(model: BpmnModel) {
  return model.processes.map((p) => ({
    id: p.id, key: p.key, name: p.name, lanes: p.lanes.map((l) => l.name),
    nodes: p.nodes.map((n) => ({ ...n, el: undefined, base: undefined })),
    flows: p.flows.map((f) => ({ ...f, el: undefined, base: undefined })),
  }))
}

/** The same text with Windows line ends. */
export const crlf = (text: string): string => text.replace(/\r?\n/g, '\r\n')

/** What the process engine reads of a step (`process_engine/loader.py` and `bpmn.py`), kept here to compare with. */
export interface EngineStep {
  instructions: string | null
  lane: string | null
  message: string | null
}

const kids = (el: Element): Element[] => Array.from(el.children)

/**
 * The engine's reading of the parts the view shares with it, written again from `bpmn.py` (`documentation_of`,
 * `_lanes_of`) and `loader.py` (message names), on a different XML parser than the view's own. If the engine changes
 * how it reads these, this is where the test is told.
 */
export function engineReads(xml: string): Map<string, EngineStep> {
  const dom = new DOMParser().parseFromString(xml, 'application/xml')
  if (dom.getElementsByTagName('parsererror').length > 0) throw new Error('the engine would not parse this file')
  const root = dom.documentElement
  const messages = new Map<string, string>()
  for (const c of kids(root)) {
    if (c.localName === 'message') messages.set(c.getAttribute('id') ?? '', c.getAttribute('name') || c.getAttribute('id') || '')
  }
  const lanesOf = (set: Element): Map<string, string> => {
    const mapping = new Map<string, string>()
    for (const lane of kids(set)) {
      if (lane.localName !== 'lane') continue
      const name = (lane.getAttribute('name') ?? '').trim()
      for (const child of kids(lane)) {
        if (child.localName === 'flowNodeRef') {
          const id = (child.textContent ?? '').trim()
          if (id && name) mapping.set(id, name)
        } else if (child.localName === 'childLaneSet') {
          for (const [k, v] of lanesOf(child)) mapping.set(k, v)
        }
      }
    }
    return mapping
  }
  const process = kids(root).find((c) => c.localName === 'process')!
  const lanes = new Map<string, string>()
  for (const set of kids(process)) {
    if (set.localName === 'laneSet') for (const [k, v] of lanesOf(set)) lanes.set(k, v)
  }
  const out = new Map<string, EngineStep>()
  for (const node of kids(process)) {
    const id = node.getAttribute('id')
    if (!id || ['documentation', 'extensionElements', 'incoming', 'outgoing', 'laneSet', 'sequenceFlow'].includes(node.localName)) continue
    let instructions: string | null = null
    for (const doc of kids(node)) {
      const text = (doc.textContent ?? '').trim()
      if (doc.localName !== 'documentation' || !text) continue
      const tag = doc.getAttribute('id') ?? ''
      if (/_(criteria|commands|prompts)$/.test(tag)) continue
      // The engine's `documentation_of` (process_engine/bpmn.py): a tagged one wins; an untagged one is the
      // instructions unless it carries the specification's headings (SPEC_HEADINGS there, `hasSpecHeadings` here).
      if (tag.endsWith('_instructions')) instructions = text
      else if (instructions === null && !hasSpecHeadings(text)) instructions = text
    }
    let message: string | null = null
    for (const d of kids(node)) {
      if (d.localName === 'messageEventDefinition') {
        const ref = d.getAttribute('messageRef')
        message = ref ? (messages.get(ref) ?? ref) : null
      }
    }
    out.set(id, { instructions, lane: lanes.get(id) ?? null, message })
  }
  return out
}
