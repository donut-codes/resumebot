import { describe, expect, it } from 'vitest'
import { addStepAfter, addWayOut, moveStep, removeStep, removeWayOut, updateFlow, updateNode, whyNot } from '../bpmn/edit'
import { processToGraph } from '../bpmn/graph'
import { isGateway } from '../bpmn/model'
import { parseBpmn } from '../bpmn/parse'
import { serializeBpmn } from '../bpmn/serialize'
import { applySpec, formatRuleItems, parseRuleItems, readSpec, renumber } from '../bpmn/spec'
import { parseXml, writeXml, XmlError } from '../bpmn/xml'
import { fixture, lineDiff, load, meaning, node, playbooks } from './testkit'

const checkOne = playbooks.find(([name]) => name === 'check_one_item.bpmn')![1]

describe('the XML reader and writer', () => {
  it('writes back exactly what it read, whatever the spelling', () => {
    const text = `<?xml version="1.0"?>\n<!-- c -->\n<a  x='1'\n   y="&amp;&lt;">\n  <b/>\n  <c>t &gt; u &#233; <![CDATA[<x>]]></c>\n</a>\n`
    expect(writeXml(parseXml(text))).toBe(text)
  })

  it('refuses a document type, so nothing can expand', () => {
    expect(() => parseXml('<!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>')).toThrow(XmlError)
  })

  it('refuses text that is not well formed', () => {
    expect(() => parseXml('<a><b></a>')).toThrow(XmlError)
    expect(() => parseXml('<a x=1/>')).toThrow(XmlError)
    expect(() => parseXml('<a/><b/>')).toThrow(XmlError)
  })
})

describe('every BPMN file in playbooks/', () => {
  it('finds them', () => {
    expect(playbooks.length).toBeGreaterThanOrEqual(9)
  })

  it.each(playbooks)('%s: reads and writes back to the same text, and reads the same again', (_name, text) => {
    const model = parseBpmn(text)
    const out = serializeBpmn(model)
    expect(out).toBe(text)
    // Same meaning after a second read, too.
    expect(meaning(parseBpmn(out))).toEqual(meaning(model))
  })

  it.each(playbooks)('%s: saving with edits that change nothing is a no-op diff', (_name, text) => {
    const { model, pid, process } = load(text)
    let m = model
    for (const n of process.nodes) {
      m = updateNode(m, pid, n.id, (x) => {
        x.name = x.name
        x.spec = { ...x.spec }
        x.instructions = x.instructions
      })
    }
    for (const f of process.flows) m = updateFlow(m, pid, f.id, (x) => (x.condition = x.condition))
    expect(serializeBpmn(m)).toBe(text)
  })

  it.each(playbooks)('%s: reads the steps, the ways out and their words', (_name, text) => {
    const { process } = load(text)
    expect(process.nodes.length).toBeGreaterThan(0)
    expect(process.flows.length).toBeGreaterThan(0)
    for (const f of process.flows) {
      expect(process.nodes.some((n) => n.id === f.source)).toBe(true)
      expect(process.nodes.some((n) => n.id === f.target)).toBe(true)
    }
    // Every decision with a rule says what it means.
    for (const n of process.nodes.filter(isGateway)) {
      for (const f of process.flows.filter((x) => x.source === n.id && x.condition)) expect(f.meaning).not.toBe('')
    }
  })

  it.each(playbooks)('%s: adding a step and taking it out again leaves the file as it was', (_name, text) => {
    const { model, pid, process } = load(text)
    const at = process.nodes.find(
      (n) => !isGateway(n) && n.type !== 'endEvent' && process.flows.filter((f) => f.source === n.id).length === 1 &&
        !whyNot(() => addStepAfter(model, pid, n.id)),
    )
    expect(at).toBeDefined()
    const { model: added, id } = addStepAfter(model, pid, at!.id)
    const withStep = serializeBpmn(added)
    expect(withStep).not.toBe(text)
    expect(withStep).toContain(`id="${id}"`)
    expect(serializeBpmn(removeStep(added, pid, id))).toBe(text)
    // And the file it wrote reads back with the step in place.
    const again = parseBpmn(withStep).processes[0]
    expect(again.nodes.map((n) => n.id)).toContain(id)
    expect(again.flows.length).toBe(process.flows.length + 1)
  })

  it.each(playbooks)('%s: moving a step down and up again leaves the file as it was', (_name, text) => {
    const { model, pid, process } = load(text)
    const at = process.nodes.find((n) => !whyNot(() => moveStep(model, pid, n.id, 'down')))
    if (!at) return // a file with no two plain steps in a row has nothing to move
    const moved = moveStep(model, pid, at.id, 'down')
    expect(serializeBpmn(moved)).not.toBe(text)
    expect(serializeBpmn(moveStep(moved, pid, at.id, 'up'))).toBe(text)
  })

  it.each(playbooks)('%s: adding a way out to a decision and removing it leaves the file as it was', (_name, text) => {
    const { model, pid, process } = load(text)
    const gate = process.nodes.find((n) => isGateway(n) && process.flows.filter((f) => f.source === n.id).length >= 2)
    if (!gate) return
    const { model: added, flowId } = addWayOut(model, pid, gate.id, { name: 'Another way' })
    const out = serializeBpmn(added)
    expect(out).toContain(`id="${flowId}"`)
    expect(serializeBpmn(removeWayOut(added, pid, flowId))).toBe(text)
  })
})

describe('a small edit changes only its own lines', () => {
  it('renaming a step changes the one line that names it', () => {
    const { model, pid } = load(checkOne)
    const out = serializeBpmn(updateNode(model, pid, 'Make_check', (n) => (n.name = 'Make the check again')))
    const { removed, added } = lineDiff(checkOne, out)
    expect(removed).toHaveLength(1)
    expect(added).toHaveLength(1)
    expect(removed[0]).toContain('name="Make the check"')
    expect(added[0]).toContain('name="Make the check again"')
    expect(added[0].replace('Make the check again', 'Make the check')).toBe(removed[0])
  })

  it("changing a step's trigger changes the one line of its documentation", () => {
    const { model, pid } = load(checkOne)
    const out = serializeBpmn(updateNode(model, pid, 'Make_check', (n) => (n.spec.trigger = 'An item was tried, or retried.')))
    const { removed, added } = lineDiff(checkOne, out)
    expect(removed).toEqual(['Trigger: The item was tried.'])
    expect(added).toEqual(['Trigger: An item was tried, or retried.'])
  })

  it('adding a numbered rule adds one line', () => {
    const { model, pid } = load(checkOne)
    const rules = node(model.processes[0], 'Make_check').spec.rules
    const items = parseRuleItems(rules)
    expect(items.map((i) => i.n)).toEqual([1, 2])
    const out = serializeBpmn(
      updateNode(model, pid, 'Make_check', (n) => (n.spec.rules = formatRuleItems([...items, { n: 3, text: 'A third rule.' }]))),
    )
    const { removed, added } = lineDiff(checkOne, out)
    expect(removed).toEqual([])
    expect(added).toEqual(['3. A third rule.'])
  })

  it("changing a way out's label, plain meaning and condition changes those three lines", () => {
    const { model, pid } = load(checkOne)
    const out = serializeBpmn(
      updateFlow(model, pid, 'F_on_plan', (f) => {
        f.name = 'It is on plan'
        f.meaning = 'The item matches the plan.'
        f.condition = 'found.on_plan = true and made.by_app = false'
      }),
    )
    const { removed, added } = lineDiff(checkOne, out)
    expect(removed).toHaveLength(3)
    expect(added).toHaveLength(3)
    expect(added.join('\n')).toContain('name="It is on plan"')
    expect(added.join('\n')).toContain('The item matches the plan.')
    expect(added.join('\n')).toContain('found.on_plan = true and made.by_app = false')
  })

  it("keeps a decision's attributes in the order they were written", () => {
    const { model, pid } = load(checkOne)
    const out = serializeBpmn(updateNode(model, pid, 'Gw_checker', (n) => (n.porter.engine = 'app.x:y')))
    const line = out.split('\n').find((l) => l.includes('id="Gw_checker"'))!
    expect(line).toBe('    <bpmn:exclusiveGateway id="Gw_checker" name="Who checks it?" default="F_specialist" porter:engine="app.x:y">')
  })

  it("changing a step's lane moves it to the lane of that name, or makes a lane", () => {
    const { model, pid } = load(checkOne)
    const moved = serializeBpmn(updateNode(model, pid, 'Make_check', (n) => (n.lane = 'Specialist (session agent)')))
    const m = parseBpmn(moved).processes[0]
    expect(node(m, 'Make_check').lane).toBe('Specialist (session agent)')
    const { removed, added } = lineDiff(checkOne, moved)
    expect(removed).toEqual(['        <bpmn:flowNodeRef>Make_check</bpmn:flowNodeRef>'])
    expect(added).toEqual(['        <bpmn:flowNodeRef>Make_check</bpmn:flowNodeRef>'])
    const fresh = parseBpmn(serializeBpmn(updateNode(model, pid, 'Make_check', (n) => (n.lane = 'The household')))).processes[0]
    expect(node(fresh, 'Make_check').lane).toBe('The household')
    expect(fresh.lanes.map((l) => l.name)).toContain('The household')
  })
})

describe('the list operations', () => {
  it('adds a step after one: what led out now leads to it, and it leads on', () => {
    const { model, pid } = load(checkOne)
    const { model: next, id } = addStepAfter(model, pid, 'Make_check')
    const p = parseBpmn(serializeBpmn(next)).processes[0]
    expect(id).toBe('New_step')
    expect(node(p, id).name).toBe('New step')
    expect(node(p, id).lane).toBe(node(p, 'Make_check').lane)
    expect(p.flows.filter((f) => f.source === 'Make_check').map((f) => f.target)).toEqual([id])
    expect(p.flows.filter((f) => f.source === id).map((f) => f.target)).toEqual(['Gw_checker'])
    // The flow that was there keeps its id.
    expect(p.flows.find((f) => f.id === 'S_Make_check__Gw_checker')!.target).toBe(id)
    // The step sits in the lane's list, after the step it follows in the file.
    const text = serializeBpmn(next)
    expect(text.indexOf('id="Make_check"')).toBeLessThan(text.indexOf(`id="${id}"`))
    expect(text.indexOf(`id="${id}"`)).toBeLessThan(text.indexOf('id="Gw_checker"'))
    expect(text).toMatch(new RegExp(`<bpmn:flowNodeRef>${id}</bpmn:flowNodeRef>`))
  })

  it('gives a new step the role of the step it follows, so the engine can run it', () => {
    const { model, pid } = load(checkOne)
    const { model: next, id } = addStepAfter(model, pid, 'Specialist_checks')
    expect(serializeBpmn(next)).toContain(`<bpmn:task id="${id}" name="New step" porter:role="specialist"`)
  })

  it('edits an engine setting in place, and adds one a step did not have', () => {
    const { model, pid } = load(checkOne)
    const out = serializeBpmn(updateNode(model, pid, 'Specialist_checks', (n) => (n.porter.role = 'doer')))
    const { removed, added } = lineDiff(checkOne, out)
    expect(removed).toHaveLength(1)
    expect(added).toEqual([removed[0].replace('porter:role="specialist"', 'porter:role="doer"')])
    const more = serializeBpmn(updateNode(model, pid, 'Make_check', (n) => (n.porter.template = 'x.response')))
    expect(more).toContain('porter:engine="app.planengine.plan_check:_create_plan_checks" porter:template="x.response"')
    const cleared = serializeBpmn(updateNode(model, pid, 'Specialist_checks', (n) => delete n.porter.role))
    expect(cleared).not.toContain('porter:role="specialist"')
  })

  it('numbers a second new step apart from the first', () => {
    const { model, pid } = load(checkOne)
    const one = addStepAfter(model, pid, 'Make_check')
    const two = addStepAfter(one.model, pid, 'Make_check')
    expect(two.id).toBe('New_step_2')
  })

  it('takes a step out by joining its one way in to its one way out', () => {
    const { model, pid } = load(checkOne)
    const out = serializeBpmn(removeStep(model, pid, 'Make_check'))
    const p = parseBpmn(out).processes[0]
    expect(p.nodes.map((n) => n.id)).not.toContain('Make_check')
    expect(p.flows.filter((f) => f.source === 'Start_check').map((f) => f.target)).toEqual(['Gw_checker'])
    expect(out).not.toContain('id="Make_check"')
  })

  it('swaps a step with the next one on its path', () => {
    const { model, pid } = load(checkOne)
    // Start_check -> Make_check -> Gw_checker; a decision is not moved past.
    expect(whyNot(() => moveStep(model, pid, 'Make_check', 'down'))).toMatch(/plain step/)
    const p0 = model.processes[0]
    const mover = p0.nodes.find((n) => !whyNot(() => moveStep(model, pid, n.id, 'down')))
    expect(mover).toBeUndefined()
  })

  it('moves a step down and up along a path of plain steps', () => {
    const text = fixture('process-wiki-export.bpmn')
    const { model, pid } = load(text)
    // Step_review is plain, but the decision after it is not: there is nothing to swap with.
    expect(whyNot(() => moveStep(model, pid, 'Step_review', 'down'))).not.toBeNull()
    // Step_second -> Step_pay -> Step_end: Step_pay is plain but ends in an end event, which is not plain either.
    const { model: grown, id } = addStepAfter(model, pid, 'Step_second', { name: 'Check the bank details' })
    const down = moveStep(grown, pid, 'Step_second', 'down')
    const p = parseBpmn(serializeBpmn(down)).processes[0]
    expect(p.flows.filter((f) => f.source === 'Step_gate' && f.id === 'Flow_big').map((f) => f.target)).toEqual([id])
    expect(p.flows.filter((f) => f.source === id).map((f) => f.target)).toEqual(['Step_second'])
    expect(p.flows.filter((f) => f.source === 'Step_second').map((f) => f.target)).toEqual(['Step_pay'])
    const order = p.nodes.map((n) => n.id)
    expect(order.indexOf(id)).toBeLessThan(order.indexOf('Step_second'))
  })

  it('adds a way out of a decision, to a new end or to a step, and takes one away', () => {
    const { model, pid } = load(checkOne)
    const { model: next, flowId } = addWayOut(model, pid, 'Gw_found', { name: 'Needs a second look' })
    const p = parseBpmn(serializeBpmn(next)).processes[0]
    const f = p.flows.find((x) => x.id === flowId)!
    expect(f.name).toBe('Needs a second look')
    expect(f.source).toBe('Gw_found')
    expect(node(p, f.target).type).toBe('endEvent')
    // To an existing step instead.
    const { model: to } = addWayOut(model, pid, 'Gw_found', { name: 'Check again', targetId: 'Make_check' })
    expect(parseBpmn(serializeBpmn(to)).processes[0].flows.some((x) => x.name === 'Check again' && x.target === 'Make_check')).toBe(true)
    // A decision keeps two ways out.
    expect(whyNot(() => removeWayOut(model, pid, 'F_on_plan'))).toMatch(/at least two/)
    expect(whyNot(() => removeWayOut(next, pid, flowId))).toBeNull()
  })

  it('says why it cannot do what it cannot', () => {
    const { model, pid } = load(checkOne)
    expect(whyNot(() => removeStep(model, pid, 'Gw_checker'))).toMatch(/one way in and one way out/)
    expect(whyNot(() => addStepAfter(model, pid, 'End_on_plan'))).toMatch(/Nothing comes after an end/)
    expect(whyNot(() => addStepAfter(model, pid, 'Gw_found'))).toMatch(/more than one place/)
    expect(whyNot(() => addWayOut(model, pid, 'Make_check'))).toMatch(/Only a decision/)
  })

  it('leaves the model it was given alone', () => {
    const { model, pid } = load(checkOne)
    const before = JSON.stringify(meaning(model))
    addStepAfter(model, pid, 'Make_check')
    removeStep(model, pid, 'Make_check')
    updateNode(model, pid, 'Make_check', (n) => (n.name = 'x'))
    expect(JSON.stringify(meaning(model))).toBe(before)
  })
})

describe('the specification text', () => {
  const doc = 'Actor: The app.\nTrigger: When asked.\nRules:\nPreconditions. A plan exists.\nInputs. The plan.\nProcessing rules.\n1. First.\n2. Second.\nExceptions. None.\nOutputs: A check.\nWhat the household sees: Nothing.\nSource: PR-11.'

  it('reads each heading and each part of the rules', () => {
    const s = readSpec(doc)
    expect(s.actor).toBe('The app.')
    expect(s.trigger).toBe('When asked.')
    expect(s.preconditions).toBe('A plan exists.')
    expect(s.inputs).toBe('The plan.')
    expect(s.rules).toBe('1. First.\n2. Second.')
    expect(s.exceptions).toBe('None.')
    expect(s.outputs).toBe('A check.')
    expect(s.householdSees).toBe('Nothing.')
    expect(s.source).toBe('PR-11.')
  })

  it('writes back nothing when nothing changed, and one field when one did', () => {
    const s = readSpec(doc)
    expect(applySpec(doc, s, s)).toBe(doc)
    const out = applySpec(doc, s, { ...s, inputs: 'The plan, and its items.' })
    expect(lineDiff(doc, out)).toEqual({ removed: ['Inputs. The plan.'], added: ['Inputs. The plan, and its items.'] })
  })

  it('adds a heading in its place and drops one that was cleared', () => {
    const s = readSpec('Actor: A.\nSource: S.')
    const added = applySpec('Actor: A.\nSource: S.', s, { ...s, trigger: 'Always.', outputs: 'Out.' })
    expect(added).toBe('Actor: A.\nTrigger: Always.\nOutputs: Out.\nSource: S.')
    expect(applySpec('Actor: A.\nSource: S.', s, { ...s, source: '' })).toBe('Actor: A.\n')
  })

  it('numbers rules as a list', () => {
    const items = parseRuleItems('Lead.\n1. one\ncontinued\n2. two')
    expect(items).toEqual([{ n: null, text: 'Lead.' }, { n: 1, text: 'one\ncontinued' }, { n: 2, text: 'two' }])
    expect(formatRuleItems(renumber([items[0], items[2]]))).toBe('Lead.\n1. two')
  })
})

describe("a file the upstream project wrote, with its tagged documentation and its extension elements", () => {
  const text = fixture('process-wiki-export.bpmn')

  it('reads and writes back unchanged', () => {
    expect(serializeBpmn(parseBpmn(text))).toBe(text)
  })

  it('reads instructions, decision criteria, commands and prompts from the tagged documentation', () => {
    const p = parseBpmn(text).processes[0]
    const review = node(p, 'Step_review')
    expect(review.instructions).toBe("Check the amount against the purchase order & the supplier's terms (net 30).")
    expect(review.commands).toBe('erp.find_po --invoice {id} --supplier "{name}"')
    expect(review.prompts).toBe('Ask the agent: does this invoice match PO {po}? Answer yes, no or unsure.')
    expect(node(p, 'Step_gate').decisionCriteria).toMatch(/^Over \$5,000/)
    expect(node(p, 'Step_pay').commands).toBe('erp.pay --invoice {id} --when "<due date>"')
  })

  it('reads documentation with no tag as the instructions', () => {
    const p = parseBpmn(text).processes[0]
    expect(node(p, 'Step_second').instructions).toMatch(/^Ask the Finance Manager/)
  })

  it("reads who owns a step and what it uses from the extension elements", () => {
    const review = node(parseBpmn(text).processes[0], 'Step_review')
    expect(review.refs).toEqual({
      owner: 'Finance', roles: ['AP Clerk'], people: ['Dana Reyes'], software: ['SAP', 'Outlook'],
      documents: ['Purchase order'],
      controls: [{ reference: 'A.9.4', framework: 'ISO 27001' }, { reference: 'CC6.1', framework: '' }],
    })
  })

  it('shows them in the step list', () => {
    const { steps } = processToGraph(parseBpmn(text).processes[0])
    const review = steps.find((s) => s.id === 'Step_review')!
    expect(review.commands).toContain('erp.find_po')
    expect(review.prompts).toContain('Ask the agent')
    expect(review.performer).toBe('AP Clerk')
    expect(steps.find((s) => s.id === 'Step_gate')!.decision_criteria).toContain('second approver')
  })

  it('edits each of them and changes only that line', () => {
    const { model, pid } = load(text)
    const edits: [string, (n: ReturnType<typeof node>) => void, string, string][] = [
      ['commands', (n) => (n.commands = 'erp.find_po --invoice {id}'), 'erp.find_po --invoice {id} --supplier "{name}"', 'erp.find_po --invoice {id}'],
      ['prompts', (n) => (n.prompts = 'Ask: does it match?'), 'Ask the agent: does this invoice match PO {po}?', 'Ask: does it match?'],
      ['instructions', (n) => (n.instructions = 'Check it.'), 'Check the amount against', 'Check it.'],
      ['owner', (n) => (n.refs.owner = 'Accounts'), '<pa:owner name="Finance"/>', '<pa:owner name="Accounts"/>'],
    ]
    for (const [, fn, was, now] of edits) {
      const out = serializeBpmn(updateNode(model, pid, 'Step_review', fn))
      const { removed, added } = lineDiff(text, out)
      expect(removed).toHaveLength(1)
      expect(removed[0]).toContain(was)
      expect(added).toHaveLength(1)
      expect(added[0]).toContain(now)
    }
    const out = serializeBpmn(updateNode(model, pid, 'Step_gate', (n) => (n.decisionCriteria = 'Over $10,000 needs a second approver.')))
    expect(lineDiff(text, out).added[0]).toContain('Over $10,000')
  })

  it('adds a tagged field a step did not have, and the extension elements, in the places the upstream project writes them', () => {
    const { model, pid } = load(text)
    const out = serializeBpmn(
      updateNode(model, pid, 'Step_start', (n) => {
        n.commands = 'mail.fetch --inbox ap'
        n.refs.software = ['Outlook']
        n.refs.controls = [{ reference: 'A.8', framework: 'ISO 27001' }]
      }),
    )
    const m = parseBpmn(out).processes[0]
    expect(node(m, 'Step_start').commands).toBe('mail.fetch --inbox ap')
    expect(node(m, 'Step_start').refs.software).toEqual(['Outlook'])
    expect(out).toContain('<bpmn:documentation id="Step_start_commands">mail.fetch --inbox ap</bpmn:documentation>')
    expect(out).toContain('<pa:software name="Outlook"/>')
    expect(out).toContain('<pa:control reference="A.8" framework="ISO 27001"/>')
    // Everything else is as it was.
    expect(lineDiff(text, out).removed).toEqual([])
    // Clearing them takes them out again.
    const back = serializeBpmn(
      updateNode(parseBpmn(out), pid, 'Step_start', (n) => {
        n.commands = ''
        n.refs.software = []
        n.refs.controls = []
      }),
    )
    expect(back).toBe(text)
  })

  it('writes the extension namespace into a file that did not declare it', () => {
    const plain = checkOne
    const { model, pid } = load(plain)
    const out = serializeBpmn(updateNode(model, pid, 'Make_check', (n) => (n.refs.owner = 'The app')))
    expect(out).toMatch(/xmlns:pa="http:\/\/processatlas\.local\/bpmn\/extensions"/)
    expect(node(parseBpmn(out).processes[0], 'Make_check').refs.owner).toBe('The app')
  })

  it('keeps and tidies the diagram as steps come and go', () => {
    const { model, pid } = load(text)
    const { model: grown, id } = addStepAfter(model, pid, 'Step_pay', { name: 'Tell the supplier' })
    const out = serializeBpmn(grown)
    expect(out).toContain(`bpmnElement="${id}"`)
    expect(out).not.toContain('bpmndi:BPMNEdge id="undefined')
    const gone = serializeBpmn(removeStep(grown, pid, id))
    expect(gone).toBe(text)
    const removed = serializeBpmn(removeStep(model, pid, 'Step_second'))
    expect(removed).not.toContain('bpmnElement="Step_second"')
    expect(removed).toContain('bpmnElement="Step_end"')
  })
})

describe('the steps and lines the view draws', () => {
  it('draws every step once and every line but a step looping to itself', () => {
    const p = parseBpmn(checkOne).processes[0]
    const { steps, edges } = processToGraph(p)
    expect(steps.map((s) => s.id)).toEqual(p.nodes.map((n) => n.id))
    expect(edges).toHaveLength(p.flows.length)
    expect(steps.find((s) => s.id === 'Gw_checker')!.step_type).toBe('gateway_exclusive')
    expect(steps.find((s) => s.id === 'Start_check')!.step_type).toBe('event_start')
    expect(steps.find((s) => s.id === 'Make_check')!.performer).toBe('Porter: checks and report')
  })

  it('gives a process with no start event one, so it still reads from the top', () => {
    const { steps, edges } = processToGraph(parseBpmn(fixture('process-wiki-export.bpmn').replace(/<bpmn:startEvent[\s\S]*?<\/bpmn:startEvent>/, '').replace('<bpmn:sequenceFlow id="Flow_1" sourceRef="Step_start" targetRef="Step_review"/>', '')).processes[0])
    expect(steps[0].id).toBe('start')
    expect(edges[0].source_step_id).toBe('start')
  })
})
