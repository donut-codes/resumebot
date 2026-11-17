// What an edit must not do to the file: rewrite lines it did not edit, lose what it does not know, indent new lines
// wrong, mix line endings, write what XML forbids, or write an id that is not a name.
import { describe, expect, it } from 'vitest'
import { addStepAfter, addWayOut, removeStep, updateFlow, updateNode, whyNot } from '../bpmn/edit'
import { BpmnParseError, type SpecFields } from '../bpmn/model'
import { parseBpmn } from '../bpmn/parse'
import { serializeBpmn } from '../bpmn/serialize'
import { parseXml, writeXml, XmlError } from '../bpmn/xml'
import { crlf, engineReads, fixture, lineDiff, load, meaning, node, playbooks } from './testkit'

const file = (name: string): string => playbooks.find(([n]) => n === name)![1]
const equip = file('equip_a_skill.bpmn')
const checkOne = file('check_one_item.bpmn')

const NS =
  'xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:pa="http://processatlas.local/bpmn/extensions" ' +
  'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'

/** A small file with a step that carries the upstream project's references, and elements a reader of them does not know. */
const WITH_REFS = `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions ${NS} id="D" targetNamespace="http://x">
  <bpmn:process id="P" name="P">
    <bpmn:startEvent id="S" name="Start"/>
    <bpmn:task id="T" name="Do it">
      <bpmn:extensionElements>
        <!-- process-wiki: keep this -->
        <pa:owner name="Finance" since="2024"/>
        <pa:role name="Clerk"/>
        <pa:team name="Back office" lead="Dana"/>
        <pa:software name="SAP" url="https://sap.example"/>
        <pa:software name="Outlook"/>
        <pa:control reference="A.9.4" framework="ISO 27001" owner="X"/>
        <pa:future-thing name="unknown to this reader" a="1"/>
      </bpmn:extensionElements>
    </bpmn:task>
    <bpmn:task id="U" name="Only one reference">
      <bpmn:extensionElements>
        <pa:role name="Clerk"/>
      </bpmn:extensionElements>
    </bpmn:task>
    <bpmn:endEvent id="E" name="End"/>
    <bpmn:sequenceFlow id="F1" sourceRef="S" targetRef="T"/>
    <bpmn:sequenceFlow id="F2" sourceRef="T" targetRef="U"/>
    <bpmn:sequenceFlow id="F3" sourceRef="U" targetRef="E"/>
  </bpmn:process>
</bpmn:definitions>
`

/** The same shape of file with the diagram's namespaces declared on the diagram, not on the root. */
const LOCAL_DI = `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D" targetNamespace="http://x">
  <bpmn:process id="P" name="P">
    <bpmn:startEvent id="S" name="Start"/>
    <bpmn:task id="T" name="Do it"/>
    <bpmn:endEvent id="E" name="End"/>
    <bpmn:sequenceFlow id="F1" sourceRef="S" targetRef="T"/>
    <bpmn:sequenceFlow id="F2" sourceRef="T" targetRef="E"/>
  </bpmn:process>
  <bpmndi:BPMNDiagram id="Dg" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" xmlns:di="http://www.omg.org/spec/DD/20100524/DI">
    <bpmndi:BPMNPlane id="Pl" bpmnElement="P">
      <bpmndi:BPMNShape id="S_di" bpmnElement="S">
        <dc:Bounds x="0" y="0" width="36" height="36"/>
      </bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="T_di" bpmnElement="T">
        <dc:Bounds x="100" y="0" width="100" height="80"/>
      </bpmndi:BPMNShape>
      <bpmndi:BPMNShape id="E_di" bpmnElement="E">
        <dc:Bounds x="250" y="0" width="36" height="36"/>
      </bpmndi:BPMNShape>
      <bpmndi:BPMNEdge id="F1_di" bpmnElement="F1">
        <di:waypoint x="36" y="18"/>
        <di:waypoint x="100" y="40"/>
      </bpmndi:BPMNEdge>
      <bpmndi:BPMNEdge id="F2_di" bpmnElement="F2">
        <di:waypoint x="200" y="40"/>
        <di:waypoint x="250" y="18"/>
      </bpmndi:BPMNEdge>
    </bpmndi:BPMNPlane>
  </bpmndi:BPMNDiagram>
</bpmn:definitions>
`

const indent = (line: string): number => /^ */.exec(line)![0].length

describe('one edit changes only the lines it edits', () => {
  it('hands back the file itself when nothing changed, however the values were rebuilt', () => {
    const { model, pid, process } = load(equip)
    let m = model
    for (const n of process.nodes) {
      m = updateNode(m, pid, n.id, (x) => {
        x.spec = Object.fromEntries(Object.entries(x.spec).reverse()) as unknown as SpecFields
        x.porter = Object.fromEntries(Object.entries(x.porter).reverse())
        x.refs = { ...x.refs, controls: x.refs.controls.map((c) => ({ framework: c.framework, reference: c.reference })) }
      })
    }
    expect(serializeBpmn(m)).toBe(equip)
    expect(serializeBpmn(model)).toBe(equip)
  })

  it('does not rewrite a step whose value is the same, even when another one is edited', () => {
    const { model, pid, process } = load(equip)
    expect(equip).toContain('Settings > Skills')
    let m = model
    for (const n of process.nodes) {
      m = updateNode(m, pid, n.id, (x) => {
        x.spec = Object.fromEntries(Object.entries(x.spec).reverse()) as unknown as SpecFields
      })
    }
    m = updateNode(m, pid, 'Teacher_works', (x) => (x.name = `${x.name} (renamed)`))
    const { removed, added } = lineDiff(equip, serializeBpmn(m))
    expect(removed).toHaveLength(1)
    expect(added).toHaveLength(1)
    expect(removed[0]).toContain('id="Teacher_works"')
  })

  it('keeps the spelling of the lines it did not touch (&gt;, &quot;, &#8212;) when it adds a rule', () => {
    const { model, pid } = load(equip)
    const was = node(model.processes[0], 'Teacher_works')
    const out = serializeBpmn(updateNode(model, pid, 'Teacher_works', (n) => (n.spec.rules = `${was.spec.rules}\n99. A new rule.`)))
    const { removed, added } = lineDiff(equip, out)
    expect(removed).toEqual([])
    expect(added).toEqual(['99. A new rule.'])
    expect(out).toContain('Settings > Skills')
    expect(out).not.toContain('Settings &gt; Skills')
  })

  it('keeps entities in the lines around an edited one', () => {
    const text = `<bpmn:definitions ${NS} id="D"><bpmn:process id="P"><bpmn:task id="T" name="x">
<bpmn:documentation id="T_instructions">Open Settings &gt; Skills.
Say &quot;yes&quot; &#8212; then stop.
Last line.</bpmn:documentation></bpmn:task></bpmn:process></bpmn:definitions>`
    const { model, pid } = load(text)
    const out = serializeBpmn(updateNode(model, pid, 'T', (n) => (n.instructions = `${n.instructions.replace('Last line.', 'The last line.')}`)))
    expect(out).toBe(text.replace('Last line.', 'The last line.'))
  })

  it('writes the same value again as the same text', () => {
    const { model, pid } = load(equip)
    const was = node(model.processes[0], 'Teacher_works')
    expect(serializeBpmn(updateNode(model, pid, 'Teacher_works', (n) => (n.instructions = was.instructions)))).toBe(equip)
  })
})

describe('references from the upstream project', () => {
  const read = (xml: string, id = 'T') => node(parseBpmn(xml).processes[0], id).refs

  it('adding one leaves every other element, attribute and comment', () => {
    const { model, pid } = load(WITH_REFS)
    const out = serializeBpmn(updateNode(model, pid, 'T', (n) => (n.refs.software = [...n.refs.software, 'Excel'])))
    const { removed, added } = lineDiff(WITH_REFS, out)
    expect(removed).toEqual([])
    expect(added).toEqual(['        <pa:software name="Excel"/>'])
    expect(read(out).software).toEqual(['SAP', 'Outlook', 'Excel'])
    // And it went after the software, not at the end.
    expect(out.indexOf('name="Excel"')).toBeLessThan(out.indexOf('<pa:control'))
  })

  it('taking one away takes that element and no other', () => {
    const { model, pid } = load(WITH_REFS)
    const out = serializeBpmn(updateNode(model, pid, 'T', (n) => (n.refs.software = ['SAP'])))
    const { removed, added } = lineDiff(WITH_REFS, out)
    expect(removed).toEqual(['        <pa:software name="Outlook"/>'])
    expect(added).toEqual([])
  })

  it('renaming one changes that element in place and keeps the rest of it', () => {
    const { model, pid } = load(WITH_REFS)
    const out = serializeBpmn(
      updateNode(model, pid, 'T', (n) => {
        n.refs.software = ['SAP S/4', 'Outlook']
        n.refs.owner = 'Accounts'
        n.refs.controls = [{ reference: 'A.9.5', framework: 'ISO 27001' }]
      }),
    )
    expect(out).toContain('<pa:software name="SAP S/4" url="https://sap.example"/>')
    expect(out).toContain('<pa:owner name="Accounts" since="2024"/>')
    expect(out).toContain('<pa:control reference="A.9.5" framework="ISO 27001" owner="X"/>')
    expect(lineDiff(WITH_REFS, out).removed).toHaveLength(3)
    expect(lineDiff(WITH_REFS, out).added).toHaveLength(3)
  })

  it('a new kind goes in after the kinds before it, and an unknown element is not moved', () => {
    const { model, pid } = load(WITH_REFS)
    const out = serializeBpmn(updateNode(model, pid, 'T', (n) => (n.refs.people = ['Dana Reyes'])))
    const { removed, added } = lineDiff(WITH_REFS, out)
    expect(removed).toEqual([])
    expect(added).toEqual(['        <pa:person name="Dana Reyes"/>'])
    expect(out.indexOf('<pa:role')).toBeLessThan(out.indexOf('<pa:person'))
    expect(out.indexOf('<pa:person')).toBeLessThan(out.indexOf('<pa:team'))
  })

  it('clearing every reference leaves what is not one, and the element that holds it', () => {
    const { model, pid } = load(WITH_REFS)
    const out = serializeBpmn(updateNode(model, pid, 'T', (n) => (n.refs = { owner: '', roles: [], people: [], software: [], documents: [], controls: [] })))
    expect(out).toContain('<!-- process-wiki: keep this -->')
    expect(out).toContain('<pa:team name="Back office" lead="Dana"/>')
    expect(out).toContain('<pa:future-thing name="unknown to this reader" a="1"/>')
    expect(read(out)).toEqual({ owner: '', roles: [], people: [], software: [], documents: [], controls: [] })
  })

  it('clearing the only one takes the empty element away too', () => {
    const { model, pid } = load(WITH_REFS)
    const out = serializeBpmn(updateNode(model, pid, 'U', (n) => (n.refs.roles = [])))
    const { removed, added } = lineDiff(WITH_REFS, out)
    expect(added).toEqual([])
    expect(removed).toHaveLength(3)
    expect(out).not.toContain('Only one reference">\n      <bpmn:extensionElements>')
  })

  it('reordering what is there is a reorder, not a loss', () => {
    const { model, pid } = load(WITH_REFS)
    const out = serializeBpmn(updateNode(model, pid, 'T', (n) => (n.refs.software = ['Outlook', 'SAP'])))
    expect(read(out).software).toEqual(['Outlook', 'SAP'])
    expect(out).toContain('url="https://sap.example"')
    expect(out).toContain('<pa:future-thing')
  })

  it('does not touch a kind that did not change', () => {
    const { model, pid } = load(WITH_REFS)
    const out = serializeBpmn(updateNode(model, pid, 'T', (n) => (n.refs.roles = ['Clerk', 'Approver'])))
    expect(lineDiff(WITH_REFS, out)).toEqual({ removed: [], added: ['        <pa:role name="Approver"/>'] })
  })
})

describe('new steps are indented like their neighbours', () => {
  it.each(playbooks)('%s: a step, its lines and its ways in and out', (_name, text) => {
    const { model, pid, process } = load(text)
    const at = process.nodes.find(
      (n) => n.type !== 'endEvent' && process.flows.filter((f) => f.source === n.id).length === 1 && !whyNot(() => addStepAfter(model, pid, n.id)),
    )!
    const { model: grown, id } = addStepAfter(model, pid, at.id, { name: 'An added step' })
    const lines = serializeBpmn(grown).split('\n')
    const own = lines.findIndex((l) => l.includes(`id="${id}"`))
    const neighbour = lines.findIndex((l) => l.includes(`id="${at.id}"`) && !l.includes('flowNodeRef') && !l.includes('Ref='))
    expect(indent(lines[own])).toBe(indent(lines[neighbour]))
    if (!lines[own].trimEnd().endsWith('/>')) {
      let end = own + 1
      while (end < lines.length && indent(lines[end]) > indent(lines[own])) {
        expect(indent(lines[end]), `inside the step: ${lines[end]}`).toBe(indent(lines[own]) + 2)
        end++
      }
      expect(lines[end].trim()).toMatch(/^<\/bpmn:/)
      expect(indent(lines[end])).toBe(indent(lines[own]))
    }
    // The way out it was given goes where the other flows are.
    const flow = lines.find((l) => l.includes('<bpmn:sequenceFlow') && l.includes(`sourceRef="${id}"`))!
    const otherFlow = lines.find((l) => l.includes('<bpmn:sequenceFlow') && !l.includes(id))!
    expect(indent(flow)).toBe(indent(otherFlow))
  })

  it('also in a file with a diagram: the new shape and the new lines are where the others are', () => {
    const text = fixture('process-wiki-export.bpmn')
    const { model, pid } = load(text)
    const { model: grown, id } = addStepAfter(model, pid, 'Step_pay', { name: 'Tell the supplier' })
    const lines = serializeBpmn(grown).split('\n')
    const shape = lines.findIndex((l) => l.includes('<bpmndi:BPMNShape') && l.includes(`bpmnElement="${id}"`))
    const other = lines.findIndex((l) => l.includes('<bpmndi:BPMNShape') && l.includes('bpmnElement="Step_end"'))
    expect(indent(lines[shape])).toBe(indent(lines[other]))
    expect(indent(lines[shape + 1])).toBe(indent(lines[other]) + 2)
    expect(lines[shape + 2].trim()).toBe('</bpmndi:BPMNShape>')
    expect(indent(lines[shape + 2])).toBe(indent(lines[other]))
    const edges = lines.map((l, i) => (l.includes('<bpmndi:BPMNEdge') ? i : -1)).filter((i) => i >= 0)
    expect(edges.length).toBe(2)
    for (const e of edges) {
      expect(indent(lines[e])).toBe(indent(lines[other]))
      expect(indent(lines[e + 1])).toBe(indent(lines[other]) + 2)
      expect(lines[e + 3].trim()).toBe('</bpmndi:BPMNEdge>')
      expect(indent(lines[e + 3])).toBe(indent(lines[other]))
    }
  })

  it('a step added with references is indented all the way down', () => {
    const { model, pid } = load(WITH_REFS)
    const { model: grown, id } = addStepAfter(model, pid, 'U', { name: 'Check' })
    const out = serializeBpmn(
      updateNode(grown, pid, id, (n) => {
        n.refs.owner = 'Finance'
        n.refs.software = ['SAP']
        n.instructions = 'Look.'
      }),
    )
    const lines = out.split('\n')
    const own = lines.findIndex((l) => l.includes(`id="${id}"`))
    expect(lines.slice(own, own + 8).map((l) => `${' '.repeat(indent(l))}${l.trim().split(' ')[0]}`)).toEqual([
      '    <bpmn:task',
      '      <bpmn:documentation',
      '      <bpmn:extensionElements>',
      '        <pa:owner',
      '        <pa:software',
      '      </bpmn:extensionElements>',
      '    </bpmn:task>',
      '    <bpmn:endEvent',
    ])
  })
})

describe('a file with Windows line ends', () => {
  it.each(playbooks)('%s: reads as the same file and is written back byte for byte', (_name, text) => {
    const dos = crlf(text)
    expect(dos).not.toBe(text)
    const model = parseBpmn(dos)
    expect(meaning(model)).toEqual(meaning(parseBpmn(text)))
    expect(serializeBpmn(model)).toBe(dos)
    // The reader and writer under it keep the line ends themselves, not only the shortcut for an unchanged file.
    expect(writeXml(parseXml(dos))).toBe(dos)
    // A rename in the middle of the file touches the one line.
    const { model: m, pid, process } = load(dos)
    const n = process.nodes[Math.floor(process.nodes.length / 2)]
    const { removed, added } = lineDiff(dos, serializeBpmn(updateNode(m, pid, n.id, (x) => (x.name = `${x.name} (renamed)`))))
    expect(removed).toHaveLength(1)
    expect(added).toHaveLength(1)
    expect(added[0].endsWith('\r')).toBe(removed[0].endsWith('\r'))
  })

  /** A handful of edits of different kinds, applied to a file's text. */
  function edited(text: string): string {
    const { model, pid, process } = load(text)
    const first = process.nodes.find((n) => n.spec.rules && n.instructions)!
    let m = updateNode(model, pid, first.id, (n) => {
      n.name = `${n.name} (edited)`
      n.spec.rules = `${n.spec.rules}\n99. An added rule.\nIt goes on to a second line.`
      n.spec.preconditions = 'There is something to do.'
      n.instructions = `${n.instructions}\nAnd one more line.`
      n.refs.software = [...n.refs.software, 'A new tool']
    })
    const gate = process.nodes.find((n) => n.type === 'exclusiveGateway')!
    m = addWayOut(m, pid, gate.id, { name: 'Another way' }).model
    const after = process.nodes.find((n) => n.id !== first.id && !whyNot(() => addStepAfter(m, pid, n.id)))!
    m = addStepAfter(m, pid, after.id, { name: 'An added step' }).model
    const flow = process.flows.find((f) => f.meaning)!
    m = updateFlow(m, pid, flow.id, (f) => (f.meaning = `${f.meaning} And more.`))
    return serializeBpmn(m)
  }

  it.each(playbooks)('%s: an edit gives the edit of the same file with Unix line ends, in Windows line ends', (_name, text) => {
    const dos = edited(crlf(text))
    expect(dos).toBe(crlf(edited(text)))
    expect(dos.replace(/\r\n/g, '')).not.toMatch(/[\r\n]/)
  })

  it('writes a changed line with the file\'s own line end, and leaves lines nobody touched as they were', () => {
    const text = crlf(fixture('process-wiki-export.bpmn'))
    const { model, pid } = load(text)
    const out = serializeBpmn(updateNode(model, pid, 'Step_review', (n) => (n.instructions = 'First line.\nSecond line.')))
    expect(out).toContain('First line.\r\nSecond line.')
    expect(out.replace(/\r\n/g, '')).not.toMatch(/[\r\n]/)
    expect(lineDiff(text, out).removed).toHaveLength(1)
  })

  it('reads a line end typed into an attribute as a space, as the engine does', () => {
    const xml = `<bpmn:definitions ${NS} id="D"><bpmn:process id="P"><bpmn:task id="T" name="one\r\ntwo\tthree"/></bpmn:process></bpmn:definitions>`
    expect(node(parseBpmn(xml).processes[0], 'T').name).toBe('one two three')
    expect(engineReads(xml).has('T')).toBe(true)
  })
})

describe('what is written is valid XML', () => {
  it('leaves out characters XML 1.0 forbids, and writes a newline, tab and CR in an attribute as references', () => {
    const { model, pid } = load(checkOne)
    const out = serializeBpmn(
      updateNode(model, pid, 'Make_check', (n) => {
        n.name = 'Make\fthe\u0001 check\nagain\there\r'
        n.instructions = 'Pasted\fform feed\u0000 and \u001f control.'
        n.spec.trigger = 'Pasted\u000b in a trigger.'
      }),
    )
    expect(out).not.toMatch(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/)
    expect(() => engineReads(out)).not.toThrow()
    expect(out).toContain('name="Makethe check&#10;again&#9;here&#13;"')
    const back = node(parseBpmn(out).processes[0], 'Make_check')
    expect(back.name).toBe('Makethe check\nagain\there\r')
    expect(back.instructions).toBe('Pastedform feed and  control.')
    expect(back.spec.trigger).toBe('Pasted in a trigger.')
  })

  it('repairs a forbidden character the file already holds in a line that is edited, and keeps the rest', () => {
    const text = `<bpmn:definitions ${NS} id="D"><bpmn:process id="P"><bpmn:task id="T" name="x"><bpmn:documentation id="T_instructions">Fine &gt; line.\nBad\fline.\nLast.</bpmn:documentation></bpmn:task></bpmn:process></bpmn:definitions>`
    const { model, pid } = load(text)
    const out = serializeBpmn(updateNode(model, pid, 'T', (n) => (n.instructions = n.instructions.replace('Last.', 'The last.'))))
    expect(out).toContain('Fine &gt; line.\nBadline.\nThe last.')
  })

  it('lone halves of a surrogate pair are left out, whole pairs stay', () => {
    const { model, pid } = load(checkOne)
    const out = serializeBpmn(updateNode(model, pid, 'Make_check', (n) => (n.name = 'Half\ud83d and whole 😀 and \ude00 half')))
    expect(node(parseBpmn(out).processes[0], 'Make_check').name).toBe('Half and whole 😀 and  half')
  })

  it.each([
    ['2nd check', 'Step_2nd_check'],
    ['Überprüfen & prüfen', 'Überprüfen_prüfen'],
    ['日本語 step', '日本語_step'],
    ['  ', 'Step'],
    ['!!! why', 'why'],
    ['3', 'Step_3'],
    ['New step', 'New_step'],
  ])('a new step called "%s" gets the id %s', (name, id) => {
    const { model, pid } = load(checkOne)
    const { model: grown, id: made } = addStepAfter(model, pid, 'Make_check', { name })
    expect(made).toBe(id)
    expect(made).toMatch(/^[\p{L}_][\p{L}\p{N}_.-]*$/u)
    const out = serializeBpmn(grown)
    expect(node(parseBpmn(out).processes[0], made).name).toBe(name)
    expect(() => engineReads(out)).not.toThrow()
  })

  it('a new way out and its end get ids that are names too', () => {
    const { model, pid } = load(checkOne)
    const { model: grown, flowId } = addWayOut(model, pid, 'Gw_found', { name: '2nd look' })
    expect(flowId).toMatch(/^[\p{L}_][\p{L}\p{N}_.-]*$/u)
    for (const n of grown.processes[0].nodes) expect(n.id).toMatch(/^[\p{L}_][\p{L}\p{N}_.-]*$/u)
  })
})

describe('a file that nests far too deep', () => {
  const head = '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="D"><bpmn:process id="P">'
  const tail = '</bpmn:process></bpmn:definitions>'

  it('is refused with a parse error, not a crash', () => {
    const deep = head + '<g>'.repeat(20000) + '</g>'.repeat(20000) + tail
    expect(() => parseBpmn(deep)).toThrow(BpmnParseError)
    expect(() => parseXml('<a>'.repeat(20000) + '</a>'.repeat(20000))).toThrow(XmlError)
    expect(() => parseBpmn(head + '<g>'.repeat(400) + '</g>'.repeat(400) + tail)).toThrow(/nested/)
  })

  it('reads nesting that a person could have written', () => {
    const ok = head + '<g>'.repeat(200) + '</g>'.repeat(200) + tail
    expect(writeXml(parseXml(ok))).toBe(ok)
    expect(parseBpmn(ok).processes).toHaveLength(1)
  })
})

describe('a file whose diagram namespaces are not on the root', () => {
  it('is written back byte for byte when it is only read, or edited in a way that touches no diagram', () => {
    expect(serializeBpmn(parseBpmn(LOCAL_DI))).toBe(LOCAL_DI)
    const { model, pid } = load(LOCAL_DI)
    const out = serializeBpmn(updateNode(model, pid, 'T', (n) => (n.name = 'Do it again')))
    const { removed, added } = lineDiff(LOCAL_DI, out)
    expect(removed).toEqual(['    <bpmn:task id="T" name="Do it"/>'])
    expect(added).toEqual(['    <bpmn:task id="T" name="Do it again"/>'])
    const text = serializeBpmn(updateFlow(model, pid, 'F1', (f) => (f.name = 'Go')))
    expect(text.split('\n')[1]).toBe(LOCAL_DI.split('\n')[1])
  })

  it('takes a step out without adding a namespace to the root', () => {
    const { model, pid } = load(LOCAL_DI)
    const out = serializeBpmn(removeStep(model, pid, 'T'))
    expect(out.split('\n')[1]).toBe(LOCAL_DI.split('\n')[1])
    expect(out).not.toContain('T_di')
  })

  it('draws a new step with the prefixes the diagram already uses', () => {
    const { model, pid } = load(LOCAL_DI)
    const { model: grown, id } = addStepAfter(model, pid, 'T', { name: 'Then this' })
    const out = serializeBpmn(grown)
    expect(out.split('\n')[1]).toBe(LOCAL_DI.split('\n')[1])
    expect(out).toContain(`<bpmndi:BPMNShape id="${id}_di" bpmnElement="${id}">`)
    expect(out).toContain('<dc:Bounds')
    expect(out).toContain('<di:waypoint')
    const lines = out.split('\n')
    const shape = lines.findIndex((l) => l.includes(`<bpmndi:BPMNShape id="${id}_di"`))
    expect(indent(lines[shape])).toBe(6)
    expect(indent(lines[shape + 1])).toBe(8)
  })
})
