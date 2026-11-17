import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import BpmnProcessView from '../BpmnProcessView'
import { parseBpmn } from '../bpmn/parse'
import { playbooks } from './testkit'

const checkOne = playbooks.find(([name]) => name === 'check_one_item.bpmn')![1]
const stepCount = parseBpmn(checkOne).processes[0].nodes.length

const titles = () =>
  Array.from(document.querySelectorAll('.step-list .step-title-editable')).map((n) =>
    (n.textContent ?? '').replace(/^(End of this path|Ends the whole process): /, ''),
  )
const circles = () => document.querySelectorAll('.react-flow__node').length

function open(id: string) {
  fireEvent.click(document.getElementById(`step-${id}`)!)
  return document.getElementById(`step-${id}`)!
}

describe('showing a BPMN file', () => {
  it('draws the Process flow graph and the Steps list from the file', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} />)
    expect(screen.getByRole('heading', { level: 2, name: 'Process flow' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Steps' })).toBeInTheDocument()
    expect(circles()).toBe(stepCount)
    expect(titles()).toHaveLength(stepCount)
    expect(titles()).toContain('Make the check')
    expect(screen.getByText('Reading')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
  })

  it("says who does a step and what it is responsible for, and leaves the engineering specification out", () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} />)
    const row = open('Make_check')
    const says = row.querySelector('.step-instructions')!
    expect(says.querySelector('code')!.textContent).toBe('_create_plan_checks')
    expect(says.querySelector('p')!.textContent).toBe('_create_plan_checks runs, which at a high level is responsible for:')
    expect(says.querySelectorAll('ul > li').length).toBeGreaterThanOrEqual(2)
    // Actor, Trigger, Rules ... (function and table names) are not the household's to read.
    expect(row.querySelector('.pv-spec')?.textContent ?? '').not.toMatch(/Actor|Trigger|Rules|Source/)
    expect(within(row).queryByText('Trigger')).toBeNull()
    expect(within(row).queryByText('Rules')).toBeNull()
    expect(within(row).queryByText('Commands')).toBeNull()
  })

  it("shows a step's specification only when the host asks, leaving out what is empty", () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} showSpec />)
    const row = open('Make_check')
    expect(within(row).getByText('Trigger')).toBeInTheDocument()
    expect(within(row).getByText('The item was tried.')).toBeInTheDocument()
    expect(within(row).queryByText('Preconditions')).toBeNull()
    expect(within(row).queryByText('Fired by')).toBeNull()
    // The rules are a numbered list.
    expect(within(row).getAllByRole('listitem').length).toBeGreaterThanOrEqual(2)
  })

  it("shows a decision's ways out with their words and rules", () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} />)
    const row = open('Gw_found')
    expect(within(row).getByText('Ways out')).toBeInTheDocument()
    expect(within(row).getByText('On plan')).toBeInTheDocument()
    expect(within(row).getByText('found.on_plan = true')).toBeInTheDocument()
  })

  it('marks the step a run is at', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} currentStepId="Gw_checker" />)
    expect(document.querySelectorAll('.step-run-mark-current')).toHaveLength(1)
    expect(document.getElementById('step-Gw_checker')).toHaveClass('step-row-run-current')
  })

  it('says so when the text is not BPMN', () => {
    render(<BpmnProcessView bpmn="<html/>" live={false} />)
    expect(screen.getByRole('alert')).toHaveTextContent(/not a BPMN file/)
  })
})

describe('editing in the Steps list', () => {
  it('shows the Edit toggle only when asked, and opens each step as text', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} editable />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    expect(screen.getByRole('button', { name: 'Editing' })).toHaveAttribute('aria-pressed', 'true')
    const row = open('Make_check')
    for (const label of ['Name', 'Who does it (lane)', 'Actor', 'Trigger', 'Preconditions', 'Inputs', 'Exceptions', 'Outputs', 'What the household sees', 'Source', 'Instructions', 'Decision criteria', 'Commands (what to run)', 'Prompts (what to ask an agent)', 'Owner', 'Roles', 'People', 'Software', 'Documents', 'Controls']) {
      expect(within(row).getByLabelText(label)).toBeInTheDocument()
    }
    expect((within(row).getByLabelText('Rule 1') as HTMLTextAreaElement).value).toContain('The check is a task')
    expect(within(row).getByLabelText('Trigger')).toHaveValue('The item was tried.')
    // There is nothing to drag on the diagram.
    expect(document.querySelector('.react-flow__node.draggable')).toBeNull()
  })

  it('gives the host the new file after every edit, with only that edit in it', () => {
    const onChange = vi.fn()
    render(<BpmnProcessView bpmn={checkOne} live={false} editable onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const row = open('Make_check')
    fireEvent.change(within(row).getByLabelText('Name'), { target: { value: 'Make the check, once' } })
    expect(onChange).toHaveBeenCalledTimes(1)
    const xml = onChange.mock.calls[0][0] as string
    expect(xml).toBe(checkOne.replace('name="Make the check"', 'name="Make the check, once"'))
    expect(titles()).toContain('Make the check, once')
  })

  it('redraws the diagram as steps are added, and the list follows', () => {
    const onChange = vi.fn()
    render(<BpmnProcessView bpmn={checkOne} live={false} editable onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const row = open('Make_check')
    expect(circles()).toBe(stepCount)
    fireEvent.click(within(row).getByRole('button', { name: 'Add a step after this' }))
    expect(circles()).toBe(stepCount + 1)
    expect(titles()).toHaveLength(stepCount + 1)
    const order = titles()
    expect(order.indexOf('New step')).toBe(order.indexOf('Make the check') + 1)
    // The new step is the one open, ready to be written.
    expect(document.getElementById('step-New_step')).toHaveClass('step-row-highlighted')
    expect(parseBpmn(onChange.mock.calls.at(-1)![0]).processes[0].nodes.map((n) => n.id)).toContain('New_step')
    // And taking it out again.
    fireEvent.click(within(document.getElementById('step-New_step')!).getByRole('button', { name: 'Remove this step' }))
    expect(circles()).toBe(stepCount)
    expect(onChange.mock.calls.at(-1)![0]).toBe(checkOne)
  })

  it('keeps what was open when a step is added', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} editable />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.click(within(document.getElementById('step-Porter_checks')!).getByRole('button', { name: /Expand details/ }))
    expect(within(document.getElementById('step-Porter_checks')!).queryByLabelText('Name')).not.toBeNull()
    const make = open('Make_check')
    fireEvent.click(within(make).getByRole('button', { name: 'Add a step after this' }))
    expect(within(document.getElementById('step-Porter_checks')!).queryByLabelText('Name')).not.toBeNull()
  })

  it("edits a decision's ways out: label, plain meaning, condition; adds one and removes one", () => {
    const onChange = vi.fn()
    render(<BpmnProcessView bpmn={checkOne} live={false} editable onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const row = open('Gw_found')
    const ways = within(row).getAllByRole('group', { name: /Way out/ })
    expect(ways).toHaveLength(2)
    fireEvent.change(within(ways[0]).getByLabelText('Rule for taking this way out'), { target: { value: 'found.on_plan = false' } })
    expect(onChange.mock.calls.at(-1)![0]).toContain('<bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">found.on_plan = false</bpmn:conditionExpression>')
    fireEvent.change(within(row).getByPlaceholderText('Label'), { target: { value: 'Needs a look' } })
    fireEvent.click(within(row).getByRole('button', { name: 'Add a way out' }))
    expect(within(document.getElementById('step-Gw_found')!).getAllByRole('group', { name: /Way out/ })).toHaveLength(3)
    expect(circles()).toBe(stepCount + 1)
    const last = within(document.getElementById('step-Gw_found')!).getAllByRole('group', { name: /Way out/ })
    fireEvent.click(within(last[2]).getByRole('button', { name: 'Remove this way out' }))
    expect(within(document.getElementById('step-Gw_found')!).getAllByRole('group', { name: /Way out/ })).toHaveLength(2)
    expect(circles()).toBe(stepCount)
  })

  it('numbers rules as they are added and removed', () => {
    const onChange = vi.fn()
    render(<BpmnProcessView bpmn={checkOne} live={false} editable onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const row = open('Make_check')
    fireEvent.click(within(row).getByRole('button', { name: 'Add a rule' }))
    fireEvent.change(within(row).getByLabelText('Rule 3'), { target: { value: 'A third rule.' } })
    expect(onChange.mock.calls.at(-1)![0]).toContain('\n3. A third rule.')
    fireEvent.click(within(row).getByRole('button', { name: 'Remove rule 1' }))
    const xml = onChange.mock.calls.at(-1)![0] as string
    expect(xml).toContain('\n1. Whether Porter makes the result itself')
    expect(xml).toContain('\n2. A third rule.')
  })

  it('says why a step cannot be removed or moved', () => {
    render(<BpmnProcessView bpmn={checkOne} live={false} editable />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const row = open('Gw_checker')
    const remove = within(row).getByRole('button', { name: 'Remove this step' })
    expect(remove).toBeDisabled()
    expect(remove).toHaveAttribute('title', expect.stringMatching(/one way in and one way out/))
  })

  it('asks the host to check the edited file, and shows what it refuses', async () => {
    const onValidate = vi.fn().mockResolvedValue({ ok: false, problems: ["The step 'X' has no way out."] })
    render(<BpmnProcessView bpmn={checkOne} live={false} editable onValidate={onValidate} />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    const row = open('Make_check')
    fireEvent.change(within(row).getByLabelText('Name'), { target: { value: 'Make it' } })
    expect(await screen.findByText("The step 'X' has no way out.")).toBeInTheDocument()
    expect(onValidate).toHaveBeenCalledWith(expect.stringContaining('name="Make it"'))
    expect(screen.getByText("It can't run as written:")).toBeInTheDocument()
  })

  it('says when the host accepts the file', async () => {
    const onValidate = vi.fn().mockResolvedValue({ ok: true, problems: [] })
    render(<BpmnProcessView bpmn={checkOne} live={false} editable onValidate={onValidate} />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.change(within(open('Make_check')).getByLabelText('Name'), { target: { value: 'Make it' } })
    await waitFor(() => expect(screen.getByText('It can run as written.')).toBeInTheDocument())
  })

  it('discards the edits and gives the host the file it started with', () => {
    const onChange = vi.fn()
    render(<BpmnProcessView bpmn={checkOne} live={false} editable onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.change(within(open('Make_check')).getByLabelText('Name'), { target: { value: 'Make it' } })
    expect(titles()).toContain('Make it')
    fireEvent.click(screen.getByRole('button', { name: 'Discard edits' }))
    expect(titles()).toContain('Make the check')
    expect(onChange.mock.calls.at(-1)![0]).toBe(checkOne)
  })

  it('takes a new file from the host when it is not the one it just wrote', () => {
    const { rerender } = render(<BpmnProcessView bpmn={checkOne} live={false} editable />)
    rerender(<BpmnProcessView bpmn={checkOne.replace('name="Make the check"', 'name="Made elsewhere"')} live={false} editable />)
    expect(titles()).toContain('Made elsewhere')
  })
})
