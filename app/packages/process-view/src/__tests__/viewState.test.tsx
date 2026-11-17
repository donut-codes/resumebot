// The view's state: who holds the draft, what stays selected, how a check with the host ends, and what the editor
// does with focus and keys. Found in the 2026-10-08 review of the package (section 3.7); each test is one finding.
import { Profiler, StrictMode, useState } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import BpmnProcessView, { type Validation } from '../BpmnProcessView'
import ProcessView from '../ProcessView'
import { processToGraph } from '../bpmn/graph'
import { parseBpmn } from '../bpmn/parse'
import { parseRuleItems } from '../bpmn/spec'
import { byAttrIn, byIdIn } from '../dom'
import { playbooks } from './testkit'

const checkOne = playbooks.find(([name]) => name === 'check_one_item.bpmn')![1]
const doOne = playbooks.find(([name]) => name === 'do_one_item.bpmn')![1]
const elsewhere = checkOne.replace('name="Make the check"', 'name="Made elsewhere"')

const titles = () =>
  Array.from(document.querySelectorAll('.step-list .step-title-editable')).map((n) =>
    (n.textContent ?? '').replace(/^(End of this path|Ends the whole process): /, ''),
  )
const row = (id: string) => document.getElementById(`step-${id}`)!
const highlighted = () => Array.from(document.querySelectorAll('.step-row-highlighted')).map((r) => r.id)
/** Selects a step by clicking its row (a click on an already selected step lets go of it). */
const open = (id: string) => {
  fireEvent.click(row(id))
  return row(id)
}
const startEditing = () => fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
const rename = (id: string, name: string) =>
  fireEvent.change(within(open(id)).getByLabelText('Name'), { target: { value: name } })

/**
 * Lets real time pass with React working as it does in a browser: an update is drawn when it comes, not held back
 * until act() ends (which would hide how often the view is drawn).
 */
async function timePasses(ms: number) {
  const env = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
  const was = env.IS_REACT_ACT_ENVIRONMENT
  env.IS_REACT_ACT_ENVIRONMENT = false
  try {
    await new Promise((r) => setTimeout(r, ms))
  } finally {
    env.IS_REACT_ACT_ENVIRONMENT = was
  }
}

/** A host that feeds every `onChange` straight back into `bpmn` (the README's example). */
function Controlled({ start, onChange }: { start: string; onChange?: (xml: string) => void }) {
  const [xml, setXml] = useState(start)
  return (
    <BpmnProcessView
      bpmn={xml}
      live={false}
      editable
      onChange={(next) => {
        onChange?.(next)
        setXml(next)
      }}
    />
  )
}

describe('who holds the draft', () => {
  it('a host that feeds onChange back into bpmn keeps the edit, and Discard still goes back', () => {
    const onChange = vi.fn()
    render(<Controlled start={checkOne} onChange={onChange} />)
    startEditing()
    rename('Make_check', 'Make it')
    // The text the view just reported, handed back as `bpmn`, is not a new file: nothing is said, nothing is lost.
    expect(titles()).toContain('Make it')
    expect(screen.queryByText(/changed since you started editing/)).toBeNull()
    fireEvent.change(within(row('Make_check')).getByLabelText('Name'), { target: { value: 'Make it twice' } })
    expect(titles()).toContain('Make it twice')
    fireEvent.click(screen.getByRole('button', { name: 'Discard edits' }))
    expect(titles()).toContain('Make the check')
    expect(titles()).not.toContain('Make it twice')
    expect(onChange.mock.calls.at(-1)![0]).toBe(checkOne)
    expect(screen.queryByRole('button', { name: 'Discard edits' })).toBeNull()
  })

  it('keeps the edits when the host gives a different file, says so, and shows the new file after Discard', () => {
    const onChange = vi.fn()
    const { rerender } = render(<BpmnProcessView bpmn={checkOne} live={false} editable onChange={onChange} />)
    startEditing()
    rename('Make_check', 'Mine')
    rerender(<BpmnProcessView bpmn={elsewhere} live={false} editable onChange={onChange} />)
    expect(titles()).toContain('Mine')
    expect(titles()).not.toContain('Made elsewhere')
    expect(
      screen.getByText('This process changed since you started editing. Discard your edits to see the new version.'),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Discard edits' }))
    expect(titles()).toContain('Made elsewhere')
    expect(titles()).not.toContain('Mine')
    expect(onChange.mock.calls.at(-1)![0]).toBe(elsewhere)
    expect(screen.queryByText(/changed since you started editing/)).toBeNull()
  })

  it('shows a new file from the host at once when nothing is edited, with no note', () => {
    const { rerender } = render(<BpmnProcessView bpmn={checkOne} live={false} editable />)
    startEditing()
    rerender(<BpmnProcessView bpmn={elsewhere} live={false} editable />)
    expect(titles()).toContain('Made elsewhere')
    expect(screen.queryByText(/changed since you started editing/)).toBeNull()
  })

  it('shows the new file when the edits are put back by hand', () => {
    const onChange = vi.fn()
    const { rerender } = render(<BpmnProcessView bpmn={checkOne} live={false} editable onChange={onChange} />)
    startEditing()
    rename('Make_check', 'Mine')
    rerender(<BpmnProcessView bpmn={elsewhere} live={false} editable onChange={onChange} />)
    fireEvent.change(within(row('Make_check')).getByLabelText('Name'), { target: { value: 'Make the check' } })
    // Nothing is edited any more, so the host's file is the one shown, and the host is told which text that is.
    expect(titles()).toContain('Made elsewhere')
    expect(screen.queryByText(/changed since you started editing/)).toBeNull()
    expect(onChange.mock.calls.at(-1)![0]).toBe(elsewhere)
  })

  it('ends editing, and drops edits not sent, when editable turns false', () => {
    const onChange = vi.fn()
    const onEditingChange = vi.fn()
    const { rerender } = render(
      <BpmnProcessView bpmn={checkOne} live={false} editable onChange={onChange} onEditingChange={onEditingChange} />,
    )
    startEditing()
    rename('Make_check', 'Mine')
    rerender(
      <BpmnProcessView bpmn={checkOne} live={false} editable={false} onChange={onChange} onEditingChange={onEditingChange} />,
    )
    expect(titles()).toContain('Make the check')
    expect(screen.queryByRole('button', { name: 'Editing' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
    expect(onChange.mock.calls.at(-1)![0]).toBe(checkOne)
    expect(onEditingChange).toHaveBeenLastCalledWith(false)
  })

  it('says the editor started or stopped once each, even when React runs everything twice', () => {
    const onEditingChange = vi.fn()
    render(
      <StrictMode>
        <BpmnProcessView bpmn={checkOne} live={false} editable onEditingChange={onEditingChange} />
      </StrictMode>,
    )
    startEditing()
    expect(onEditingChange).toHaveBeenCalledTimes(1)
    expect(onEditingChange).toHaveBeenLastCalledWith(true)
    fireEvent.click(screen.getByRole('button', { name: 'Editing' }))
    expect(onEditingChange).toHaveBeenCalledTimes(2)
    expect(onEditingChange).toHaveBeenLastCalledWith(false)
  })

  it('says an unknown processId in plain words instead of showing the first process', () => {
    render(<BpmnProcessView bpmn={checkOne} processId="Nope" live={false} editable />)
    expect(screen.getByRole('alert')).toHaveTextContent('This file has no process called “Nope”.')
    expect(document.querySelector('.step-list')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
  })

  it('shows the process the id names', () => {
    const id = parseBpmn(checkOne).processes[0].id
    render(<BpmnProcessView bpmn={checkOne} processId={id} live={false} />)
    expect(titles()).toContain('Make the check')
  })

  it('calls a step whose name was cleared "Unnamed step"', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} editable />)
    startEditing()
    rename('Make_check', '')
    expect(titles()).toContain('Unnamed step')
    expect(titles()).not.toContain('serviceTask')
  })
})

describe('what stays selected', () => {
  it('stays put after a Move or a Remove instead of snapping back to the step added last', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} editable />)
    startEditing()
    fireEvent.click(within(open('Make_check')).getByRole('button', { name: 'Add a step after this' }))
    expect(highlighted()).toEqual(['step-New_step'])
    // Select another step and move it: it stays the selected one.
    open('Make_check')
    expect(highlighted()).toEqual(['step-Make_check'])
    fireEvent.click(within(row('Make_check')).getByRole('button', { name: 'Move down' }))
    expect(highlighted()).toEqual(['step-Make_check'])
    // Remove another: the selection goes to the step that follows it, never back to the one added.
    open('Porter_checks')
    fireEvent.click(within(row('Porter_checks')).getByRole('button', { name: 'Remove this step' }))
    expect(document.getElementById('step-Porter_checks')).toBeNull()
    expect(highlighted()).toEqual(['step-Gw_found'])
  })

  it('can be asked to show the same new step again after it was taken out', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} editable />)
    startEditing()
    fireEvent.click(within(open('Make_check')).getByRole('button', { name: 'Add a step after this' }))
    fireEvent.click(within(row('New_step')).getByRole('button', { name: 'Remove this step' }))
    expect(document.getElementById('step-New_step')).toBeNull()
    fireEvent.click(within(open('Make_check')).getByRole('button', { name: 'Add a step after this' }))
    expect(highlighted()).toEqual(['step-New_step'])
  })

  it('a click inside an opened step does not let go of it; a click on its header does', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} editable />)
    startEditing()
    const make = open('Make_check')
    expect(highlighted()).toEqual(['step-Make_check'])
    fireEvent.click(within(make).getByLabelText('Name'))
    fireEvent.click(make.querySelector('.pv-edit')!)
    expect(highlighted()).toEqual(['step-Make_check'])
    fireEvent.click(make.querySelector('.step-title-row')!)
    expect(highlighted()).toEqual([])
  })

  it('does the same when reading', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} />)
    const make = open('Make_check')
    fireEvent.click(make.querySelector('.step-detail-body')!)
    expect(highlighted()).toEqual(['step-Make_check'])
  })
})

describe('keys', () => {
  it('the arrow keys and Escape work inside the view, not on the rest of the page', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} />)
    const make = open('Make_check')
    fireEvent.keyDown(document.body, { key: 'ArrowDown' })
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(highlighted()).toEqual(['step-Make_check'])
    fireEvent.keyDown(make, { key: 'ArrowDown' })
    expect(highlighted()).toEqual(['step-Gw_checker'])
    fireEvent.keyDown(row('Gw_checker'), { key: 'Escape' })
    expect(highlighted()).toEqual([])
  })

  it('leaves a key typed into a box to the box', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} editable />)
    startEditing()
    fireEvent.keyDown(within(open('Make_check')).getByLabelText('Name'), { key: 'Escape' })
    expect(highlighted()).toEqual(['step-Make_check'])
  })

  it('does not stop a key from reaching the host around the editor', () => {
    const heard = vi.fn()
    render(
      <div onKeyDown={heard}>
        <BpmnProcessView bpmn={checkOne} live={false} editable />
      </div>,
    )
    startEditing()
    fireEvent.keyDown(within(open('Make_check')).getByLabelText('Name'), { key: 'x' })
    expect(heard).toHaveBeenCalled()
  })

  it('two views on one page do not act on each other', () => {
    render(
      <>
        <BpmnProcessView bpmn={checkOne} live={false} />
        <BpmnProcessView bpmn={checkOne} live={false} />
      </>,
    )
    const views = Array.from(document.querySelectorAll<HTMLElement>('.process-view'))
    expect(views).toHaveLength(2)
    // Their section ids are their own.
    const ids = Array.from(document.querySelectorAll('.process-view > section')).map((s) => s.id)
    expect(ids).toHaveLength(4)
    expect(new Set(ids).size).toBe(4)
    // Selecting in the second leaves the first alone, and a link inside it goes to a step of the second.
    const second = views[1].querySelector<HTMLElement>('#step-Make_check')!
    fireEvent.click(second)
    expect(views[0].querySelectorAll('.step-row-highlighted')).toHaveLength(0)
    fireEvent.click(within(second).getByRole('button', { name: /Who checks it\?, which/ }))
    expect(views[1].querySelectorAll('.step-row-highlighted')).toHaveLength(1)
    expect(views[1].contains(document.activeElement)).toBe(true)
    expect(views[0].querySelectorAll('.step-row-highlighted')).toHaveLength(0)
    // Keys pressed in one move only that one.
    fireEvent.click(views[0].querySelector<HTMLElement>('#step-Make_check')!)
    fireEvent.keyDown(views[0].querySelector<HTMLElement>('#step-Make_check')!, { key: 'Escape' })
    expect(views[0].querySelectorAll('.step-row-highlighted')).toHaveLength(0)
    expect(views[1].querySelectorAll('.step-row-highlighted')).toHaveLength(1)
  })
})

describe('finding things in the view\'s own markup', () => {
  it('finds an id or a data-id with a quote in it, and only inside the root', () => {
    const root = document.createElement('div')
    root.innerHTML = '<p id=\'step-a"b\'></p><i class="n" data-id=\'x"y\'></i>'
    const other = document.createElement('div')
    other.innerHTML = '<p id=\'step-a"b\'></p>'
    expect(byIdIn(root, 'step-a"b')).toBe(root.firstElementChild)
    expect(byIdIn(root, 'step-a"b')).not.toBe(other.firstElementChild)
    expect(byAttrIn(root, '.n', 'data-id', 'x"y')).toBe(root.lastElementChild)
    expect(byAttrIn(root, '.n', 'data-id', 'nope')).toBeNull()
    expect(byIdIn(null, 'x')).toBeNull()
  })
})

describe('the editor', () => {
  it('an open blank "Add a rule" box does not write a stray rule', () => {
    const onChange = vi.fn()
    render(<BpmnProcessView bpmn={checkOne} live={false} editable onChange={onChange} />)
    startEditing()
    const make = open('Make_check')
    fireEvent.click(within(make).getByRole('button', { name: 'Add a rule' }))
    const rule1 = within(make).getByLabelText('Rule 1') as HTMLTextAreaElement
    fireEvent.change(rule1, { target: { value: `${rule1.value} (changed)` } })
    const xml = onChange.mock.calls.at(-1)![0] as string
    expect(xml).not.toMatch(/^\s*0\.\s/m)
    const rules = parseBpmn(xml).processes[0].nodes.find((n) => n.id === 'Make_check')!.spec.rules
    expect(parseRuleItems(rules).map((r) => r.n)).toEqual([1, 2])
    // The box is still open, waiting for words.
    expect(within(make).getByLabelText('Rule 3')).toHaveValue('')
  })

  it('the controls box keeps a bar while you type it', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} editable />)
    startEditing()
    const controls = within(open('Make_check')).getByLabelText('Controls') as HTMLTextAreaElement
    fireEvent.change(controls, { target: { value: 'A.9.4 | ' } })
    expect(controls.value).toBe('A.9.4 | ')
    fireEvent.change(controls, { target: { value: 'A.9.4 | ISO 27001' } })
    expect(controls.value).toBe('A.9.4 | ISO 27001')
    fireEvent.change(controls, { target: { value: 'A.9.4 | ISO 27001\nB.1' } })
    expect(controls.value).toBe('A.9.4 | ISO 27001\nB.1')
  })

  it('puts the cursor in the new step\'s Name box, on the next step after a remove, and back on the button after a move', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} editable />)
    startEditing()
    fireEvent.click(within(open('Make_check')).getByRole('button', { name: 'Add a step after this' }))
    expect(document.activeElement).toBe(within(row('New_step')).getByLabelText('Name'))
    // Remove it: the cursor is on the title of the step that follows.
    fireEvent.click(within(row('New_step')).getByRole('button', { name: 'Remove this step' }))
    expect(document.activeElement).toBe(row('Gw_checker').querySelector('.step-title-editable'))
    // Three new steps in a row, then move the first down: the button used keeps the cursor.
    fireEvent.click(within(open('Make_check')).getByRole('button', { name: 'Add a step after this' }))
    fireEvent.click(within(row('New_step')).getByRole('button', { name: 'Add a step after this' }))
    fireEvent.click(within(row('New_step_2')).getByRole('button', { name: 'Add a step after this' }))
    open('New_step')
    fireEvent.click(within(row('New_step')).getByRole('button', { name: 'Move down' }))
    expect(titles().slice(1, 5)).toEqual(['Make the check', 'New step', 'New step', 'New step'])
    expect(document.activeElement).toBe(within(row('New_step')).getByRole('button', { name: 'Move down' }))
  })

  it('says why a button is greyed out in text a screen reader reads with it, not only in a tooltip', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} editable />)
    startEditing()
    const remove = within(open('Gw_checker')).getByRole('button', { name: 'Remove this step' })
    expect(remove).toBeDisabled()
    const reasonId = remove.getAttribute('aria-describedby')!
    expect(document.getElementById(reasonId)).toHaveTextContent(/one way in and one way out/)
  })

  it('names a way out\'s target and the "Leads to" choices in words, never by id', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} editable />)
    startEditing()
    const gw = open('Gw_checker')
    const ways = within(gw).getAllByRole('group', { name: /Way out/ })
    expect(ways[0]).toHaveAccessibleName(/to Porter reads the result/)
    expect(ways[1]).toHaveAccessibleName(/to A specialist checks the item/)
    const options = within(within(gw).getByLabelText('Leads to')).getAllByRole('option').map((o) => o.textContent)
    expect(options).toContain('A specialist checks the item')
    expect(options.join('|')).not.toMatch(/_/)
  })

  it('turns spell check off for code-like boxes', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} editable />)
    startEditing()
    const gw = open('Gw_found')
    expect(within(gw).getAllByLabelText('Rule for taking this way out')[0]).toHaveAttribute('spellcheck', 'false')
    expect(within(gw).getByLabelText('Commands (what to run)')).toHaveAttribute('spellcheck', 'false')
    expect(within(gw).getByLabelText('Instructions')).toHaveAttribute('spellcheck', 'true')
  })
})

describe('checking with the host', () => {
  it('checks the file as loaded when editing starts, and not before', async () => {
    const onValidate = vi.fn().mockResolvedValue({ ok: true, problems: [] })
    render(<BpmnProcessView bpmn={checkOne} live={false} editable onValidate={onValidate} />)
    await new Promise((r) => setTimeout(r, 50))
    expect(onValidate).not.toHaveBeenCalled()
    startEditing()
    await waitFor(() => expect(onValidate).toHaveBeenCalledWith(checkOne))
    expect(await screen.findByText('It can run as written.')).toBeInTheDocument()
  })

  it('does not leave "Checking" up when the check throws', async () => {
    const onValidate = vi.fn(() => {
      throw new Error('boom')
    })
    render(<BpmnProcessView bpmn={checkOne} live={false} editable onValidate={onValidate} />)
    startEditing()
    expect(await screen.findByText('The check could not be run.')).toBeInTheDocument()
    expect(screen.queryByText(/Checking it can run/)).toBeNull()
  })

  it('does not leave "Checking" up when the check rejects', async () => {
    const onValidate = vi.fn().mockRejectedValue(new Error('down'))
    render(<BpmnProcessView bpmn={checkOne} live={false} editable onValidate={onValidate} />)
    startEditing()
    expect(await screen.findByText('The check could not be run.')).toBeInTheDocument()
    expect(screen.queryByText(/Checking it can run/)).toBeNull()
  })

  it('takes an answer with no problems listed as a plain refusal, not a crash', async () => {
    const onValidate = vi.fn().mockResolvedValue({ ok: false })
    render(<BpmnProcessView bpmn={checkOne} live={false} editable onValidate={onValidate} />)
    startEditing()
    expect(await screen.findByText("It can't run as written.")).toBeInTheDocument()
    expect(screen.queryByText(/Checking it can run/)).toBeNull()
  })

  it('takes an answer with nothing in it as a plain refusal too', async () => {
    const onValidate = vi.fn().mockResolvedValue(undefined)
    render(<BpmnProcessView bpmn={checkOne} live={false} editable onValidate={onValidate} />)
    startEditing()
    expect(await screen.findByText("It can't run as written.")).toBeInTheDocument()
  })

  it('shows only the answer to the latest check, however the answers come back', async () => {
    const answers: ((v: Validation) => void)[] = []
    const onValidate = vi.fn(
      () =>
        new Promise<Validation>((resolve) => {
          answers.push(resolve)
        }),
    )
    render(<BpmnProcessView bpmn={checkOne} live={false} editable onValidate={onValidate} />)
    startEditing()
    await waitFor(() => expect(onValidate).toHaveBeenCalledTimes(1))
    rename('Make_check', 'Make it')
    await waitFor(() => expect(onValidate).toHaveBeenCalledTimes(2))
    // The later check answers first, then the earlier one limps in.
    await act(async () => answers[1]({ ok: true, problems: [] }))
    expect(await screen.findByText('It can run as written.')).toBeInTheDocument()
    await act(async () => answers[0]({ ok: false, problems: ['An answer about the old file.'] }))
    expect(screen.queryByText('An answer about the old file.')).toBeNull()
    expect(screen.getByText('It can run as written.')).toBeInTheDocument()
  })
})

describe('the Process flow graph', () => {
  it('is visible again when a different process is passed to a mounted live view', async () => {
    const board = () => document.querySelector('.read-flow-board')!
    const { rerender } = render(<BpmnProcessView bpmn={checkOne} />)
    await waitFor(() => expect(board()).not.toHaveClass('pw-flow-framing'))
    rerender(<BpmnProcessView bpmn={doOne} />)
    await waitFor(() => expect(board()).not.toHaveClass('pw-flow-framing'))
    expect(document.querySelectorAll('.react-flow__node')).toHaveLength(processToGraph(parseBpmn(doOne).processes[0]).steps.length)
  })

  it('does not draw the whole view again while its circles move or idle', async () => {
    const { steps, edges } = processToGraph(parseBpmn(checkOne).processes[0])
    const renderDetail = vi.fn(() => null)
    render(<ProcessView steps={steps} edges={edges} renderDetail={renderDetail} />)
    // A step is open, so each time the view is drawn it asks for its detail.
    fireEvent.click(document.getElementById(`step-${steps[1].id}`)!)
    const before = renderDetail.mock.calls.length
    expect(before).toBeGreaterThan(0)
    await timePasses(1500)
    expect(renderDetail.mock.calls.length).toBe(before)
    // The graph itself is there.
    expect(document.querySelectorAll('.react-flow__node')).toHaveLength(steps.length)
  })

  it('draws the graph less and less as the circles settle, instead of ten times a second for good', async () => {
    const { steps, edges } = processToGraph(parseBpmn(checkOne).processes[0])
    const drawn = vi.fn()
    render(
      <Profiler id="view" onRender={drawn}>
        <ProcessView steps={steps} edges={edges} />
      </Profiler>,
    )
    await timePasses(1500)
    const settled = drawn.mock.calls.length
    await timePasses(1500)
    // Drawn on every sixth step of the simulation it would be 15 times or more in this stretch; a few is a layout
    // still easing into place.
    expect(drawn.mock.calls.length - settled).toBeLessThanOrEqual(8)
  }, 20000)
})
