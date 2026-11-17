// Every editable field of every step the host app ships (and of a step added to each file): what the editor sets is what a
// reader finds in the file it writes, and the lines that change are that field's.
import { describe, expect, it } from 'vitest'
import { addStepAfter, updateNode, whyNot } from '../bpmn/edit'
import { SPEC_KEYS, type BpmnNode } from '../bpmn/model'
import { parseBpmn } from '../bpmn/parse'
import { serializeBpmn } from '../bpmn/serialize'
import { decode } from '../bpmn/xml'
import { engineReads, lineDiff, load, meaning, playbooks } from './testkit'

type Edit = (n: BpmnNode) => void

/** One edit per field, each with the words that tell its lines from the others. */
const EDITS: [string, Edit][] = [
  ['name', (n) => (n.name = 'A new name for it')],
  ['instructions', (n) => (n.instructions = 'New instructions, with <angle> & "quotes".\nSecond line of them.')],
  ['decisionCriteria', (n) => (n.decisionCriteria = 'New criteria for the choice.')],
  ['commands', (n) => (n.commands = 'tool.run --flag "{value}"')],
  ['prompts', (n) => (n.prompts = 'Ask: is it so?')],
  ['owner', (n) => (n.refs.owner = 'A new owner')],
  ['roles', (n) => (n.refs.roles = [...n.refs.roles, 'A new role'])],
  ['people', (n) => (n.refs.people = [...n.refs.people, 'A new person'])],
  ['software', (n) => (n.refs.software = [...n.refs.software, 'A new tool'])],
  ['documents', (n) => (n.refs.documents = [...n.refs.documents, 'A new document'])],
  ['controls', (n) => (n.refs.controls = [...n.refs.controls, { reference: 'Z.9', framework: 'A framework' }])],
  ['an engine setting', (n) => (n.porter.extra = 'a value')],
]

/** Whether a changed line is one an edit to `field` may change, for the step `n`. */
function mayChange(line: string, field: string, n: BpmnNode, old: string): boolean {
  const t = decode(line).trim()
  if (t === '') return true
  if (field === 'name' || field === 'an engine setting') return line.includes(`id="${n.id}"`)
  if (['owner', 'roles', 'people', 'software', 'documents', 'controls'].includes(field)) {
    return /^<pa:|^<\/?bpmn:extensionElements/.test(t)
  }
  // The tagged documentation: its own tag lines, or a line of its old words (or, when it is new, anything it holds).
  return t.includes(`id="${n.id}_`) || old.split('\n').some((l) => l.trim() !== '' && t.includes(l.trim())) || /documentation>$/.test(t)
}

const oldWords = (n: BpmnNode, field: string): string =>
  ({ instructions: n.instructions, decisionCriteria: n.decisionCriteria, commands: n.commands, prompts: n.prompts } as Record<string, string>)[field] ?? ''

describe('every field of every step', () => {
  it.each(playbooks)('%s: reads back as the editor set it, and the lines that changed are its own', (name, text) => {
    const { model, pid, process } = load(text)
    for (const n of process.nodes) {
      for (const [field, edit] of EDITS) {
        const edited = updateNode(model, pid, n.id, edit)
        const out = serializeBpmn(edited)
        expect(meaning(parseBpmn(out)), `${name} ${n.id} ${field}`).toEqual(meaning(edited))
        const { removed, added } = lineDiff(text, out)
        const apart = lineDiff(
          text.replace(/(<bpmn:documentation[^>]*>)/g, '$1\n').replace(/(<\/bpmn:documentation>)/g, '\n$1'),
          out.replace(/(<bpmn:documentation[^>]*>)/g, '$1\n').replace(/(<\/bpmn:documentation>)/g, '\n$1'),
        )
        for (const l of apart.removed) expect(mayChange(l, field, n, oldWords(n, field)), `${name} ${n.id} ${field}: removed "${l}"`).toBe(true)
        const after = edited.processes[0].nodes.find((x) => x.id === n.id)!
        for (const l of apart.added) expect(mayChange(l, field, n, oldWords(after, field)), `${name} ${n.id} ${field}: added "${l}"`).toBe(true)
        expect(removed.length + added.length, `${name} ${n.id} ${field}`).toBeGreaterThan(0)
      }
    }
  }, 120_000)

  it.each(playbooks)('%s: a lane change moves the step\'s reference to the lane and nothing else', (name, text) => {
    const { model, pid, process } = load(text)
    const names = process.lanes.map((l) => l.name).filter(Boolean)
    expect(names.length).toBeGreaterThan(1)
    for (const n of process.nodes) {
      const other = names.find((l) => l !== n.lane)!
      const edited = updateNode(model, pid, n.id, (x) => (x.lane = other))
      const out = serializeBpmn(edited)
      expect(meaning(parseBpmn(out)), `${name} ${n.id}`).toEqual(meaning(edited))
      const { removed, added } = lineDiff(text, out)
      for (const l of [...removed, ...added]) expect(l, `${name} ${n.id}`).toContain(`<bpmn:flowNodeRef>${n.id}</bpmn:flowNodeRef>`)
    }
  }, 120_000)
})

describe('a step added to each file', () => {
  it.each(playbooks)('%s: with every field set, reads back as set, and the engine reads it as the view does', (name, text) => {
    const { model, pid, process } = load(text)
    const at = process.nodes.find((n) => n.type !== 'endEvent' && !whyNot(() => addStepAfter(model, pid, n.id)))!
    const { model: grown, id } = addStepAfter(model, pid, at.id, { name: 'A step with everything' })
    const names = process.lanes.map((l) => l.name).filter(Boolean)
    const full = updateNode(grown, pid, id, (n) => {
      for (const key of SPEC_KEYS) n.spec[key] = key === 'rules' ? '1. A rule.\n2. Another rule.' : `Its ${key}.`
      for (const [, edit] of EDITS) edit(n)
      n.lane = names[names.length - 1]
    })
    const out = serializeBpmn(full)
    expect(meaning(parseBpmn(out)), name).toEqual(meaning(full))
    const engine = engineReads(out).get(id)!
    const view = parseBpmn(out).processes[0].nodes.find((n) => n.id === id)!
    expect(engine.lane).toBe(view.lane)
    expect(engine.instructions).toBe(view.instructions)
    // Nothing else in the file moved.
    const { removed } = lineDiff(text, out)
    expect(removed.every((l) => /<bpmn:(outgoing|incoming|sequenceFlow)|targetRef=/.test(l) || l.trim().startsWith('</bpmn:')), name).toBe(true)
  })
})
