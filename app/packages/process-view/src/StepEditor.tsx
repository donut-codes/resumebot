// A step's fields as text, and the list operations on it. Every field is a plain text box: the process is edited as
// words, never by dragging on the diagram.
import { useEffect, useId, useRef, useState, type Ref } from 'react'
import {
  addStepAfter, addWayOut, moveStep, removeStep, removeWayOut, updateFlow, updateNode, whyNot,
} from './bpmn/edit'
import {
  EditError, isGateway, type BpmnFlow, type BpmnModel, type BpmnNode, type BpmnProcess, type SpecFields,
} from './bpmn/model'
import { formatRuleItems, parseRuleItems, renumber, type RuleItem } from './bpmn/spec'
import { byAttrIn } from './dom'

export interface StepEditorProps {
  model: BpmnModel
  process: BpmnProcess
  node: BpmnNode
  /**
   * The model after an edit, and the step to show next: the one just added, or after a removal the one that follows
   * it. `where` says what takes the cursor there: the step's Name box (a new step, ready to be written) or its title
   * in the list (a step that was only moved to).
   */
  apply: (next: BpmnModel, focusId?: string, where?: 'name' | 'title') => void
  /** Said when an operation can't be done. */
  onProblem?: (message: string | null) => void
  /** true: the Name box takes the cursor when this opens (a step that was just added). */
  autoFocusName?: boolean
  /** Said once the Name box has taken the cursor, so the request is used up. */
  onNameFocused?: () => void
}

const rowsFor = (value: string) => Math.min(14, Math.max(1, value.split('\n').length + Math.floor(value.length / 100)))

/** What a step is called where a person reads it: its name, or "Unnamed step" when it has none. */
const nameOf = (n: { name: string }) => n.name.trim() || 'Unnamed step'

function Field({
  label, value, onChange, multiline = true, hint, list, code = false, placeholder, inputRef,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  multiline?: boolean
  hint?: string
  list?: string[]
  /** Code-like text (a rule the engine reads, a setting, a file or function name): no spell check. */
  code?: boolean
  placeholder?: string
  inputRef?: Ref<HTMLInputElement>
}) {
  const id = useId()
  const listId = `${id}-list`
  return (
    <div className="pv-edit-field">
      <label htmlFor={id}>{label}</label>
      {multiline ? (
        <textarea id={id} value={value} rows={rowsFor(value)} onChange={(e) => onChange(e.target.value)} spellCheck={!code} />
      ) : (
        <input
          id={id} ref={inputRef} type="text" value={value} list={list ? listId : undefined} placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)} spellCheck={!code}
        />
      )}
      {list && (
        <datalist id={listId}>
          {list.map((l) => (
            <option key={l} value={l} />
          ))}
        </datalist>
      )}
      {hint && <p className="pv-edit-hint">{hint}</p>}
    </div>
  )
}

/**
 * A list, one to a line. Keeps what is being typed (a blank line, a bar with nothing after it yet) until the step
 * changes: the text is put back from the list only when it no longer reads as the same list.
 */
function LinesField<T>({ label, value, onChange, hint, code, format, parse }: {
  label: string
  value: T[]
  onChange: (v: T[]) => void
  hint?: string
  code?: boolean
  /** A list item as the line that shows it. */
  format: (item: T) => string
  /** A line as a list item, or null for a line that is not one (empty). */
  parse: (line: string) => T | null
}) {
  const read = (text: string) => text.split('\n').map(parse).filter((x): x is T => x !== null)
  const [text, setText] = useState(value.map(format).join('\n'))
  const shown = JSON.stringify(value)
  useEffect(() => {
    if (JSON.stringify(read(text)) !== shown) setText(value.map(format).join('\n'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shown])
  return (
    <Field
      label={label}
      value={text}
      hint={hint}
      code={code}
      onChange={(t) => {
        setText(t)
        onChange(read(t))
      }}
    />
  )
}

const asLine = (s: string) => s
const fromLine = (line: string) => line.trim() || null

function RulesField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const items = parseRuleItems(value)
  // A rule being started: shown as an empty box until it has words. It is never part of what is written: a box
  // that is open and still blank leaves the rules as they are.
  const [starting, setStarting] = useState(false)
  const rows: RuleItem[] = starting ? [...items, { n: 0, text: '' }] : items
  const commit = (next: RuleItem[], renum = false) => onChange(formatRuleItems(renum ? renumber(next) : next))
  return (
    <fieldset className="pv-edit-field pv-edit-rules">
      <legend>Rules</legend>
      <ol>
        {rows.map((item, i) => (
          <li key={i}>
            <span className="pv-rule-n" aria-hidden="true">
              {item.n ? `${item.n}.` : ''}
            </span>
            <textarea
              aria-label={`Rule ${i + 1}`}
              value={item.text}
              rows={rowsFor(item.text)}
              autoFocus={starting && i === items.length}
              onChange={(e) => {
                const text = e.target.value
                const next = rows.map((r, k) => (k === i ? { ...r, text } : r))
                if (starting && i === items.length) {
                  if (!text.trim()) return
                  setStarting(false)
                  // The new rule takes the next number; the others keep theirs.
                  next[i].n = items.reduce((m, r) => Math.max(m, r.n ?? 0), 0) + 1
                  commit(next)
                } else {
                  // Editing another rule while a blank box is open: the blank box is not written.
                  commit(next.slice(0, items.length))
                }
              }}
            />
            <button
              type="button"
              className="pv-btn pv-btn-quiet pv-btn-sm"
              aria-label={`Remove rule ${i + 1}`}
              onClick={() => {
                if (starting && i === items.length) setStarting(false)
                else commit(items.filter((_, k) => k !== i), true)
              }}
            >
              Remove
            </button>
          </li>
        ))}
      </ol>
      {!starting && (
        <button type="button" className="pv-btn pv-btn-quiet pv-btn-sm" onClick={() => setStarting(true)}>
          Add a rule
        </button>
      )}
    </fieldset>
  )
}

/** Attributes a host's engine reads from a step (`porter:role` ...), shown as they are written, one text box each. */
const KNOWN_SETTINGS = ['role', 'instructions', 'template', 'action', 'card', 'harness', 'output', 'in', 'out', 'engine']

function PorterSettings({ node, onSet }: { node: BpmnNode; onSet: (key: string, value: string) => void }) {
  const present = Object.keys(node.porter)
  // A setting stays on screen while its box is emptied or just added, until the step is closed.
  const [seen, setSeen] = useState<string[]>(present)
  const [adding, setAdding] = useState('')
  const shown = [...new Set([...seen, ...present])]
  const missing = KNOWN_SETTINGS.filter((k) => !shown.includes(k))
  if (shown.length === 0 && missing.length === 0) return null
  return (
    <div className="pv-edit-settings">
      <h5 className="pv-edit-group">How it runs</h5>
      {shown.map((key) => (
        <Field key={key} label={key} multiline={false} code value={node.porter[key] ?? ''} onChange={(v) => onSet(key, v)} />
      ))}
      <div className="pv-edit-addway">
        <label>
          Add a setting
          <select value={adding} onChange={(e) => setAdding(e.target.value)}>
            <option value="">Choose one</option>
            {missing.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="pv-btn pv-btn-quiet pv-btn-sm"
          disabled={!adding}
          onClick={() => {
            setSeen([...shown, adding])
            setAdding('')
          }}
        >
          Add
        </button>
      </div>
    </div>
  )
}

/** A button that may be greyed out. When it is, the reason is said in text a screen reader reads with the button, and
 * in the tooltip, not in the tooltip alone. */
function ActionButton({ label, why, onClick, act }: { label: string; why: string | null; onClick: () => void; act?: string }) {
  const reasonId = useId()
  return (
    <>
      <button
        type="button"
        className="pv-btn pv-btn-quiet pv-btn-sm"
        data-pv-act={act}
        disabled={why !== null}
        title={why ?? undefined}
        aria-describedby={why !== null ? reasonId : undefined}
        onClick={onClick}
      >
        {label}
      </button>
      {why !== null && (
        <span id={reasonId} className="pv-visually-hidden">
          {why}
        </span>
      )}
    </>
  )
}

function WayOut({ flow, node, targets, onFlow, onRemove, canRemove }: {
  flow: BpmnFlow
  node: BpmnNode
  targets: BpmnNode[]
  onFlow: (fn: (f: BpmnFlow) => void) => void
  onRemove: () => void
  canRemove: string | null
}) {
  const target = targets.find((t) => t.id === flow.target)
  return (
    <fieldset className="pv-edit-way">
      <legend>
        Way out{flow.id === node.defaultFlow ? ' (when nothing else applies)' : ''}: to {target ? nameOf(target) : 'a step that is not in the file'}
      </legend>
      <Field label="Label" multiline={false} value={flow.name} onChange={(v) => onFlow((f) => (f.name = v))} />
      <Field label="Plain meaning" value={flow.meaning} onChange={(v) => onFlow((f) => (f.meaning = v))} />
      <Field
        label="Rule for taking this way out"
        multiline={false}
        code
        value={flow.condition}
        onChange={(v) => onFlow((f) => (f.condition = v))}
        hint="Written the way the engine reads it, for example found.on_plan = true. Leave empty for the way out that applies when nothing else does."
      />
      <ActionButton label="Remove this way out" why={canRemove} onClick={onRemove} />
    </fieldset>
  )
}

/** A control as the line that shows it: the reference, then a bar and its framework when it has one. */
const controlLine = (c: { reference: string; framework: string }) => (c.framework ? `${c.reference} | ${c.framework}` : c.reference)
function controlFromLine(line: string): { reference: string; framework: string } | null {
  const [reference, ...rest] = line.split('|')
  const control = { reference: reference.trim(), framework: rest.join('|').trim() }
  return control.reference ? control : null
}

export default function StepEditor({ model, process, node, apply, onProblem, autoFocusName = false, onNameFocused }: StepEditorProps) {
  const pid = process.id
  const rootRef = useRef<HTMLDivElement>(null)
  const nameRef = useRef<HTMLInputElement>(null)
  // The button to give the cursor back to after the list changes: a step that moves has its row taken out of the
  // page and put back, and the browser lets go of what was focused in it.
  const refocus = useRef<string | null>(null)
  const edit = (fn: (n: BpmnNode) => void) => apply(updateNode(model, pid, node.id, fn))
  const spec = (key: keyof SpecFields) => (v: string) => edit((n) => (n.spec[key] = v))
  const attempt = (op: () => { model: BpmnModel; focus?: string; where?: 'name' | 'title' }) => {
    try {
      const out = op()
      onProblem?.(null)
      apply(out.model, out.focus, out.where)
    } catch (e) {
      refocus.current = null
      if (e instanceof EditError) onProblem?.(e.message)
      else throw e
    }
  }
  const gateway = isGateway(node)
  const ways = process.flows.filter((f) => f.source === node.id)
  const whyAdd = whyNot(() => addStepAfter(model, pid, node.id))
  const whyRemove = whyNot(() => removeStep(model, pid, node.id))
  const whyUp = whyNot(() => moveStep(model, pid, node.id, 'up'))
  const whyDown = whyNot(() => moveStep(model, pid, node.id, 'down'))
  const lanes = process.lanes.map((l) => l.name).filter(Boolean)
  const [newWay, setNewWay] = useState('')
  const [newTarget, setNewTarget] = useState('')

  // A step just added opens with the cursor in its Name box, ready to be written.
  useEffect(() => {
    if (!autoFocusName) return
    nameRef.current?.focus()
    onNameFocused?.()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // After a list operation the cursor goes back to the button that was used (or, if that is greyed out now, to the
  // first one that is not) instead of being lost.
  useEffect(() => {
    const act = refocus.current
    if (!act) return
    refocus.current = null
    const root = rootRef.current
    const same = byAttrIn<HTMLButtonElement>(root, 'button', 'data-pv-act', act)
    const target = same && !same.disabled ? same : root?.querySelector<HTMLElement>('.pv-edit-bar button:not(:disabled)')
    ;(target ?? nameRef.current)?.focus()
  })

  const removeFocus = process.flows.find((f) => f.source === node.id)?.target

  return (
    <div className="pv-edit" ref={rootRef}>
      <div className="pv-edit-bar" role="group" aria-label="Change the list">
        <ActionButton
          label="Add a step after this" why={whyAdd} act="add"
          onClick={() => attempt(() => {
            const added = addStepAfter(model, pid, node.id)
            return { model: added.model, focus: added.id, where: 'name' }
          })}
        />
        <ActionButton
          label="Move up" why={whyUp} act="up"
          onClick={() => {
            refocus.current = 'up'
            attempt(() => ({ model: moveStep(model, pid, node.id, 'up') }))
          }}
        />
        <ActionButton
          label="Move down" why={whyDown} act="down"
          onClick={() => {
            refocus.current = 'down'
            attempt(() => ({ model: moveStep(model, pid, node.id, 'down') }))
          }}
        />
        <ActionButton
          label="Remove this step" why={whyRemove} act="remove"
          onClick={() => attempt(() => ({ model: removeStep(model, pid, node.id), focus: removeFocus, where: 'title' }))}
        />
      </div>

      <Field label="Name" multiline={false} value={node.name} placeholder="Unnamed step" inputRef={nameRef} onChange={(v) => edit((n) => (n.name = v))} />
      <Field label="Who does it (lane)" multiline={false} value={node.lane} list={lanes} onChange={(v) => edit((n) => (n.lane = v))} />
      <Field label="Actor" value={node.spec.actor} onChange={spec('actor')} />
      <Field label="Trigger" value={node.spec.trigger} onChange={spec('trigger')} />
      {(node.type === 'startEvent' || node.spec.firedBy || node.spec.entersAt) && (
        <>
          <Field label="Fired by" code value={node.spec.firedBy} onChange={spec('firedBy')} />
          <Field label="Enters at" code value={node.spec.entersAt} onChange={spec('entersAt')} />
        </>
      )}
      <Field label="Preconditions" value={node.spec.preconditions} onChange={spec('preconditions')} />
      <Field label="Inputs" value={node.spec.inputs} onChange={spec('inputs')} />
      <RulesField value={node.spec.rules} onChange={spec('rules')} />
      <Field label="Exceptions" value={node.spec.exceptions} onChange={spec('exceptions')} />
      <Field label="Outputs" value={node.spec.outputs} onChange={spec('outputs')} />
      <Field label="What the household sees" value={node.spec.householdSees} onChange={spec('householdSees')} />
      <Field label="Source" code value={node.spec.source} onChange={spec('source')} />

      <h5 className="pv-edit-group">For agents</h5>
      <Field label="Instructions" value={node.instructions} onChange={(v) => edit((n) => (n.instructions = v))} />
      <Field label="Decision criteria" value={node.decisionCriteria} onChange={(v) => edit((n) => (n.decisionCriteria = v))} />
      <Field label="Commands (what to run)" code value={node.commands} onChange={(v) => edit((n) => (n.commands = v))} />
      <Field label="Prompts (what to ask an agent)" value={node.prompts} onChange={(v) => edit((n) => (n.prompts = v))} />

      <PorterSettings node={node} onSet={(key, value) => edit((n) => {
        if (value === '') delete n.porter[key]
        else n.porter[key] = value
      })} />

      <h5 className="pv-edit-group">Who and with what</h5>
      <Field label="Owner" multiline={false} value={node.refs.owner} onChange={(v) => edit((n) => (n.refs.owner = v))} />
      <LinesField label="Roles" hint="One to a line." value={node.refs.roles} format={asLine} parse={fromLine} onChange={(v) => edit((n) => (n.refs.roles = v))} />
      <LinesField label="People" hint="One to a line." value={node.refs.people} format={asLine} parse={fromLine} onChange={(v) => edit((n) => (n.refs.people = v))} />
      <LinesField label="Software" hint="One to a line." value={node.refs.software} format={asLine} parse={fromLine} onChange={(v) => edit((n) => (n.refs.software = v))} />
      <LinesField label="Documents" hint="One to a line." value={node.refs.documents} format={asLine} parse={fromLine} onChange={(v) => edit((n) => (n.refs.documents = v))} />
      <LinesField
        label="Controls"
        code
        hint="One to a line: the control, then a bar and its framework (A.9.4 | ISO 27001)."
        value={node.refs.controls}
        format={controlLine}
        parse={controlFromLine}
        onChange={(controls) => edit((n) => (n.refs.controls = controls))}
      />

      {gateway && (
        <div className="pv-edit-ways">
          <h5 className="pv-edit-group">Ways out</h5>
          {ways.map((f) => (
            <WayOut
              key={f.id}
              flow={f}
              node={node}
              targets={process.nodes}
              onFlow={(fn) => apply(updateFlow(model, pid, f.id, fn))}
              onRemove={() => attempt(() => ({ model: removeWayOut(model, pid, f.id) }))}
              canRemove={whyNot(() => removeWayOut(model, pid, f.id))}
            />
          ))}
          <div className="pv-edit-addway">
            <label>
              New way out
              <input type="text" value={newWay} placeholder="Label" onChange={(e) => setNewWay(e.target.value)} />
            </label>
            <label>
              Leads to
              <select value={newTarget} onChange={(e) => setNewTarget(e.target.value)}>
                <option value="">A new end</option>
                {process.nodes.filter((n) => n.id !== node.id).map((n) => (
                  <option key={n.id} value={n.id}>
                    {nameOf(n)}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className="pv-btn pv-btn-quiet pv-btn-sm"
              onClick={() => {
                attempt(() => ({ model: addWayOut(model, pid, node.id, { name: newWay, targetId: newTarget || undefined }).model }))
                setNewWay('')
                setNewTarget('')
              }}
            >
              Add a way out
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
