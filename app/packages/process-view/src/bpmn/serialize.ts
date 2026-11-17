// The model -> BPMN XML text. It works on a copy of the tree the model was read from and writes only what differs
// from what was read, so a model nobody changed comes out as the same text, and an edit changes its own lines.
import { sameValue } from './canon'
import {
  BPMNDI_NS, DC_NS, DI_NS, emptyRefs, emptySpec, flowSnapshot, nodeSnapshot, PA_NS, PORTER_NS, XSI_NS,
  type BpmnFlow, type BpmnModel, type BpmnNode, type BpmnProcess, type Refs, type SpecFields,
} from './model'
import { docSlots, docTag, prefixesOf, TAG_SUFFIX, type Prefixes } from './parse'
import { applySpec } from './spec'
import {
  attr, elements, insertBefore, insertChild, removeChild, setAttr, setText, slugWords, textOf, writeXml,
  type XDoc, type XEl, type XNode,
} from './xml'

// --- copying the tree, keeping track of which element is which -------------------------------------------------------

function cloneWithMap(doc: XDoc): { doc: XDoc; map: Map<XEl, XEl> } {
  const map = new Map<XEl, XEl>()
  parents = new Map<XEl, XEl>()
  const copy = (n: XNode, parent: XEl | null): XNode => {
    if (n.kind !== 'el') return { ...n }
    const el: XEl = { ...n, attrs: n.attrs.map((a) => ({ ...a })), children: [] }
    map.set(n, el)
    if (parent) parents.set(el, parent)
    el.children = n.children.map((c) => copy(c, el))
    return el
  }
  return { doc: { nodes: doc.nodes.map((n) => copy(n, null)), eol: doc.eol }, map }
}

// The copy's parents, to work out how deep an element is indented. An element made while writing is added here by
// `put` (and by `lay` for the elements it holds), so it is indented like the elements around it.
let parents = new Map<XEl, XEl>()

/** The line break and indent in front of `el` in the file. */
function ownIndent(el: XEl): string {
  const parent = parents.get(el)
  if (!parent) return '\n'
  const before = parent.children[parent.children.indexOf(el) - 1]
  const m = before && before.kind === 'text' ? /\n[ \t]*$/.exec(before.raw ?? before.text) : null
  return m ? m[0] : '\n'
}

/** The line break and indent in front of an element's children: the file's own, else one step in from the element. */
function childPad(el: XEl): string {
  for (const c of el.children) {
    if (c.kind === 'text') {
      const m = /^\s*(\n[ \t]*)$/.exec(c.raw ?? c.text)
      if (m) return m[1]
    }
  }
  return ownIndent(el) + '  '
}

/** Puts a new element into `parent` at index `at`, on its own line indented `pad`, and remembers whose it is. */
function put(parent: XEl, at: number, child: XEl, pad: string): void {
  insertChild(parent, at, child, pad)
  parents.set(child, parent)
}

/** Puts a new element on its own line just before `before`. */
function putBefore(parent: XEl, before: XNode, child: XEl, pad: string): void {
  insertBefore(parent, before, child, pad)
  parents.set(child, parent)
}

// --- small builders -------------------------------------------------------------------------------------------------

const qn = (prefix: string, local: string) => (prefix ? `${prefix}:${local}` : local)

function mk(name: string, attrs: [string, string][] = [], kids: XNode[] = []): XEl {
  const colon = name.indexOf(':')
  return {
    kind: 'el', name, prefix: colon < 0 ? '' : name.slice(0, colon), local: colon < 0 ? name : name.slice(colon + 1),
    ns: '', attrs: attrs.map(([n, v]) => ({ name: n, value: v, quote: '"' as const, sep: ' ' })), children: kids,
    selfClose: kids.length === 0, tail: '',
  }
}

function withText(name: string, attrs: [string, string][], text: string): XEl {
  const el = mk(name, attrs)
  setText(el, text)
  return el
}

/** `el` holding `kids`, each on its own line, indented one step in from `pad` (the element's own indent). */
function lay(el: XEl, kids: XEl[], pad: string): XEl {
  if (kids.length === 0) {
    el.children = []
    el.selfClose = true
    return el
  }
  el.selfClose = false
  el.children = []
  for (const k of kids) {
    el.children.push({ kind: 'text', text: pad + '  ' }, k)
    parents.set(k, el)
  }
  el.children.push({ kind: 'text', text: pad })
  return el
}

/** What a text read as `old` should be written as when its words become `value`: the same space around them. */
function keepSpace(old: string, value: string): string {
  if (old.trim() === '') return value
  const lead = /^\s*/.exec(old)?.[0] ?? ''
  const trail = /\s*$/.exec(old)?.[0] ?? ''
  return lead + value + trail
}

/** Declares a namespace on the root when the file hasn't, and says what prefix it has. */
function ensurePrefix(root: XEl, ns: string, preferred: string, known: string | null): string {
  if (known !== null) return known
  const taken = new Set(root.attrs.map((a) => a.name))
  let name = preferred
  for (let i = 2; taken.has(`xmlns:${name}`); i++) name = `${preferred}${i}`
  const last = root.attrs[root.attrs.length - 1]
  root.attrs.push({ name: `xmlns:${name}`, value: ns, quote: '"', sep: last && last.sep.includes('\n') ? last.sep : ' ' })
  return name
}

function bump(p: Prefixes, root: XEl) {
  return {
    porter: () => {
      p.porter = ensurePrefix(root, PORTER_NS, 'porter', p.porter)
      return p.porter
    },
    pa: () => {
      p.pa = ensurePrefix(root, PA_NS, 'pa', p.pa)
      return p.pa
    },
    xsi: () => ensurePrefix(root, XSI_NS, 'xsi', root.attrs.find((a) => a.value === XSI_NS && a.name.startsWith('xmlns:'))?.name.slice(6) ?? null),
  }
}

// --- reading what the file had --------------------------------------------------------------------------------------

interface NodeBase {
  name: string
  lane: string
  porter: Record<string, string>
  spec: SpecFields
  instructions: string
  decisionCriteria: string
  commands: string
  prompts: string
  refs: Refs
  defaultFlow: string | null
  calledElement: string | null
  attachedTo: string | null
  interrupting: boolean
}

function nodeBase(n: BpmnNode): NodeBase | null {
  if (!n.base) return null
  const [name, lane, porter, spec, instructions, decisionCriteria, commands, prompts, refs, defaultFlow, calledElement, attachedTo, interrupting] =
    JSON.parse(n.base)
  return { name, lane, porter, spec, instructions, decisionCriteria, commands, prompts, refs, defaultFlow, calledElement, attachedTo, interrupting }
}

function flowBase(f: BpmnFlow) {
  if (!f.base) return null
  const [name, source, target, condition, meaning, porter] = JSON.parse(f.base)
  return { name, source, target, condition, meaning, porter } as {
    name: string; source: string; target: string; condition: string; meaning: string; porter: Record<string, string>
  }
}

// --- where a new child of a flow node goes: after documentation and extensions, before the rest -------------------------

function firstAfterHead(el: XEl, kinds: string[]): number {
  let at = 0
  el.children.forEach((c, i) => {
    if (c.kind === 'el' && kinds.includes(c.local)) at = i + 1
  })
  return at
}

function addDoc(el: XEl, doc: XEl): void {
  put(el, firstAfterHead(el, ['documentation']), doc, childPad(el))
}

// --- who/with what (the upstream project's extension elements) -------------------------------------------------------------

const REF_KINDS = ['owner', 'role', 'person', 'software', 'document', 'control'] as const
type RefKind = (typeof REF_KINDS)[number]

/** Each kind of reference as the keys that tell its elements apart: a name, or a control's reference and framework. */
function refKeys(refs: Refs): Record<RefKind, string[]> {
  // A reference with no name names nothing (the reader skips such an element), so none is written.
  const named = (names: string[]) => names.filter((n) => n.trim() !== '')
  return {
    owner: named(refs.owner ? [refs.owner] : []),
    role: named(refs.roles),
    person: named(refs.people),
    software: named(refs.software),
    document: named(refs.documents),
    control: refs.controls.filter((c) => c.reference.trim() !== '').map((c) => `${c.reference}\u0001${c.framework}`),
  }
}

/** The kind of reference an element of the extension elements is, when the upstream project wrote it and it names something. */
function refKindOf(c: XNode): RefKind | null {
  if (c.kind !== 'el' || c.ns !== PA_NS) return null
  const kind = REF_KINDS.find((k) => k === c.local)
  if (!kind) return null
  if (kind === 'control') return (attr(c, 'reference') ?? '').trim() ? kind : null
  return (attr(c, 'name') ?? '').trim() ? kind : null
}

function refKey(el: XEl, kind: RefKind): string {
  if (kind === 'control') return `${(attr(el, 'reference') ?? '').trim()}\u0001${(attr(el, 'framework') ?? '').trim()}`
  return (attr(el, 'name') ?? '').trim()
}

function setRef(el: XEl, kind: RefKind, key: string): void {
  if (kind === 'control') {
    const [reference, framework] = key.split('\u0001')
    setAttr(el, 'reference', reference)
    setAttr(el, 'framework', framework ? framework : null)
  } else {
    setAttr(el, 'name', key)
  }
}

/**
 * Writes the references that differ from `was` (what the file had) into the step's extension elements. An element
 * that stays is not touched, so anything else on it, an element the upstream project added that this does not know, or a
 * comment, is kept; an element whose value changed is changed in place.
 */
function writeRefs(el: XEl, refs: Refs, was: Refs | null, prefixes: Prefixes, root: XEl, kidPad: string = childPad(el)): void {
  const want = refKeys(refs)
  const had = refKeys(was ?? emptyRefs())
  const changed = REF_KINDS.filter((k) => !sameValue(want[k], had[k]))
  if (changed.length === 0) return
  let ext = elements(el, 'extensionElements')[0]
  if (!ext) {
    if (changed.every((k) => want[k].length === 0)) return
    ext = mk(qn(prefixesOf(root).bpmn, 'extensionElements'))
    put(el, firstAfterHead(el, ['documentation']), ext, kidPad)
  }
  const host = ext
  const pad = childPad(host)
  const recognized = () => host.children.filter((c): c is XEl => refKindOf(c) !== null)

  for (const kind of changed) {
    const existing = host.children.filter((c): c is XEl => c.kind === 'el' && refKindOf(c) === kind)
    const wanted = want[kind]
    // What is still there keeps its element; then what changed takes the elements that are left, in place.
    const used = new Set<XEl>()
    const match = wanted.map((key) => {
      const found = existing.find((x) => !used.has(x) && refKey(x, kind) === key)
      if (found) used.add(found)
      return found ?? null
    })
    const spare = existing.filter((x) => !used.has(x))
    const final = match.map((found, i) => {
      if (found) return found
      const reuse = spare.shift()
      if (reuse) {
        setRef(reuse, kind, wanted[i])
        return reuse
      }
      const fresh = mk(`${bump(prefixes, root).pa()}:${kind}`)
      fresh.ns = PA_NS
      setRef(fresh, kind, wanted[i])
      return fresh
    })
    for (const gone of spare) removeChild(host, gone)

    // Elements that stay must still be in the order they are wanted in.
    const stay = final.filter((x) => host.children.includes(x))
    const at = stay.map((x) => host.children.indexOf(x))
    if (at.some((p, i) => i > 0 && p < at[i - 1])) for (const x of stay) removeChild(host, x)

    let prev: XEl | null = null
    for (const x of final) {
      if (host.children.includes(x)) {
        prev = x
        continue
      }
      if (prev) {
        put(host, host.children.indexOf(prev) + 1, x, pad)
      } else {
        const later = final.find((y) => host.children.includes(y))
        const others = recognized()
        const lower = others.filter((o) => REF_KINDS.indexOf(refKindOf(o)!) <= REF_KINDS.indexOf(kind))
        if (later) putBefore(host, later, x, pad)
        else if (lower.length) put(host, host.children.indexOf(lower[lower.length - 1]) + 1, x, pad)
        else if (others.length) putBefore(host, others[0], x, pad)
        else put(host, host.children.length, x, pad)
      }
      prev = x
    }
  }
  // Nothing left in it: take the element away (but not one that holds a comment or an element of someone else's).
  if (!host.children.some((c) => c.kind !== 'text' || c.text.trim() !== '')) removeChild(el, host)
}

/** A step's tagged documentation: the field, and the documentation id suffix it is written under. */
const TAGGED_FIELDS = [
  ['instructions', 'instructions'], ['decisionCriteria', 'criteria'], ['commands', 'commands'], ['prompts', 'prompts'],
] as const

/** Whether some element of the file (other than `except`) already has this id. */
function idTaken(root: XEl, id: string, except: XEl): boolean {
  const walk = (el: XEl): boolean => el !== except && (attr(el, 'id') === id || el.children.some((c) => c.kind === 'el' && walk(c)))
  return walk(root)
}

/**
 * The engine reads the first untagged `<documentation>` of a step as its instructions, and the specification is one.
 * So where a step has both, its instructions are tagged (`<step>_instructions`), and are read as instructions
 * whichever comes first.
 */
function tagInstructions(target: XEl, stepId: string, root: XEl): void {
  const slots = docSlots(target)
  if (!slots.spec || !slots.instructions || docTag(slots.instructions) !== null) return
  const id = `${stepId}${TAG_SUFFIX.instructions}`
  // An empty documentation already tagged so says nothing; it makes way.
  for (const d of elements(target, 'documentation')) {
    if (d !== slots.instructions && attr(d, 'id') === id && textOf(d).trim() === '') removeChild(target, d)
  }
  if (!idTaken(root, id, slots.instructions)) setAttr(slots.instructions, 'id', id)
}

// --- a step's fields ------------------------------------------------------------------------------------------------

function writeNodeFields(n: BpmnNode, target: XEl, orig: XEl, prefixes: Prefixes, root: XEl): void {
  const base = nodeBase(n)
  if (!base) return
  const b = bump(prefixes, root)
  const same = (k: keyof NodeBase) => sameValue(base[k], n[k as keyof BpmnNode])
  if (!same('name')) setAttr(target, 'name', n.name === '' ? null : n.name)
  if (!same('defaultFlow')) setAttr(target, 'default', n.defaultFlow)
  if (!same('calledElement')) setAttr(target, 'calledElement', n.calledElement)
  if (!same('attachedTo')) setAttr(target, 'attachedToRef', n.attachedTo)
  if (!same('interrupting')) setAttr(target, 'cancelActivity', n.interrupting ? null : 'false')
  if (!same('porter')) {
    for (const key of new Set([...Object.keys(base.porter), ...Object.keys(n.porter)])) {
      if (base.porter[key] === n.porter[key]) continue
      const value = n.porter[key]
      if (value === undefined) setAttr(target, `${prefixes.porter}:${key}`, null)
      else setAttr(target, `${b.porter()}:${key}`, value)
    }
  }
  const slots = docSlots(orig)
  const mapped = (x: XEl | null) => (x ? (cloneMap.get(x) ?? null) : null)
  let words = false
  if (!same('spec')) {
    words = true
    const was = slots.spec ? textOf(slots.spec) : ''
    const next = applySpec(was, base.spec, n.spec)
    const el = mapped(slots.spec)
    if (el) {
      if (next.trim() === '') removeChild(target, el)
      else setText(el, next)
    } else if (next.trim() !== '') {
      // The specification goes first: the engine reads the first documentation as the step's own words.
      put(target, 0, withText(qn(prefixesOf(root).bpmn, 'documentation'), [], next), childPad(target))
    }
  }
  for (const [field, slot] of TAGGED_FIELDS) {
    if (same(field)) continue
    if (slot === 'instructions') words = true
    const value = n[field]
    const el = mapped(slots[slot])
    if (el) {
      if (value.trim() === '') removeChild(target, el)
      else setText(el, keepSpace(textOf(el), value))
    } else if (value.trim() !== '') {
      const doc = withText(qn(prefixesOf(root).bpmn, 'documentation'), [['id', `${n.id}${TAG_SUFFIX[slot]}`]], value)
      addDoc(target, doc)
    }
  }
  if (words) tagInstructions(target, n.id, root)
  if (!same('refs')) writeRefs(target, n.refs, base.refs, prefixes, root)
}

// The map from the file's own elements to the copy being written, for the call in progress.
let cloneMap = new Map<XEl, XEl>()

function buildNode(n: BpmnNode, pad: string, prefixes: Prefixes, root: XEl): XEl {
  const bp = prefixesOf(root).bpmn
  const attrs: [string, string][] = [['id', n.id]]
  if (n.name) attrs.push(['name', n.name])
  if (n.attachedTo) attrs.push(['attachedToRef', n.attachedTo])
  if (n.attachedTo && !n.interrupting) attrs.push(['cancelActivity', 'false'])
  if (n.calledElement) attrs.push(['calledElement', n.calledElement])
  if (n.defaultFlow) attrs.push(['default', n.defaultFlow])
  const b = bump(prefixes, root)
  for (const [k, v] of Object.entries(n.porter)) attrs.push([`${b.porter()}:${k}`, v])
  const el = mk(qn(bp, n.type), attrs)
  const spec = applySpec('', emptySpec(), n.spec)
  const kids: XEl[] = []
  if (spec.trim()) kids.push(withText(qn(bp, 'documentation'), [], spec))
  for (const [field, slot] of TAGGED_FIELDS) {
    if (n[field].trim()) kids.push(withText(qn(bp, 'documentation'), [['id', `${n.id}${TAG_SUFFIX[slot]}`]], n[field]))
  }
  lay(el, kids, pad)
  writeRefs(el, n.refs, null, prefixes, root, pad + '  ')
  return el
}

// --- flows ---------------------------------------------------------------------------------------------------------

function writeFlowFields(f: BpmnFlow, target: XEl, prefixes: Prefixes, root: XEl): void {
  const base = flowBase(f)
  if (!base) return
  const bp = prefixesOf(root).bpmn
  if (base.name !== f.name) setAttr(target, 'name', f.name === '' ? null : f.name)
  if (base.source !== f.source) setAttr(target, 'sourceRef', f.source)
  if (base.target !== f.target) setAttr(target, 'targetRef', f.target)
  if (!sameValue(base.porter, f.porter)) {
    const b = bump(prefixes, root)
    for (const key of new Set([...Object.keys(base.porter), ...Object.keys(f.porter)])) {
      if (base.porter[key] === f.porter[key]) continue
      if (f.porter[key] === undefined) setAttr(target, `${prefixes.porter}:${key}`, null)
      else setAttr(target, `${b.porter()}:${key}`, f.porter[key])
    }
  }
  if (base.meaning !== f.meaning) {
    const doc = elements(target, 'documentation')[0]
    if (doc) {
      if (f.meaning === '') removeChild(target, doc)
      else setText(doc, keepSpace(textOf(doc), f.meaning))
    } else if (f.meaning !== '') {
      put(target, 0, withText(qn(bp, 'documentation'), [], f.meaning), childPad(target))
    }
  }
  if (base.condition !== f.condition) {
    const cond = elements(target, 'conditionExpression')[0]
    if (cond) {
      if (f.condition === '') removeChild(target, cond)
      else setText(cond, keepSpace(textOf(cond), f.condition))
    } else if (f.condition !== '') {
      put(target, target.children.length, conditionEl(f.condition, root, prefixes), childPad(target))
    }
  }
}

function conditionEl(text: string, root: XEl, prefixes: Prefixes): XEl {
  const bp = prefixesOf(root).bpmn
  const xsi = bump(prefixes, root).xsi()
  return withText(qn(bp, 'conditionExpression'), [[`${xsi}:type`, `${qn(bp, 'tFormalExpression')}`]], text)
}

function buildFlow(f: BpmnFlow, pad: string, prefixes: Prefixes, root: XEl): XEl {
  const bp = prefixesOf(root).bpmn
  const attrs: [string, string][] = [['id', f.id]]
  if (f.name) attrs.push(['name', f.name])
  attrs.push(['sourceRef', f.source], ['targetRef', f.target])
  const b = bump(prefixes, root)
  for (const [k, v] of Object.entries(f.porter)) attrs.push([`${b.porter()}:${k}`, v])
  const kids: XEl[] = []
  if (f.meaning) kids.push(withText(qn(bp, 'documentation'), [], f.meaning))
  if (f.condition) kids.push(conditionEl(f.condition, root, prefixes))
  return lay(mk(qn(bp, 'sequenceFlow'), attrs), kids, pad)
}

// --- lanes ---------------------------------------------------------------------------------------------------------

function setLane(pe: XEl, nodeId: string, laneName: string, root: XEl, processId: string): void {
  const bp = prefixesOf(root).bpmn
  const sets = elements(pe, 'laneSet')
  const allLanes: XEl[] = []
  const collect = (set: XEl) => {
    for (const l of elements(set, 'lane')) {
      allLanes.push(l)
      for (const c of elements(l, 'childLaneSet')) collect(c)
    }
  }
  sets.forEach(collect)
  for (const lane of allLanes) {
    for (const ref of elements(lane, 'flowNodeRef')) if (textOf(ref).trim() === nodeId) removeChild(lane, ref)
  }
  if (!laneName) return
  let lane = allLanes.find((l) => (attr(l, 'name') ?? '').trim().toLowerCase() === laneName.trim().toLowerCase())
  if (!lane) {
    let set = sets[0]
    const pad = childPad(pe)
    if (!set) {
      set = lay(mk(qn(bp, 'laneSet'), [['id', `LaneSet_${processId}`]]), [], pad)
      const firstNode = pe.children.findIndex((c) => c.kind === 'el' && c.local !== 'documentation' && c.local !== 'extensionElements')
      put(pe, firstNode < 0 ? pe.children.length : firstNode, set, pad)
    }
    const slug = slugWords(laneName) || 'lane'
    const ids = new Set(allLanes.map((l) => attr(l, 'id')))
    let id = `Lane_${slug}`
    for (let i = 2; ids.has(id); i++) id = `Lane_${slug}_${i}`
    lane = mk(qn(bp, 'lane'), [['id', id], ['name', laneName.trim()]])
    put(set, set.children.length, lane, childPad(set))
  }
  const ref = withText(qn(bp, 'flowNodeRef'), [], nodeId)
  // A lane lists its steps before the lanes inside it.
  const inner = elements(lane, 'childLaneSet')[0]
  if (inner) putBefore(lane, inner, ref, childPad(lane))
  else put(lane, lane.children.length, ref, childPad(lane))
}

// --- incoming and outgoing ------------------------------------------------------------------------------------------

function syncRefs(el: XEl, kind: 'incoming' | 'outgoing', wanted: string[], root: XEl): void {
  const bp = prefixesOf(root).bpmn
  const have = elements(el, kind)
  const haveIds = have.map((h) => textOf(h).trim())
  if (haveIds.length === wanted.length && haveIds.every((id, i) => id === wanted[i])) return
  for (const h of have) if (!wanted.includes(textOf(h).trim())) removeChild(el, h)
  const kept = elements(el, kind)
  const keptIds = kept.map((h) => textOf(h).trim())
  for (const id of wanted) {
    if (keptIds.includes(id)) continue
    const last = elements(el, kind).pop()
    const at = last
      ? el.children.indexOf(last) + 1
      : kind === 'outgoing'
        ? Math.max(firstAfterHead(el, ['documentation', 'extensionElements', 'incoming']), 0)
        : firstAfterHead(el, ['documentation', 'extensionElements'])
    put(el, at, withText(qn(bp, kind), [], id), childPad(el))
    keptIds.push(id)
  }
}

// --- the diagram ---------------------------------------------------------------------------------------------------

interface Box { x: number; y: number; w: number; h: number }

function writeDiagram(root: XEl, p: BpmnProcess, removedIds: Set<string>, rewired: BpmnFlow[], addedNodes: BpmnNode[], addedFlows: BpmnFlow[]): void {
  const plane = root.children
    .filter((c): c is XEl => c.kind === 'el' && c.ns === BPMNDI_NS && c.local === 'BPMNDiagram')
    .flatMap((d) => elements(d, 'BPMNPlane'))
    .find((pl) => {
      const ref = attr(pl, 'bpmnElement')
      return ref === p.id || elements(root, 'collaboration').some((c) => elements(c, 'participant').some((x) => attr(x, 'processRef') === p.id && attr(x, 'id') === ref))
    })
  if (!plane) return
  // The prefix the diagram already uses for a namespace, found before anything is taken out of it.
  const inDiagram = (ns: string): string | null => {
    if (plane.ns === ns) return plane.prefix
    const walk = (el: XEl): string | null => {
      for (const c of el.children) {
        if (c.kind !== 'el') continue
        if (c.ns === ns) return c.prefix
        const inner = walk(c)
        if (inner !== null) return inner
      }
      return null
    }
    return walk(plane)
  }
  const known = new Map<string, string | null>([BPMNDI_NS, DC_NS, DI_NS].map((ns): [string, string | null] => [ns, inDiagram(ns)]))
  for (const c of [...elements(plane)]) {
    if (removedIds.has(attr(c, 'bpmnElement') ?? '')) removeChild(plane, c)
  }
  const bounds = new Map<string, Box>()
  for (const s of elements(plane, 'BPMNShape')) {
    const b = elements(s, 'Bounds', DC_NS)[0]
    if (b) {
      bounds.set(attr(s, 'bpmnElement') ?? '', {
        x: Number(attr(b, 'x') ?? 0), y: Number(attr(b, 'y') ?? 0), w: Number(attr(b, 'width') ?? 100), h: Number(attr(b, 'height') ?? 80),
      })
    }
  }
  // A namespace is declared on the root only when something is drawn and the diagram has no prefix for it: a file
  // that is only read, or whose diagram declares its own namespaces, keeps its root as it is.
  const prefixFor = (ns: string, preferred: string): string => {
    const found = known.get(ns)
    if (found !== null && found !== undefined) return found
    const declared = root.attrs.find((x) => x.value === ns && x.name.startsWith('xmlns:'))
    return ensurePrefix(root, ns, preferred, declared ? declared.name.slice(6) : null)
  }
  const di = () => prefixFor(BPMNDI_NS, 'bpmndi')
  const dc = () => prefixFor(DC_NS, 'dc')
  const dd = () => prefixFor(DI_NS, 'di')
  const pad = childPad(plane)
  const size = (n: BpmnNode) => (n.type.endsWith('Event') ? { w: 36, h: 36 } : n.type.endsWith('Gateway') ? { w: 50, h: 50 } : { w: 100, h: 80 })
  const rightmost = [...bounds.values()].reduce((m, b) => Math.max(m, b.x + b.w), 0)
  let nextX = rightmost + 50
  for (const n of addedNodes) {
    const s = size(n)
    // Beside the step it follows, when the file has drawn that one.
    const into = p.flows.find((f) => f.target === n.id)
    const from = into ? bounds.get(into.source) : undefined
    const box: Box = from ? { x: from.x + from.w + 50, y: from.y + (from.h - s.h) / 2, w: s.w, h: s.h } : { x: nextX, y: 0, w: s.w, h: s.h }
    nextX = Math.max(nextX, box.x) + s.w + 50
    bounds.set(n.id, box)
    const shape = mk(qn(di(), 'BPMNShape'), [['id', `${n.id}_di`], ['bpmnElement', n.id]])
    lay(shape, [mk(qn(dc(), 'Bounds'), [['x', String(Math.round(box.x))], ['y', String(Math.round(box.y))], ['width', String(box.w)], ['height', String(box.h)]])], pad)
    put(plane, plane.children.length, shape, pad)
  }
  const centre = (id: string): [number, number] | null => {
    const b = bounds.get(id)
    return b ? [Math.round(b.x + b.w / 2), Math.round(b.y + b.h / 2)] : null
  }
  const edgeFor = (f: BpmnFlow): XEl | null => {
    const a = centre(f.source)
    const z = centre(f.target)
    if (!a || !z) return null
    const e = mk(qn(di(), 'BPMNEdge'), [['id', `${f.id}_di`], ['bpmnElement', f.id]])
    return lay(e, [mk(qn(dd(), 'waypoint'), [['x', String(a[0])], ['y', String(a[1])]]), mk(qn(dd(), 'waypoint'), [['x', String(z[0])], ['y', String(z[1])]])], pad)
  }
  for (const f of rewired) {
    for (const c of elements(plane, 'BPMNEdge')) if (attr(c, 'bpmnElement') === f.id) removeChild(plane, c)
  }
  for (const f of [...rewired, ...addedFlows]) {
    const e = edgeFor(f)
    if (e) put(plane, plane.children.length, e, pad)
  }
}

// --- a process -----------------------------------------------------------------------------------------------------

function writeProcess(p: BpmnProcess, root: XEl, prefixes: Prefixes): void {
  const pe = cloneMap.get(p.el)
  if (!pe) return
  const nodeEls = new Set(p.nodes.map((n) => n.el).filter(Boolean))
  const flowEls = new Set(p.flows.map((f) => f.el).filter(Boolean))
  const removedIds = new Set<string>()

  // Removed.
  for (const n of p.origNodes) {
    if (nodeEls.has(n.el)) continue
    const el = cloneMap.get(n.el!)
    if (el) removeChild(pe, el)
    removedIds.add(n.id)
    setLane(pe, n.id, '', root, p.id)
  }
  for (const f of p.origFlows) {
    if (flowEls.has(f.el)) continue
    const el = cloneMap.get(f.el!)
    if (el) removeChild(pe, el)
    removedIds.add(f.id)
  }

  // Changed fields of what stays.
  const rewired: BpmnFlow[] = []
  for (const n of p.nodes) {
    if (!n.el) continue
    const target = cloneMap.get(n.el)!
    if (n.base !== nodeSnapshot(n)) {
      writeNodeFields(n, target, n.el, prefixes, root)
      const base = nodeBase(n)
      if (base && base.lane !== n.lane) setLane(pe, n.id, n.lane, root, p.id)
    }
  }
  for (const f of p.flows) {
    if (!f.el) continue
    if (f.base !== flowSnapshot(f)) {
      writeFlowFields(f, cloneMap.get(f.el)!, prefixes, root)
      const base = flowBase(f)
      if (base && (base.source !== f.source || base.target !== f.target)) rewired.push(f)
    }
  }

  // New steps and flows go in beside what they follow.
  const pad = childPad(pe)
  const addedNodes: BpmnNode[] = []
  const created = new Map<BpmnNode, XEl>()
  p.nodes.forEach((n, i) => {
    if (n.el) return
    const el = buildNode(n, pad, prefixes, root)
    created.set(n, el)
    addedNodes.push(n)
    let prev: XEl | undefined
    for (let k = i - 1; k >= 0 && !prev; k--) prev = created.get(p.nodes[k]) ?? (p.nodes[k].el ? cloneMap.get(p.nodes[k].el!) : undefined)
    let at = prev ? pe.children.indexOf(prev) + 1 : -1
    if (at < 0) {
      const firstFlow = pe.children.findIndex((c) => c.kind === 'el' && c.local === 'sequenceFlow')
      at = firstFlow < 0 ? pe.children.length : firstFlow
      // Keep the new step ahead of the flows but after the lanes and the like: just before the first flow.
      if (firstFlow >= 1 && pe.children[firstFlow - 1].kind === 'text') at = firstFlow - 1
    }
    put(pe, at, el, pad)
  })
  const addedFlows: BpmnFlow[] = []
  const createdFlows = new Map<BpmnFlow, XEl>()
  p.flows.forEach((f, i) => {
    if (f.el) return
    const el = buildFlow(f, pad, prefixes, root)
    createdFlows.set(f, el)
    addedFlows.push(f)
    let prev: XEl | undefined
    for (let k = i - 1; k >= 0 && !prev; k--) prev = createdFlows.get(p.flows[k]) ?? (p.flows[k].el ? cloneMap.get(p.flows[k].el!) : undefined)
    let at: number
    if (prev) at = pe.children.indexOf(prev) + 1
    else {
      const lastNode = [...pe.children].reverse().find((c) => c.kind === 'el' && NODE_LOCALS.has(c.local))
      at = lastNode ? pe.children.indexOf(lastNode) + 1 : pe.children.length
    }
    put(pe, at, el, pad)
  })

  // Lanes of the new steps.
  for (const n of addedNodes) if (n.lane) setLane(pe, n.id, n.lane, root, p.id)

  // Order of the steps in the file follows the order in the model.
  const kept = p.nodes.filter((n) => n.el).map((n) => cloneMap.get(n.el!)!)
  const slots = pe.children.map((c, i) => (c.kind === 'el' && kept.includes(c) ? i : -1)).filter((i) => i >= 0)
  const inTree = slots.map((i) => pe.children[i] as XEl)
  if (inTree.some((el, i) => el !== kept[i])) slots.forEach((slot, i) => (pe.children[slot] = kept[i]))

  // incoming / outgoing, for the steps whose flows changed.
  const touched = new Set<string>()
  for (const f of [...addedFlows, ...rewired]) {
    touched.add(f.source)
    touched.add(f.target)
    const base = flowBase(f)
    if (base) {
      touched.add(base.source)
      touched.add(base.target)
    }
  }
  for (const f of p.origFlows) if (!flowEls.has(f.el)) {
    touched.add(f.source)
    touched.add(f.target)
  }
  const usesOut = p.origNodes.some((n) => elements(n.el!, 'outgoing').length > 0)
  const usesIn = p.origNodes.some((n) => elements(n.el!, 'incoming').length > 0)
  for (const n of p.nodes) {
    if (!touched.has(n.id)) continue
    const el = n.el ? cloneMap.get(n.el)! : created.get(n)!
    if (!el) continue
    if (usesIn) syncRefs(el, 'incoming', p.flows.filter((f) => f.target === n.id).map((f) => f.id), root)
    if (usesOut) syncRefs(el, 'outgoing', p.flows.filter((f) => f.source === n.id).map((f) => f.id), root)
  }

  writeDiagram(root, p, removedIds, rewired, addedNodes, addedFlows)
}

const NODE_LOCALS = new Set([
  'task', 'userTask', 'manualTask', 'serviceTask', 'scriptTask', 'businessRuleTask', 'sendTask', 'receiveTask',
  'exclusiveGateway', 'inclusiveGateway', 'complexGateway', 'eventBasedGateway', 'parallelGateway', 'startEvent',
  'endEvent', 'intermediateCatchEvent', 'intermediateThrowEvent', 'boundaryEvent', 'subProcess', 'transaction',
  'adHocSubProcess', 'callActivity',
])

/** Whether nothing was changed since the model was read: the same steps and ways out, in order, with the same fields. */
function untouched(model: BpmnModel): boolean {
  return model.processes.every(
    (p) =>
      p.nodes.length === p.origNodes.length && p.flows.length === p.origFlows.length &&
      p.nodes.every((n, i) => n.el === p.origNodes[i].el && n.base === nodeSnapshot(n)) &&
      p.flows.every((f, i) => f.el === p.origFlows[i].el && f.base === flowSnapshot(f)),
  )
}

/** The model as BPMN XML text. A model nobody changed comes out exactly as the file it was read from. */
export function serializeBpmn(model: BpmnModel): string {
  if (untouched(model)) return model.source
  const { doc, map } = cloneWithMap(model.doc)
  cloneMap = map
  try {
    const root = doc.nodes.find((n): n is XEl => n.kind === 'el')!
    const prefixes = prefixesOf(root)
    for (const p of model.processes) writeProcess(p, root, prefixes)
    return writeXml(doc)
  } finally {
    cloneMap = new Map()
  }
}
