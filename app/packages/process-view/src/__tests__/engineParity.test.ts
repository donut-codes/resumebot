// The view and the process engine read the same files. What they share (a step's instructions, its lane, the name of
// the message that starts it) must come out the same in both, for every file the host app ships and for every file an
// editor operation writes. The engine's reading is written again in `testkit.ts` (`engineReads`) from
// `packages/process-engine/src/process_engine/bpmn.py` and `loader.py`, and run on a second XML parser.
import { describe, expect, it } from 'vitest'
import { addStepAfter, addWayOut, moveStep, removeStep, updateFlow, updateNode, whyNot } from '../bpmn/edit'
import { processToGraph } from '../bpmn/graph'
import { isGateway, NODE_TYPES, SPEC_KEYS } from '../bpmn/model'
import { parseBpmn } from '../bpmn/parse'
import { serializeBpmn } from '../bpmn/serialize'
import { engineReads, load, node, playbooks } from './testkit'

const file = (name: string): string => playbooks.find(([n]) => n === name)![1]

const NS =
  'xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:pa="http://processatlas.local/bpmn/extensions" ' +
  'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'

const wrap = (inner: string, before = ''): string =>
  `<?xml version="1.0" encoding="UTF-8"?>\n<bpmn:definitions ${NS} id="D" targetNamespace="http://x">\n${before}  <bpmn:process id="P" name="P">\n${inner}  </bpmn:process>\n</bpmn:definitions>\n`

/** For every step: the view's instructions, lane and message are the engine's. */
function expectSameReading(xml: string, where: string): void {
  const engine = engineReads(xml)
  for (const p of parseBpmn(xml).processes) {
    for (const n of p.nodes) {
      const e = engine.get(n.id)
      expect(e, `${where}: ${n.id} is in the engine's file`).toBeDefined()
      expect(e!.lane, `${where}: ${n.id} lane`).toBe(n.lane || null)
      // What the engine calls instructions is the tagged ones, else the first untagged documentation that is not the
      // specification (it skips one carrying the spec's headings, as the view does): the view's instructions, exactly.
      expect(e!.instructions, `${where}: ${n.id} instructions`).toBe(n.instructions || null)
      if (n.trigger?.kind === 'message') expect(e!.message, `${where}: ${n.id} message`).toBe(n.trigger.detail)
    }
  }
}

describe('the files the host app ships', () => {
  it.each(playbooks)('%s: the view reads each step\'s instructions, lane and message as the engine does', (name, text) => {
    expectSameReading(text, name)
  })

  it.each(playbooks)('%s: the arrows that are a decision\'s default are marked, and no other', (_name, text) => {
    const p = parseBpmn(text).processes[0]
    const { edges } = processToGraph(p)
    const defaults = p.nodes.filter(isGateway).map((n) => n.defaultFlow).filter((id): id is string => !!id)
    expect(defaults.length).toBeGreaterThan(0)
    expect(edges.filter((e) => e.is_default).map((e) => e.id).sort()).toEqual(defaults.filter((id) => edges.some((e) => e.id === id)).sort())
  })

  it.each(playbooks)('%s: no step is called by its BPMN type', (_name, text) => {
    const { steps } = processToGraph(parseBpmn(text).processes[0])
    for (const s of steps) expect(NODE_TYPES.has(s.label ?? ''), s.id).toBe(false)
  })
})

const PLAIN = wrap(
  `    <bpmn:task id="T" name="Call">
      <bpmn:documentation>Call the supplier.</bpmn:documentation>
    </bpmn:task>
    <bpmn:task id="V" name="No words at all"/>
    <bpmn:sequenceFlow id="F1" sourceRef="T" targetRef="V"/>
`,
)

describe('adding a specification to a step with plain instructions', () => {
  it('keeps what the engine tells the agent', () => {
    const { model, pid } = load(PLAIN)
    expect(engineReads(PLAIN).get('T')!.instructions).toBe('Call the supplier.')
    const out = serializeBpmn(updateNode(model, pid, 'T', (n) => (n.spec.actor = 'Dana')))
    expect(engineReads(out).get('T')!.instructions).toBe('Call the supplier.')
    expect(out).toContain('<bpmn:documentation id="T_instructions">Call the supplier.</bpmn:documentation>')
    const back = node(parseBpmn(out).processes[0], 'T')
    expect(back.spec.actor).toBe('Dana')
    expect(back.instructions).toBe('Call the supplier.')
    expectSameReading(out, 'first spec write')
    // And on writing more of it, and on changing the instructions.
    const more = serializeBpmn(updateNode(parseBpmn(out), pid, 'T', (n) => (n.spec.rules = '1. Ring first.')))
    expect(engineReads(more).get('T')!.instructions).toBe('Call the supplier.')
    const words = serializeBpmn(updateNode(parseBpmn(more), pid, 'T', (n) => (n.instructions = 'Call the supplier today.')))
    expect(engineReads(words).get('T')!.instructions).toBe('Call the supplier today.')
    expectSameReading(words, 'then the instructions')
  })

  it('also when the specification and the new words are written together', () => {
    const { model, pid } = load(PLAIN)
    const out = serializeBpmn(
      updateNode(model, pid, 'T', (n) => {
        n.spec.actor = 'Dana'
        n.spec.preconditions = 'A supplier is chosen.'
        n.instructions = 'Call the supplier today.'
      }),
    )
    expect(engineReads(out).get('T')!.instructions).toBe('Call the supplier today.')
    expect(out.match(/id="T_instructions"/g)).toHaveLength(1)
    expectSameReading(out, 'together')
  })

  it('does not touch the words when the specification is written and then taken away', () => {
    const { model, pid } = load(PLAIN)
    const out = serializeBpmn(updateNode(model, pid, 'T', (n) => (n.spec.actor = 'Dana')))
    const gone = serializeBpmn(updateNode(parseBpmn(out), pid, 'T', (n) => (n.spec.actor = '')))
    expect(engineReads(gone).get('T')!.instructions).toBe('Call the supplier.')
    expect(parseBpmn(gone).processes[0].nodes.find((n) => n.id === 'T')!.spec.actor).toBe('')
  })

  it('a step with no words gets its specification as the only documentation', () => {
    const { model, pid } = load(PLAIN)
    const out = serializeBpmn(updateNode(model, pid, 'V', (n) => (n.spec.actor = 'Dana')))
    expect(node(parseBpmn(out).processes[0], 'V').instructions).toBe('')
    expectSameReading(out, 'no words')
  })

  it('a step the old editor left with its specification first and its words untagged is put right when the words change', () => {
    const old = wrap(`    <bpmn:task id="T" name="Call">
      <bpmn:documentation>Actor: Dana.</bpmn:documentation>
      <bpmn:documentation>Call the supplier.</bpmn:documentation>
    </bpmn:task>
`)
    const { model, pid } = load(old)
    // The engine skips the specification even here: it carries the specification's headings.
    expect(engineReads(old).get('T')!.instructions).toBe('Call the supplier.')
    const out = serializeBpmn(updateNode(model, pid, 'T', (n) => (n.instructions = 'Call the supplier today.')))
    expect(engineReads(out).get('T')!.instructions).toBe('Call the supplier today.')
    expect(out).toContain('<bpmn:documentation id="T_instructions">Call the supplier today.</bpmn:documentation>')
    // A change that is not to the words or the specification leaves it be.
    const renamed = serializeBpmn(updateNode(model, pid, 'T', (n) => (n.name = 'Ring')))
    expect(renamed).toBe(old.replace('name="Call"', 'name="Ring"'))
  })

  // The engine skips an untagged documentation that carries the specification's headings (process_engine/bpmn.py
  // SPEC_HEADINGS), as the view does, so clearing the words leaves the step with no instructions, never the spec.
  it('clearing the instructions of a step with a specification does not hand the engine the specification', () => {
    const { model, pid } = load(PLAIN)
    const out = serializeBpmn(updateNode(model, pid, 'T', (n) => (n.spec.actor = 'Dana')))
    const cleared = serializeBpmn(updateNode(parseBpmn(out), pid, 'T', (n) => (n.instructions = '')))
    expect(engineReads(cleared).get('T')!.instructions).toBeNull()
    expect(node(parseBpmn(cleared).processes[0], 'T').spec.actor).toBe('Dana')
  })

  it('does not tag a step whose instructions are already tagged', () => {
    const tagged = wrap(`    <bpmn:task id="T" name="Call">
      <bpmn:documentation>Actor: Dana.</bpmn:documentation>
      <bpmn:documentation id="T_instructions">Call the supplier.</bpmn:documentation>
    </bpmn:task>
`)
    const { model, pid } = load(tagged)
    const out = serializeBpmn(updateNode(model, pid, 'T', (n) => (n.spec.trigger = 'A call is due.')))
    expect(out.match(/id="T_instructions"/g)).toHaveLength(1)
    expect(engineReads(out).get('T')!.instructions).toBe('Call the supplier.')
  })
})

describe.each(['check_one_item.bpmn', 'equip_a_skill.bpmn', 'project.round.bpmn'])('every editor operation on %s', (name) => {
  it('leaves the view and the engine reading the same', () => {
    const text = file(name)
    const { model, pid, process } = load(text)
    const step = process.nodes.find((n) => n.spec.rules && n.instructions && n.type !== 'startEvent')!
    const ops: [string, string][] = []
    ops.push(['spec', serializeBpmn(updateNode(model, pid, step.id, (n) => { n.spec.preconditions = 'Something.'; n.spec.actor = 'Someone' }))])
    ops.push(['instructions', serializeBpmn(updateNode(model, pid, step.id, (n) => (n.instructions = 'New words.')))])
    ops.push(['instructions cleared', serializeBpmn(updateNode(model, pid, step.id, (n) => (n.instructions = '')))])
    ops.push(['spec cleared', serializeBpmn(updateNode(model, pid, step.id, (n) => { for (const k of SPEC_KEYS) n.spec[k] = '' }))])
    ops.push(['lane', serializeBpmn(updateNode(model, pid, step.id, (n) => (n.lane = 'A brand new lane')))])
    ops.push(['lane back', serializeBpmn(updateNode(updateNode(model, pid, step.id, (n) => (n.lane = 'A brand new lane')), pid, step.id, (n) => (n.lane = step.lane)))])
    const at = process.nodes.find((n) => !whyNot(() => addStepAfter(model, pid, n.id)))!
    ops.push(['add step', serializeBpmn(addStepAfter(model, pid, at.id, { name: 'An added step' }).model)])
    const gate = process.nodes.find(isGateway)!
    ops.push(['add way out', serializeBpmn(addWayOut(model, pid, gate.id, { name: 'Another way' }).model)])
    const mid = process.nodes.find((n) => !whyNot(() => removeStep(model, pid, n.id)))
    if (mid) ops.push(['remove step', serializeBpmn(removeStep(model, pid, mid.id))])
    const mover = process.nodes.find((n) => !whyNot(() => moveStep(model, pid, n.id, 'down')))
    if (mover) ops.push(['move step', serializeBpmn(moveStep(model, pid, mover.id, 'down'))])
    const flow = process.flows.find((f) => f.meaning)!
    ops.push(['flow', serializeBpmn(updateFlow(model, pid, flow.id, (f) => (f.meaning = 'Said differently.')))])
    ops.push(['refs', serializeBpmn(updateNode(model, pid, step.id, (n) => (n.refs.software = [...n.refs.software, 'A new tool'])))])
    for (const [op, out] of ops) expectSameReading(out, `${name}: ${op}`)
  })
})

describe('lanes inside lanes', () => {
  const lanes = (outer: string) => wrap(
    `    <bpmn:startEvent id="A" name="Begin"/>
    <bpmn:task id="B" name="Work"/>
    <bpmn:endEvent id="E" name="End"/>
    <bpmn:sequenceFlow id="F1" sourceRef="A" targetRef="B"/>
    <bpmn:sequenceFlow id="F2" sourceRef="B" targetRef="E"/>
`,
  ).replace('  <bpmn:process id="P" name="P">\n', `  <bpmn:process id="P" name="P">\n${outer}`)

  const INNER_AFTER = lanes(`    <bpmn:laneSet id="LS">
      <bpmn:lane id="L1" name="Outer">
        <bpmn:flowNodeRef>A</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>B</bpmn:flowNodeRef>
        <bpmn:childLaneSet id="CLS">
          <bpmn:lane id="L2" name="Inner">
            <bpmn:flowNodeRef>B</bpmn:flowNodeRef>
          </bpmn:lane>
        </bpmn:childLaneSet>
      </bpmn:lane>
    </bpmn:laneSet>
`)

  it('a step listed in a lane and in a lane inside it is in the inner one', () => {
    const p = parseBpmn(INNER_AFTER).processes[0]
    expect(node(p, 'B').lane).toBe('Inner')
    expect(node(p, 'A').lane).toBe('Outer')
    expect(p.lanes.map((l) => l.name)).toEqual(['Outer', 'Inner'])
    expectSameReading(INNER_AFTER, 'inner lane')
  })

  it('reads the order the engine reads: the last lane to list a step is its lane', () => {
    const innerFirst = lanes(`    <bpmn:laneSet id="LS">
      <bpmn:lane id="L1" name="Outer">
        <bpmn:childLaneSet id="CLS">
          <bpmn:lane id="L2" name="Inner">
            <bpmn:flowNodeRef>B</bpmn:flowNodeRef>
          </bpmn:lane>
        </bpmn:childLaneSet>
        <bpmn:flowNodeRef>A</bpmn:flowNodeRef>
        <bpmn:flowNodeRef>B</bpmn:flowNodeRef>
      </bpmn:lane>
    </bpmn:laneSet>
`)
    const twice = lanes(`    <bpmn:laneSet id="LS">
      <bpmn:lane id="L1" name="First"><bpmn:flowNodeRef>A</bpmn:flowNodeRef><bpmn:flowNodeRef>B</bpmn:flowNodeRef></bpmn:lane>
      <bpmn:lane id="L2" name="Second"><bpmn:flowNodeRef>B</bpmn:flowNodeRef></bpmn:lane>
    </bpmn:laneSet>
    <bpmn:laneSet id="LS2">
      <bpmn:lane id="L3" name="Third"><bpmn:flowNodeRef>A</bpmn:flowNodeRef></bpmn:lane>
    </bpmn:laneSet>
`)
    expectSameReading(innerFirst, 'inner first')
    expectSameReading(twice, 'listed twice')
    expect(node(parseBpmn(twice).processes[0], 'B').lane).toBe('Second')
    expect(node(parseBpmn(twice).processes[0], 'A').lane).toBe('Third')
  })

  it('moving a step to a lane inside another writes it where the engine finds it', () => {
    const { model, pid } = load(INNER_AFTER)
    for (const lane of ['Inner', 'Outer', 'A new one', '']) {
      const out = serializeBpmn(updateNode(model, pid, 'A', (n) => (n.lane = lane)))
      expect(engineReads(out).get('A')!.lane).toBe(lane || null)
      expectSameReading(out, `A to ${lane || 'no lane'}`)
    }
    const out = serializeBpmn(updateNode(model, pid, 'B', (n) => (n.lane = 'Outer')))
    expect(engineReads(out).get('B')!.lane).toBe('Outer')
  })
})

describe('the message that starts or waits', () => {
  const messages = wrap(
    `    <bpmn:startEvent id="A" name="Named"><bpmn:messageEventDefinition messageRef="Message_1"/></bpmn:startEvent>
    <bpmn:startEvent id="B" name="Unnamed message"><bpmn:messageEventDefinition messageRef="Message_2"/></bpmn:startEvent>
    <bpmn:startEvent id="C" name="Not declared"><bpmn:messageEventDefinition messageRef="Message_9"/></bpmn:startEvent>
    <bpmn:startEvent id="D" name="No reference"><bpmn:messageEventDefinition/></bpmn:startEvent>
    <bpmn:intermediateCatchEvent id="W" name="Waiting"><bpmn:messageEventDefinition messageRef="Message_1"/></bpmn:intermediateCatchEvent>
`,
    `  <bpmn:message id="Message_1" name="item.to_check"/>\n  <bpmn:message id="Message_2"/>\n`,
  )

  it('is shown by the message\'s name, or its id when it has none, as the engine names it', () => {
    const p = parseBpmn(messages).processes[0]
    expect(node(p, 'A').trigger).toEqual({ kind: 'message', detail: 'item.to_check', subtype: null })
    expect(node(p, 'W').trigger!.detail).toBe('item.to_check')
    expect(node(p, 'B').trigger!.detail).toBe('Message_2')
    expect(node(p, 'C').trigger!.detail).toBe('Message_9')
    expect(node(p, 'D').trigger!.detail).toBeNull()
    expectSameReading(messages, 'messages')
    const graph = processToGraph(p).steps
    expect(graph.find((s) => s.id === 'A')!.event_trigger_detail).toBe('item.to_check')
  })
})

describe('the arrows and names the view draws', () => {
  const decision = wrap(
    `    <bpmn:startEvent id="S" name="Start"/>
    <bpmn:exclusiveGateway id="G" default="F_else"/>
    <bpmn:serviceTask id="C"/>
    <bpmn:task id="D" name="   "/>
    <bpmn:endEvent id="E"/>
    <bpmn:sequenceFlow id="F0" sourceRef="S" targetRef="G"/>
    <bpmn:sequenceFlow id="F_yes" name="Yes" sourceRef="G" targetRef="C"/>
    <bpmn:sequenceFlow id="F_else" name="Otherwise" sourceRef="G" targetRef="D"/>
    <bpmn:sequenceFlow id="F2" sourceRef="C" targetRef="E"/>
    <bpmn:sequenceFlow id="F3" sourceRef="D" targetRef="E"/>
`,
  )

  it('marks the way out a decision takes by default', () => {
    const { edges } = processToGraph(parseBpmn(decision).processes[0])
    expect(edges.filter((e) => e.is_default).map((e) => e.id)).toEqual(['F_else'])
  })

  it('calls a step with no name "Unnamed step", never its BPMN type', () => {
    const { steps } = processToGraph(parseBpmn(decision).processes[0])
    expect(steps.filter((s) => s.label === 'Unnamed step').map((s) => s.id)).toEqual(['G', 'C', 'D', 'E'])
    expect(steps.find((s) => s.id === 'S')!.label).toBe('Start')
  })
})
