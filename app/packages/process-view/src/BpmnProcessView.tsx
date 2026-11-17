// A BPMN process, shown and edited as text. Takes the BPMN file as text; draws the Process flow graph and the Steps
// list from it; and, when `editable`, lets the person edit each step's words in the list, with the graph following
// live. It never saves: every edit goes out as the new file through `onChange`, and `onValidate` lets the host
// check it (with its own engine) while it is being written.
//
// Who holds the draft: the view does. `bpmn` is the file it starts from, not the draft. While there are edits the
// view keeps the edited text itself and reports each new text through `onChange`; the host may keep it, feed it back
// as `bpmn`, or ignore it. Discard goes back to the file the edits started from. See the README for the contract.
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { processToGraph } from './bpmn/graph'
import { BpmnParseError, type BpmnModel, type BpmnNode, type BpmnProcess } from './bpmn/model'
import { parseBpmn } from './bpmn/parse'
import { serializeBpmn } from './bpmn/serialize'
import { byIdIn } from './dom'
import ProcessView from './ProcessView'
import StepDetail from './StepDetail'
import StepEditor from './StepEditor'
import type { ProcessStep } from './types'

export interface Validation {
  ok: boolean
  /** In plain words, one to a line, what the host's engine refused. */
  problems: string[]
}

export interface BpmnProcessViewProps {
  /**
   * The BPMN file, as text: the file the view starts from. A new value while nothing is edited replaces what is
   * shown. A different value while there are edits is not taken (the edits stay, with a note saying the process
   * changed); the person's Discard then shows it. The value the view itself last reported through `onChange` is never
   * a new file, so a host may feed `onChange` straight back into `bpmn`.
   */
  bpmn: string
  /** Which process of the file to show; the first when not given. An id the file does not have is said, not guessed. */
  processId?: string
  /** Shows the Edit toggle. Without it the view only reads. When it turns false, editing ends and unsent edits are dropped. */
  editable?: boolean
  /** The new file after every edit, and the file again on Discard. Called with the whole text, never a patch. */
  onChange?: (bpmnXml: string) => void
  /**
   * Asks the host whether the edited file is one it can run. Called when editing starts (for the file as loaded) and
   * after each edit, a moment after typing stops. Only the answer to the latest call is shown. May return the answer
   * or a promise of it; one that throws, rejects, or has no `problems` is shown as a check that could not be run or as
   * a plain refusal, never as a check that never ends.
   */
  onValidate?: (bpmnXml: string) => Promise<Validation> | Validation
  /** A running instance's place (the live view): each step it is at ("current") or has passed ("passed"). Wins over `currentStepId`. */
  marks?: Record<string, 'current' | 'passed'>
  /** What the current step's mark says; "Here now" unless the run has ended (the host passes "Ended here"). */
  currentWord?: string
  /** The step a run is at, when the host knows it by id instead of through `marks`. */
  currentStepId?: string
  /** true (the default): the graph runs its live simulation, so a step can be dragged. false: it settles once and stays still (tests, a run's view). */
  live?: boolean
  /** true: each step's actor is on its row, under the title, instead of a "Who" chip inside the opened step. */
  performerOnRows?: boolean
  /** More for the host to add under a step's own detail (read or edit mode). `editing` says which mode the view is in. `node` is undefined for a step the file has no element for. */
  renderDetail?: (step: ProcessStep, node: BpmnNode | undefined, editing: boolean) => ReactNode
  /** Said when the editor starts or stops (also when it stops because `editable` turned false). */
  onEditingChange?: (editing: boolean) => void
  /** Shown beside the Edit toggle, e.g. the host's Save button. */
  actions?: ReactNode
  /** false when the host shows a decision's ways out in its own detail (read mode only; the editor always has them). */
  showWays?: boolean
  /** true to also show each step's engineering specification (off by default: it is not for the household). */
  showSpec?: boolean
}

function tryParse(bpmn: string): { model: BpmnModel | null; error: string | null } {
  try {
    return { model: parseBpmn(bpmn), error: null }
  } catch (e) {
    if (e instanceof BpmnParseError) return { model: null, error: e.message }
    throw e
  }
}

/** The view's draft: the file it started from, and the text it has now. */
interface Draft {
  /** The last `bpmn` the host gave, handled. */
  seen: string
  /** The file the edits began from. */
  start: string
  /** The draft's text as it stands: `start` while nothing is edited. */
  xml: string
  model: BpmnModel | null
  error: string | null
  /** The texts this view reported (or began from) lately. A host handing one of them back is not giving a new file. */
  recent: string[]
  /** A different file the host gave while there were edits; shown after Discard. */
  foreign: string | null
}

const RECENT = 8
const remember = (recent: string[], xml: string) => [...recent.filter((x) => x !== xml), xml].slice(-RECENT)

/** A draft that starts from `file`, with nothing edited. */
function begin(file: string, seen: string, recent: string[] = []): Draft {
  return { seen, start: file, xml: file, ...tryParse(file), recent: remember(recent, file), foreign: null }
}

/** The draft after the host gives `bpmn`. */
function fromHost(d: Draft, bpmn: string): Draft {
  if (bpmn === d.start) return { ...d, seen: bpmn, foreign: null }
  // Its own text handed back (the host feeding `onChange` into `bpmn`): nothing new.
  if (bpmn === d.xml || d.recent.includes(bpmn)) return { ...d, seen: bpmn }
  // A different file. With nothing edited it simply replaces what is shown; with edits the edits win.
  if (d.xml === d.start) return begin(bpmn, bpmn, d.recent)
  return { ...d, seen: bpmn, foreign: bpmn }
}

function cleanValidation(result: unknown): Validation {
  const r = (result ?? {}) as Partial<Validation>
  return {
    ok: r.ok === true,
    problems: Array.isArray(r.problems) ? r.problems.filter((p): p is string => typeof p === 'string') : [],
  }
}

const COULD_NOT_CHECK: Validation = { ok: false, problems: ['The check could not be run.'] }

export default function BpmnProcessView({
  bpmn, processId, editable = false, onChange, onValidate, marks, currentWord, currentStepId, live = true,
  performerOnRows = false, renderDetail, onEditingChange, actions, showWays = true, showSpec = false,
}: BpmnProcessViewProps) {
  const rootRef = useRef<HTMLDivElement>(null)
  const [draft, setDraft] = useState<Draft>(() => begin(bpmn, bpmn))
  // The host's file moved on: take it, or note it, before anything is drawn from the old one.
  if (bpmn !== draft.seen) setDraft(fromHost(draft, bpmn))
  const { model, error } = draft
  const edited = draft.xml !== draft.start

  const [wantsEditing, setWantsEditing] = useState(false)
  const editing = editable && wantsEditing
  const [focusId, setFocusId] = useState<string | null>(null)
  const nameFocus = useRef<string | null>(null)
  const [rowFocus, setRowFocus] = useState<string | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [checked, setChecked] = useState<{ xml: string; result: Validation | null; busy: boolean } | null>(null)

  const process: BpmnProcess | undefined = useMemo(
    () => (processId ? model?.processes.find((p) => p.id === processId) : model?.processes[0]),
    [model, processId],
  )
  const graph = useMemo(() => {
    if (!process) return { steps: [], edges: [] }
    const g = processToGraph(process)
    // A step whose name was cleared is "Unnamed step", not the kind of element it is.
    const named = new Map(process.nodes.map((n) => [n.id, n.name.trim() !== '']))
    return { ...g, steps: g.steps.map((s) => (named.get(s.id) === false ? { ...s, label: 'Unnamed step' } : s)) }
  }, [process])
  const nodes = useMemo(() => new Map((process?.nodes ?? []).map((n) => [n.id, n])), [process])

  const runMarks = useMemo(() => {
    if (marks) return marks
    return currentStepId ? { [currentStepId]: 'current' as const } : undefined
  }, [marks, currentStepId])

  // --- checking with the host ------------------------------------------------------------------------------------
  // One check counts: the latest asked for. An earlier one that is still out is dropped when it comes back.
  const latestCheck = useRef(0)
  const checkTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const validateRef = useRef(onValidate)
  validateRef.current = onValidate
  const stopChecking = () => {
    clearTimeout(checkTimer.current)
    latestCheck.current++
  }
  const check = (xml: string, delay: number) => {
    if (!validateRef.current) return
    stopChecking()
    const mine = latestCheck.current
    setChecked({ xml, result: null, busy: true })
    const finish = (result: Validation) => {
      if (latestCheck.current === mine) setChecked({ xml, result, busy: false })
    }
    checkTimer.current = setTimeout(() => {
      let pending: Promise<unknown>
      try {
        pending = Promise.resolve(validateRef.current?.(xml))
      } catch (e) {
        pending = Promise.reject(e)
      }
      pending.then((result) => finish(cleanValidation(result)), () => finish(COULD_NOT_CHECK))
    }, delay)
  }
  useEffect(() => stopChecking, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Editing starts: the file as loaded is checked once, so the person knows where they stand before the first edit.
  useEffect(() => {
    if (editing && onValidate && (!checked || checked.xml !== draft.xml)) check(draft.xml, 0)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing])

  // --- editing ---------------------------------------------------------------------------------------------------
  const apply = (next: BpmnModel, focus?: string, where: 'name' | 'title' = 'name') => {
    const xml = serializeBpmn(next)
    let out: Draft
    let sent = xml
    if (xml === draft.start && draft.foreign !== null) {
      // Back to exactly where it began, while the file has moved on: nothing is edited, so the new file shows.
      sent = draft.foreign
      out = begin(draft.foreign, draft.seen, draft.recent)
    } else {
      out = { ...draft, model: next, xml, recent: remember(draft.recent, xml) }
    }
    setDraft(out)
    // The step to show next: the one just added (its Name takes the cursor), or after a removal the one that follows.
    // Anything else asks for nothing, so an earlier request is never repeated.
    nameFocus.current = focus && where === 'name' ? focus : null
    setFocusId(focus ?? null)
    if (focus && where === 'title') setRowFocus(focus)
    onChange?.(sent)
    check(sent, 350)
  }

  // After a removal the cursor goes to the next step's title in the list, not nowhere.
  useEffect(() => {
    if (!rowFocus) return
    byIdIn(rootRef.current, `step-${rowFocus}`)?.querySelector<HTMLElement>('.step-title-editable')?.focus()
    setRowFocus(null)
  }, [rowFocus])

  const discard = () => {
    const target = draft.foreign ?? draft.start
    stopChecking()
    setDraft(begin(target, bpmn, draft.recent))
    setChecked(null)
    setProblem(null)
    setFocusId(null)
    nameFocus.current = null
    onChange?.(target)
  }

  const toggle = () => setWantsEditing((v) => !v)

  // Said when the editor starts or stops, whichever way: the toggle, or `editable` turning false.
  const reported = useRef(false)
  useEffect(() => {
    if (reported.current === editing) return
    reported.current = editing
    onEditingChange?.(editing)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing])

  // The host took editing away: it ends, and edits not sent are dropped (there is nowhere left to send them from).
  useEffect(() => {
    if (editable) return
    setWantsEditing(false)
    if (edited) discard()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editable])

  if (error) {
    return (
      <p role="alert" className="pv-error">
        This is not a BPMN file the view can read: {error}
      </p>
    )
  }
  if (!model) return null
  if (!process) {
    return (
      <p role="alert" className="pv-error">
        {processId ? `This file has no process called “${processId}”.` : 'This file has no process in it.'}
      </p>
    )
  }

  const detail = (step: ProcessStep) => {
    const node = nodes.get(step.id)
    if (!node) return renderDetail?.(step, undefined, editing) ?? null
    return (
      <>
        {editing ? (
          <StepEditor
            model={model}
            process={process}
            node={node}
            apply={apply}
            onProblem={setProblem}
            autoFocusName={nameFocus.current === step.id}
            onNameFocused={() => {
              nameFocus.current = null
            }}
          />
        ) : (
          <StepDetail node={node} process={process} showWays={showWays} showSpec={showSpec} />
        )}
        {renderDetail?.(step, node, editing)}
      </>
    )
  }

  const modeControl = editable ? (
    <span className="pv-mode">
      <button type="button" className="mode-toggle pv-edit-toggle" aria-pressed={editing} onClick={toggle}>
        {editing ? 'Editing' : 'Edit'}
      </button>
      {edited && (
        <button type="button" className="pv-btn pv-btn-quiet pv-btn-sm" onClick={discard}>
          Discard edits
        </button>
      )}
      {actions}
    </span>
  ) : undefined

  // The file changed under the edits: said where the edits are, with what to do about it.
  const changedMeanwhile = edited && draft.foreign !== null

  return (
    <div ref={rootRef} className={`pv-root${editing ? ' pv-editing' : ''}`}>
      {(editing || changedMeanwhile) && (
        <div className="pv-edit-status" role="status">
          {editing && <p>Edit the words in the list. The graph follows as you type; nothing is saved until you save it.</p>}
          {changedMeanwhile && (
            <p className="pv-problem">This process changed since you started editing. Discard your edits to see the new version.</p>
          )}
          {editing && problem && <p className="pv-problem">{problem}</p>}
          {editing && checked?.busy && <p className="pv-muted">Checking it can run&hellip;</p>}
          {editing && checked && !checked.busy && checked.result?.ok && <p className="pv-ok">It can run as written.</p>}
          {editing && checked && !checked.busy && checked.result && !checked.result.ok && (
            <div className="pv-problem">
              <p>It can't run as written{checked.result.problems.length > 0 ? ':' : '.'}</p>
              {checked.result.problems.length > 0 && (
                <ul>
                  {checked.result.problems.map((p, i) => (
                    <li key={i}>{p}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
      <ProcessView
        steps={graph.steps}
        edges={graph.edges}
        live={live}
        renderDetail={detail}
        performerOnRows={performerOnRows}
        marks={runMarks}
        currentWord={currentWord}
        modeControl={modeControl}
        focusStepId={focusId}
      />
    </div>
  )
}
