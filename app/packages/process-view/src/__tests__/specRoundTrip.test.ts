// The step editor's reading and writing of a step's specification text, over every step the host app ships: what is
// written is what is read back, and an edit to one field changes that field's lines and no others.
import { describe, expect, it } from 'vitest'
import { updateNode } from '../bpmn/edit'
import { SPEC_KEYS, type SpecFields } from '../bpmn/model'
import { docSlots, parseBpmn } from '../bpmn/parse'
import { serializeBpmn } from '../bpmn/serialize'
import { applySpec, readSpec } from '../bpmn/spec'
import { decode, textOf } from '../bpmn/xml'
import { lineDiff, load, meaning, node, playbooks } from './testkit'

/** Every step's specification text in a file, with the step's id. */
function specsOf(text: string): { id: string; doc: string }[] {
  const out: { id: string; doc: string }[] = []
  for (const p of parseBpmn(text).processes) {
    for (const n of p.nodes) {
      const slot = docSlots(n.el!).spec
      if (slot) out.push({ id: n.id, doc: textOf(slot) })
    }
  }
  return out
}

/** What each field is set to in the tests: a line, and (where it may have them) several lines. */
const VALUES: Record<keyof SpecFields, string[]> = {
  actor: ['A new actor.', 'First line of the actor.\nSecond line.'],
  trigger: ['A new trigger.', 'First line of the trigger.\nSecond line.'],
  preconditions: ['A new precondition.', 'First precondition.\nSecond one.'],
  inputs: ['A new input, with `code`, <angle> & "quotes".', 'First input.\nSecond input.'],
  rules: ['1. A new first rule.\n2. A new second rule.', '1. Just one rule, with `code`, <angle> & "quotes".'],
  exceptions: ['A new exception.', 'First exception.\nSecond exception.'],
  firedBy: ['A new thing fires it.'],
  entersAt: ['EI-9.'],
  outputs: ['A new output.', 'First output.\nSecond output.'],
  householdSees: ['A new thing the household sees.'],
  source: ['A new source.'],
}

/** The line a field's label is on. */
const LABEL: Record<keyof SpecFields, RegExp> = {
  actor: /^Actor:/, trigger: /^Trigger:/, outputs: /^Outputs:/, householdSees: /^What the household sees:/,
  source: /^Source:/, rules: /^(Rules:|Processing rules)/, preconditions: /^Preconditions\./, inputs: /^Inputs\./,
  exceptions: /^Exceptions\./, firedBy: /^Fired by:/, entersAt: /^Enters at:/,
}

/** Whether a line that changed belongs to the field that was edited (its label, or a line of its old or new words). */
function belongs(line: string, key: keyof SpecFields, ...texts: string[]): boolean {
  const t = decode(line).trim()
  if (t === '' || LABEL[key].test(t) || /^Rules:/.test(t) || t === 'Processing rules.') return true
  return texts.some((text) => text.split('\n').some((l) => l.trim() !== '' && t.includes(l.trim())))
}

/** The changed lines of an edit, checked to be the field's own. */
function expectOnlyField(before: string, after: string, key: keyof SpecFields, was: string, now: string, where: string) {
  // The first line of a documentation shares its line with the tag that opens it (and the last with the one that
  // closes it), so those tags are put on lines of their own: taking away the first heading changes the next one's line.
  const apart = (t: string) => t.replace(/(<bpmn:documentation[^>]*>)/g, '$1\n').replace(/(<\/bpmn:documentation>)/g, '\n$1')
  const { removed, added } = lineDiff(apart(before), apart(after))
  // The first line of the rules may move off the heading's own line ("Rules: text" becomes "Rules:" and "text").
  const moved = new Set(removed.map((r) => r.replace(/^Rules:\s*/, '')))
  for (const r of removed) expect(belongs(r, key, was), `${where}: removed "${r}"`).toBe(true)
  for (const a of added) expect(belongs(a, key, now) || moved.has(a), `${where}: added "${a}"`).toBe(true)
}

describe('a step specification written and read back', () => {
  it('has a few hundred steps to try', () => {
    expect(playbooks.flatMap(([, text]) => specsOf(text)).length).toBeGreaterThan(150)
  })

  it.each(playbooks)('%s: every field of every step reads back as written', (_name, text) => {
    for (const { id, doc } of specsOf(text)) {
      const was = readSpec(doc)
      expect(applySpec(doc, was, was), `${id} unchanged`).toBe(doc)
      for (const key of SPEC_KEYS) {
        for (const value of [...VALUES[key], '']) {
          if (value === was[key]) continue
          const next = { ...was, [key]: value }
          expect(readSpec(applySpec(doc, was, next)), `${id}: ${key} -> ${JSON.stringify(value)}`).toEqual(next)
        }
      }
      // All of them at once, onto the step and onto nothing.
      const all = Object.fromEntries(SPEC_KEYS.map((k) => [k, VALUES[k][0]])) as unknown as SpecFields
      expect(readSpec(applySpec(doc, was, all)), `${id}: all fields`).toEqual(all)
      const empty = Object.fromEntries(SPEC_KEYS.map((k) => [k, ''])) as unknown as SpecFields
      expect(readSpec(applySpec(doc, was, empty)), `${id}: no fields`).toEqual(empty)
      expect(readSpec(applySpec('', empty, all)), `${id}: all fields from nothing`).toEqual(all)
    }
  })

  it.each(playbooks)('%s: editing one field changes only that field\'s lines', (_name, text) => {
    for (const { id, doc } of specsOf(text)) {
      const was = readSpec(doc)
      for (const key of SPEC_KEYS) {
        for (const value of [VALUES[key][0], '']) {
          if (value === was[key]) continue
          const out = applySpec(doc, was, { ...was, [key]: value })
          expectOnlyField(doc, out, key, was[key], value, `${id}: ${key}`)
        }
      }
    }
  })

  it('adding a rule to a step changes no other line of it', () => {
    for (const [name, text] of playbooks) {
      const { model, pid, process } = load(text)
      for (const n of process.nodes.filter((x) => x.spec.rules)) {
        const out = serializeBpmn(updateNode(model, pid, n.id, (x) => (x.spec.rules = `${x.spec.rules}\n99. An added rule.`)))
        const { removed, added } = lineDiff(text, out)
        expect(removed, `${name} ${n.id}`).toEqual([])
        expect(added, `${name} ${n.id}`).toEqual(['99. An added rule.'])
      }
    }
  })
})

describe('the rules stay rules', () => {
  const plain = 'Actor: A.\nRules:\n1. First.\n2. Second.\nOutputs: O.'

  it('when a labelled part is added above plain numbered rules (Preconditions, Inputs, Fired by, Enters at)', () => {
    const s = readSpec(plain)
    expect(s.rules).toBe('1. First.\n2. Second.')
    for (const key of ['preconditions', 'inputs', 'firedBy', 'entersAt'] as const) {
      const next = { ...s, [key]: 'New text.' }
      const out = applySpec(plain, s, next)
      expect(readSpec(out), key).toEqual(next)
      expect(out, key).toContain('Processing rules.\n1. First.\n2. Second.\nOutputs: O.')
    }
    const four = { ...s, preconditions: 'P.', inputs: 'I.', firedBy: 'F.', entersAt: 'E.' }
    expect(readSpec(applySpec(plain, s, four))).toEqual(four)
  })

  it('when the rules are on the heading\'s own line', () => {
    const inline = 'Actor: A.\nRules: Only one sentence of rules.\nOutputs: O.'
    const s = readSpec(inline)
    const next = { ...s, preconditions: 'There is a plan.' }
    const out = applySpec(inline, s, next)
    expect(readSpec(out)).toEqual(next)
    expect(out).toBe('Actor: A.\nRules:\nPreconditions. There is a plan.\nProcessing rules.\nOnly one sentence of rules.\nOutputs: O.')
  })

  it('when rules are added after a labelled part', () => {
    const doc = 'Rules:\nPreconditions. P.\nExceptions. X.'
    const s = readSpec(doc)
    const next = { ...s, rules: '1. A rule.' }
    const out = applySpec(doc, s, next)
    expect(readSpec(out)).toEqual(next)
    expect(out).toBe('Rules:\nPreconditions. P.\nProcessing rules.\n1. A rule.\nExceptions. X.')
  })

  it('when the rules are edited on a step that marks them with "Processing rules."', () => {
    const doc = 'Actor: A.\nRules:\nPreconditions. P.\nProcessing rules.\n1. First.\n2. Second.\nExceptions. X.\nOutputs: O.'
    const s = readSpec(doc)
    expect(s.rules).toBe('1. First.\n2. Second.')
    const next = { ...s, rules: '1. Changed.\n2. Second.\n3. Added.' }
    const out = applySpec(doc, s, next)
    expect(out).toBe('Actor: A.\nRules:\nPreconditions. P.\nProcessing rules.\n1. Changed.\n2. Second.\n3. Added.\nExceptions. X.\nOutputs: O.')
    expect(readSpec(out)).toEqual(next)
    // A single line, and the line under the marker on its own, stay apart too.
    const one = { ...s, rules: '1. Only.' }
    expect(applySpec(doc, s, one)).toContain('Processing rules.\n1. Only.\nExceptions.')
  })

  it('on the shipped step Hand_out, whose rules sit under a marker', () => {
    const text = playbooks.find(([name]) => name === 'do_one_item.bpmn')![1]
    const { model, pid, process } = load(text)
    const hand = node(process, 'Hand_out')
    const out = serializeBpmn(updateNode(model, pid, 'Hand_out', (n) => (n.spec.rules = n.spec.rules.replace('1. Porter', '1. Porter (changed)'))))
    expect(out).toContain('Processing rules.\n1. Porter (changed)')
    expect(out).not.toMatch(/Processing rules\.1\./)
    const back = node(parseBpmn(out).processes[0], 'Hand_out')
    expect(back.spec.rules).toBe(hand.spec.rules.replace('1. Porter', '1. Porter (changed)'))
    expect(back.spec.preconditions).toBe(hand.spec.preconditions)
    const { removed, added } = lineDiff(text, out)
    expect(removed).toHaveLength(1)
    expect(added).toHaveLength(1)
  })

  it('whole-file: adding Preconditions to a step with plain rules leaves the rules in Rules', () => {
    const text = playbooks.find(([name]) => name === 'check_one_item.bpmn')![1]
    const { model, pid } = load(text)
    const before = node(model.processes[0], 'Make_check')
    expect(before.spec.rules).toMatch(/^1\./)
    const out = serializeBpmn(
      updateNode(model, pid, 'Make_check', (n) => {
        n.spec.preconditions = 'The item was tried.'
        n.spec.inputs = 'The item and the plan.'
      }),
    )
    const after = node(parseBpmn(out).processes[0], 'Make_check')
    expect(after.spec.rules).toBe(before.spec.rules)
    expect(after.spec.preconditions).toBe('The item was tried.')
    expect(after.spec.inputs).toBe('The item and the plan.')
    expect(meaning(parseBpmn(out)).flatMap((p) => p.nodes.filter((n) => n.id !== 'Make_check')))
      .toEqual(meaning(model).flatMap((p) => p.nodes.filter((n) => n.id !== 'Make_check')))
  })
})

describe('an edit to one field of a step in a file changes only that field\'s lines', () => {
  it.each(playbooks)('%s', (name, text) => {
    const { model, pid, process } = load(text)
    for (const n of process.nodes) {
      const slot = docSlots(n.el!).spec
      if (!slot) continue
      for (const key of SPEC_KEYS) {
        const values = n.spec[key] ? [VALUES[key][0], ''] : [VALUES[key][0]]
        for (const value of values) {
          const edited = updateNode(model, pid, n.id, (x) => (x.spec[key] = value))
          const out = serializeBpmn(edited)
          // The file reads back as the model that was edited, step for step...
          expect(meaning(parseBpmn(out)), `${name} ${n.id} ${key}`).toEqual(meaning(edited))
          // ...and the lines that changed are the field's.
          expectOnlyField(text, out, key, n.spec[key], value, `${name} ${n.id} ${key}`)
        }
      }
    }
  }, 120_000)
})
