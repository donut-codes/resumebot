// A process, drawn the way the upstream project's process page draws it: a "Process
// flow" board (the Living node-link graph, with zoom controls and Full
// screen) above a numbered "Steps" list (step-kind pills, branch sections
// with path numbers, Reading, Show details).
//
// Ported from the upstream project's frontend/src/pages/ProcessReadPage.tsx at
// commit 8c2e050, read-only. Cut: notes, findings, controls, teams,
// software, documents, people, edit mode and every edit affordance, Verify,
// Archive, Export BPMN, Print, the side card, routing. Everything between
// the two section headings is the upstream project's own logic, in its own order;
// see PROVENANCE.md for the full list of what was kept, cut and changed.
import { Fragment, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import './processView.css'
import type { ProcessEdge, ProcessStep } from './types'
import { RichText } from './richText'
import {
  analyzeFlow, buildSections, describeStartTrigger, describeTrigger, numberSteps, traceFrom,
  formatTimer, proseStates,
  type OutlineSection,
} from './processFlow'
import { stepKindBadge } from './processGraphKit'
import { byIdIn } from './dom'
import LivingFlow from './LivingFlow'

export { FLOW_MIN_ZOOM } from './LivingFlow'

/**
 * What a BPMN element carried that our core step type loses. Elements whose
 * meaning survives the mapping get no badge; anything we did not recognise
 * says so, so an unexplained step is visibly unexplained. Only the kinds the
 * view's BPMN reader hands on (bpmn/graph.ts `NOTED`) are here: who does a
 * service or script task now lives in the step's own text, so those kinds
 * have nothing to add.
 */
const BPMN_BADGES: Record<string, string> = {
  'bpmn:inclusiveGateway': 'Any that apply',
  'bpmn:eventBasedGateway': 'Whichever happens first',
  'bpmn:transaction': 'Transaction',
  'bpmn:adHocSubProcess': 'Ad-hoc',
}

const BPMN_UNDERSTOOD = new Set([
  'bpmn:task', 'bpmn:userTask', 'bpmn:manualTask', 'bpmn:exclusiveGateway',
  'bpmn:parallelGateway', 'bpmn:startEvent', 'bpmn:endEvent', 'bpmn:subProcess',
  'bpmn:sequenceFlow', 'bpmn:boundaryEvent', 'bpmn:intermediateCatchEvent',
  'bpmn:intermediateThrowEvent', 'bpmn:callActivity',
])

// A plain, always-present classification: the same "what kind of thing is
// this" the diagram answers with colour, stated in words for the reader who
// is looking at the outline instead.
const STEP_KIND_LABELS: Record<string, string> = {
  task: 'Task',
  gateway_exclusive: 'Decision',
  gateway_parallel: 'Decision',
  event_start: 'Start',
  event_end: 'End',
  subprocess: 'Subprocess',
  event_intermediate: 'Event',
  event_boundary: 'Event',
}

interface BpmnBadge {
  text: string
  unmapped: boolean
}

function bpmnBadge(step: ProcessStep): BpmnBadge | null {
  if (!step.bpmn_type) return null
  if (BPMN_BADGES[step.bpmn_type]) return { text: BPMN_BADGES[step.bpmn_type], unmapped: false }
  if (BPMN_UNDERSTOOD.has(step.bpmn_type)) return null
  return { text: `Not shown in full: ${step.bpmn_type.replace(/^bpmn:/, '')}`, unmapped: true }
}

// Attributes the page turns into something a reader can act on; the rest are
// listed under "Other details from the file".
const BPMN_ATTRS_INTERPRETED = new Set(['id', 'calledElement', 'assignee', 'candidateGroups', 'candidateUsers'])

function bpmnAttrEntries(step: ProcessStep): { key: string; value: string }[] {
  const attrs = step.bpmn_attrs
  if (!attrs) return []
  return Object.entries(attrs)
    .map(([k, v]) => ({ key: k.replace(/^\{[^}]*\}/, ''), value: String(v) }))
    .filter(({ key }) => key !== '__children__' && !BPMN_ATTRS_INTERPRETED.has(key))
}

const BPMN_CHILDREN_INTERPRETED = new Set([
  'messageEventDefinition', 'signalEventDefinition', 'timerEventDefinition',
  'errorEventDefinition', 'escalationEventDefinition', 'conditionalEventDefinition',
  'compensateEventDefinition', 'cancelEventDefinition', 'terminateEventDefinition',
])

function xmlLocalName(fragment: string): string | undefined {
  return /^\s*<(?:[\w.-]+:)?([\w.-]+)/.exec(fragment)?.[1]
}

function bpmnUnparsedChildNames(step: ProcessStep): string[] {
  const children = step.bpmn_attrs?.['__children__']
  if (!Array.isArray(children)) return []
  return children
    .map(c => (typeof c === 'string' ? xmlLocalName(c) : undefined))
    .filter((n): n is string => !!n && !BPMN_CHILDREN_INTERPRETED.has(n))
}

// What can fire an event, in the same words the derived sentence uses.
const EVENT_TRIGGER_LABELS: Record<string, string> = {
  timer: 'A timer expires',
  message: 'A message arrives',
  signal: 'A signal is raised',
  conditional: 'A condition is met',
  error: 'Something goes wrong',
  escalation: 'Someone escalates it',
  compensation: 'Completed work has to be undone',
  cancel: 'The transaction is cancelled',
  terminate: 'Everything stops here',
  link: 'Continues from elsewhere in the diagram',
}

/**
 * A default branch is the path taken when no condition matched, so it is not
 * itself a condition and reads wrong inside one: "If otherwise".
 */
const DEFAULT_PATH_LABELS = new Set([
  'otherwise', 'else', 'default', 'no match', 'none of the above', 'all other cases',
])

export function isDefaultPath(label: string): boolean {
  return DEFAULT_PATH_LABELS.has(label.trim().toLowerCase())
}

/** Puts a path's name inside a sentence: "Tax relevant" -> "If tax relevant". An acronym keeps its case. */
export function embedCondition(label: string): string {
  const [first, ...rest] = label.split(' ')
  if (!first) return label
  const isAcronym = first.length > 1 && first === first.toUpperCase()
  const head = isAcronym ? first : first.charAt(0).toLowerCase() + first.slice(1)
  return [head, ...rest].join(' ')
}

/**
 * Orders path handles the way a reader counts them: "2a" before "3a", and
 * "3a" before "10a". Digits compare numerically, letters alphabetically.
 */
export function comparePathNumbers(a: string, b: string): number {
  const split = (s: string) => s.match(/\d+|\D+/g) ?? []
  const ax = split(a)
  const bx = split(b)
  for (let i = 0; i < Math.max(ax.length, bx.length); i++) {
    const x = ax[i]
    const y = bx[i]
    if (x === undefined) return -1
    if (y === undefined) return 1
    if (x === y) continue
    const xNum = /^\d/.test(x)
    const yNum = /^\d/.test(y)
    if (xNum && yNum) return Number(x) - Number(y)
    return x < y ? -1 : 1
  }
  return 0
}

export interface ProcessViewProps {
  steps: ProcessStep[]
  edges: ProcessEdge[]
  /**
   * true (the default): the Living graph runs its live simulation, so a step
   * can be dragged. false: it settles once and stays still (used by tests,
   * which cannot have a timer left running).
   */
  live?: boolean
  /**
   * Extra, read-only detail a caller adds at the end of a step's opened detail (the "How they work" page puts
   * what the agent is sent here). Not in the upstream project's page; see PROVENANCE.md.
   */
  renderDetail?: (step: ProcessStep) => React.ReactNode
  /**
   * true: each step's actor (its performer) is on the step's row, under the title, so it reads without opening
   * the step, and is not repeated as the "Who" chip in the detail. Not in the upstream project's page; see PROVENANCE.md.
   */
  performerOnRows?: boolean
  /**
   * A running instance's place in the process (the live view): each step it is at ("current") or has passed
   * ("passed"). The current step is drawn and listed as "Here now", the passed ones as "Passed", and the page opens
   * on the current step. Not in the upstream project's page; see PROVENANCE.md.
   */
  marks?: Record<string, 'current' | 'passed'>
  /** What the current step's mark says; "Here now" unless the run has ended ("Ended here"). */
  currentWord?: string
  /**
   * Replaces the plain "Reading" label in the Steps header (the editor puts its Edit toggle there). Not in
   * the upstream project's page; see PROVENANCE.md.
   */
  modeControl?: React.ReactNode
  /** A step to select and open (the editor selects the step it just added). */
  focusStepId?: string | null
  /** Rows start open, not collapsed (the editor). */
  startExpanded?: boolean
}

export default function ProcessView({
  steps, edges, live = true, renderDetail, performerOnRows = false, marks, currentWord = 'Here now', modeControl,
  focusStepId = null, startExpanded = false,
}: ProcessViewProps) {
  const [highlightedStepId, setHighlightedStepId] = useState<string | null>(null)
  const [expandedDesignSteps, setExpandedDesignSteps] = useState<Set<string>>(new Set())
  const [expandedRawSteps, setExpandedRawSteps] = useState<Set<string>>(new Set())
  // Seeded with every step, so the page opens collapsed; a step leaves this
  // set when it's expanded.
  const [collapsedSteps, setCollapsedSteps] = useState<Set<string>>(() => startExpanded ? new Set() : new Set(steps.map(s => s.id)))
  // Every branch section starts open, like an unfolded code block.
  const [collapsedSections, setCollapsedSections] = useState<Set<string>>(new Set())
  const [graphExpanded, setGraphExpanded] = useState(false)
  // Counts the times a different process came in. The graph is drawn again from scratch for each (its `key`), which
  // lays it out afresh and frames it again, in live mode as well as settled.
  const [epoch, setEpoch] = useState(0)
  // Ids that are this view's own: the sections' ids and what points at them (two views on a page must not share).
  const uid = useId()
  const rootRef = useRef<HTMLDivElement>(null)

  // A different process starts collapsed again. A step added or removed in the same process (the editor) leaves the
  // others as they were: what was open stays open, what was selected stays selected while it exists, and a step
  // that is new starts collapsed unless it is the one in focus.
  const stepKey = steps.map(s => s.id).join('|')
  const previousIds = useRef<Set<string>>(new Set(steps.map(s => s.id)))
  useEffect(() => {
    const before = previousIds.current
    // The same process when most of its steps are still there.
    const same = steps.filter(s => before.has(s.id)).length >= Math.max(1, steps.length / 2)
    previousIds.current = new Set(steps.map(s => s.id))
    if (!same) {
      setCollapsedSteps(startExpanded ? new Set() : new Set(steps.map(s => s.id)))
      setHighlightedStepId(null)
      setEpoch(n => n + 1)
    } else {
      setCollapsedSteps(prev => new Set(steps.filter(s => (prev.has(s.id) || !before.has(s.id)) && !startExpanded && s.id !== focusStepId).map(s => s.id)))
      setHighlightedStepId(prev => (prev && steps.some(s => s.id === prev) ? prev : null))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stepKey])

  // The step the host asks to be shown (the one just added). It is asked for once: the request is used up when the
  // step is selected, so a later change to the steps (a move, a removal) does not snap the selection back to it. The
  // graph is not panned to it: its circle has no settled place yet, so the graph is fitted whole when the layout
  // has moved.
  const usedFocus = useRef<string | null>(null)
  useEffect(() => {
    if (!focusStepId) {
      usedFocus.current = null
      return
    }
    if (usedFocus.current === focusStepId || !steps.some(s => s.id === focusStepId)) return
    usedFocus.current = focusStepId
    if (highlightedStepId !== focusStepId) skipPan.current = true
    setHighlightedStepId(focusStepId)
    setCollapsedSteps(prev => { const next = new Set(prev); next.delete(focusStepId); return next })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusStepId, stepKey])

  // A live view opens on the step the run is at.
  const hereNow = marks ? Object.keys(marks).find(id => marks[id] === 'current') ?? null : null
  // The run opening the step is not a click: the whole graph stays fitted in its box instead of panning to it.
  const skipPan = useRef(false)
  useEffect(() => {
    if (hereNow && steps.some(s => s.id === hereNow) && hereNow !== highlightedStepId) {
      skipPan.current = true
      setHighlightedStepId(hereNow)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hereNow, stepKey])

  function toggleStepDetail(stepId: string) {
    const collapsing = !collapsedSteps.has(stepId)
    setCollapsedSteps(prev => {
      const next = new Set(prev)
      if (next.has(stepId)) next.delete(stepId)
      else next.add(stepId)
      return next
    })
    // Selection forces detail open, so collapsing the selected step by hand
    // has to drop the selection too, or the click appears to do nothing.
    if (collapsing && highlightedStepId === stepId) setHighlightedStepId(null)
  }

  function toggleIn(set: Set<string>, setter: (s: Set<string>) => void, id: string) {
    const next = new Set(set)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setter(next)
  }

  // Flow order + branch nesting come from the graph, not canvas position.
  const flow = useMemo(() => analyzeFlow(steps, edges), [steps, edges])
  const orderedSteps = flow.order

  const stepsById = useMemo(() => new Map(steps.map(s => [s.id, s])), [steps])

  // The main flow is one flat list; every branch, at whatever depth, is its
  // own flat list too, headed by its full breadcrumb -- "If $500 or more ->
  // If Approved" -- instead of by indentation.
  const sections = useMemo(
    () => buildSections(orderedSteps, flow.branchPathByStepId, flow.emptyBranches),
    [orderedSteps, flow]
  )

  // Folding is symmetric: a click opens or closes the one path you clicked.
  function toggleSection(key: string) {
    setCollapsedSections(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }
  // Numbers restart inside every path. A decision is real work and gets a
  // number like any other step; a terminal is a stopping point, not a step,
  // so it takes none.
  const numbering = useMemo(
    () => numberSteps(orderedSteps, flow.branchPathByStepId, flow.emptyBranches, s =>
      flow.terminalStepIds.has(s.id)),
    [orderedSteps, flow]
  )
  // The diagram numbers terminals too: a block at the end of an arrow has no
  // sentence to misread, and every other block carries a number.
  const diagramNumbering = useMemo(
    () => numberSteps(orderedSteps, flow.branchPathByStepId, flow.emptyBranches, () => false),
    [orderedSteps, flow]
  )
  const escapeNumberByStepId = useMemo(() => {
    const map = new Map<string, string>()
    sections.forEach(section => {
      if (!section.tag?.escape) return
      const edge = edges.find(e => e.id === section.tag!.edgeId)
      const event = edge ? steps.find(s => s.id === edge.source_step_id) : undefined
      const num = diagramNumbering.pathNumberByBranchKey.get(section.tag.branchKey)
      if (event && num) map.set(event.id, num)
    })
    return map
  }, [sections, diagramNumbering, edges, steps])
  // The number each circle in the graph wears, the same as its row in the list.
  const numberById = useMemo(() => {
    const map = new Map<string, string>()
    for (const s of steps) {
      const num = diagramNumbering.numberByStepId.get(s.id) ?? escapeNumberByStepId.get(s.id)
      if (num) map.set(s.id, num)
    }
    return map
  }, [steps, diagramNumbering, escapeNumberByStepId])

  // A rework edge belongs in "Comes from" and "Leads to" like any other, marked as a loop.
  const loopEdgesByStepId = useMemo(() => {
    const m = new Map<string, { into: { step: ProcessStep; label: string | null }[]; outOf: { step: ProcessStep; label: string | null }[] }>()
    const entry = (id: string) => {
      let e = m.get(id)
      if (!e) { e = { into: [], outOf: [] }; m.set(id, e) }
      return e
    }
    for (const e of edges) {
      if (!flow.backEdgeIds.has(e.id)) continue
      const source = stepsById.get(e.source_step_id)
      const target = stepsById.get(e.target_step_id)
      if (!source || !target) continue
      entry(target.id).into.push({ step: source, label: e.label ?? null })
      entry(source.id).outOf.push({ step: target, label: e.label ?? null })
    }
    return m
  }, [edges, stepsById, flow])

  // Every branch hangs off exactly one gateway, so it can be placed under it.
  const sectionsByGatewayId = useMemo(() => {
    const m = new Map<string, OutlineSection<ProcessStep>[]>()
    sections.forEach(s => {
      if (!s.tag) return
      const list = m.get(s.tag.gatewayId) ?? []
      list.push(s)
      m.set(s.tag.gatewayId, list)
    })
    m.forEach(list => list.sort((a, b) => a.tag!.siblingIndex - b.tag!.siblingIndex))
    return m
  }, [sections])

  // Any branch whose gateway never got rendered would otherwise vanish.
  const orphanOrder = useMemo(() => {
    const rank = (key: string) => numbering.pathNumberByBranchKey.get(key) ?? '~'
    return [...sections]
      .filter(s => s.key !== 'main')
      .sort((a, b) => comparePathNumbers(rank(a.key), rank(b.key)))
  }, [sections, numbering])

  const forkEdgeCount = useMemo(() => {
    const m = new Map<string, number>()
    edges.forEach(e => {
      m.set(e.source_step_id, (m.get(e.source_step_id) ?? 0) + 1)
    })
    return m
  }, [edges])

  // The first step of each branch a gateway opens. Those are already named by
  // the branch sections, so a fork's "Leads to" leaves them out.
  const branchHeadsByGatewayId = useMemo(() => {
    const m = new Map<string, Set<string>>()
    sections.forEach(sec => {
      const head = sec.steps[0]
      if (!sec.tag || !head) return
      const heads = m.get(sec.tag.gatewayId) ?? new Set<string>()
      heads.add(head.id)
      m.set(sec.tag.gatewayId, heads)
    })
    return m
  }, [sections])

  const allDetailsShown = collapsedSteps.size === 0

  function toggleAllDetails() {
    setCollapsedSteps(allDetailsShown ? new Set(orderedSteps.map(s => s.id)) : new Set())
  }

  // Selection drives which step shows its full detail; it isn't recorded in
  // collapsedSteps, so moving the selection elsewhere lets the previous step
  // fall back to whatever the reader had it set to.
  function handleStepClick(stepId: string) {
    setHighlightedStepId(prev => prev === stepId ? null : stepId)
  }

  // Every lookup is inside this view's own root: a second view on the page, or an id with a quote in it, changes
  // nothing here.
  const rowOf = (stepId: string) => byIdIn(rootRef.current, `step-${stepId}`)

  // Following a flow line has to actually go there: scroll, select, focus.
  function jumpToStep(stepId: string) {
    setHighlightedStepId(stepId)
    const row = rowOf(stepId)
    row?.scrollIntoView?.({ block: 'center' })
    row?.querySelector<HTMLElement>('.step-title-editable')?.focus()
  }

  function jumpToSection(key: string) {
    setCollapsedSections(prev => {
      const next = new Set(prev)
      next.delete(key)
      return next
    })
    byIdIn(rootRef.current, `branch-h-${uid}-${key}`)?.scrollIntoView?.({ block: 'center' })
  }

  // Move between steps from the keyboard: arrow keys walk the rendered order, Escape lets go. Only keys pressed
  // inside this view count (it takes focus when it is clicked), never ones typed into a box, and the arrows are
  // left to the controls inside an opened step.
  function onRootKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (!highlightedStepId) return
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp' && e.key !== 'Escape') return
    const el = e.target instanceof HTMLElement ? e.target : null
    if (el && (el.closest('input, textarea, select') || el.isContentEditable)) return
    if (e.key === 'Escape') { setHighlightedStepId(null); return }
    if (el?.closest('.step-detail-body')) return
    const order = orderedSteps.map(st => st.id)
    const i = order.indexOf(highlightedStepId)
    if (i === -1) return
    const next = order[e.key === 'ArrowDown' ? i + 1 : i - 1]
    if (!next) return
    e.preventDefault()
    setHighlightedStepId(next)
    rowOf(next)?.scrollIntoView?.({ block: 'nearest' })
  }

  // The step(s) immediately before and after the clicked one -- shown as
  // prose in the list, and echoed in the diagram by marking the same steps.
  const trace = useMemo(
    () => highlightedStepId
      ? traceFrom(highlightedStepId, flow.forwardOutgoingByStepId, flow.forwardIncomingByStepId)
      : null,
    [highlightedStepId, flow]
  )

  function stepName(s: ProcessStep): string {
    return s.label || s.description.slice(0, 60)
  }

  // "Comes from"/"Leads to" name a step by its number first -- the number is
  // the citable handle, the name is just there to confirm it's the right one.
  function stepRef(s: ProcessStep): string {
    const number = numbering.numberByStepId.get(s.id)
    return number ? `${number} · ${stepName(s)}` : stepName(s)
  }

  // `level` is the heading level for the step's title: a step inside a branch
  // sits under that branch's heading.
  function renderStepRow(step: ProcessStep, level: 3 | 4 = 3) {
    const TitleHeading = (level === 4 ? 'h4' : 'h3') as 'h3' | 'h4'
    const isHighlighted = step.id === highlightedStepId
    // The selected step always shows its detail, on top of whatever the
    // reader collapsed by hand.
    const isDetailOpen = isHighlighted || !collapsedSteps.has(step.id)
    const isFork = (forkEdgeCount.get(step.id) ?? 0) >= 2
    const mergeLabels = flow.mergeLabelsByStepId.get(step.id)
    // The paths this step opens, split by whether taking one is a decision or
    // a reaction. Both render as their own sections directly below.
    const openedSections = sectionsByGatewayId.get(step.id) ?? []
    const escapeSections = openedSections.filter(s => s.tag?.escape)
    const branchSections = openedSections.filter(s => !s.tag?.escape)
    const loops = loopEdgesByStepId.get(step.id)
    const loopsInto = loops?.into ?? []
    const loopsOutOf = loops?.outOf ?? []
    const hasSeparateDescription = !!(step.label && step.description)
    const isDesignOpen = expandedDesignSteps.has(step.id)
    const isRawOpen = expandedRawSteps.has(step.id)

    // One labelled row for a free-text field; empty fields are not part of the record.
    function textRow(label: string, value: string | null | undefined) {
      if (!value) return null
      return (
        <div key={label} className="step-field-row">
          <dt>{label}</dt>
          <dd><span className="step-field-value">{value}</span></dd>
        </div>
      )
    }

    const number = numbering.numberByStepId.get(step.id)
    const isTerminal = flow.terminalStepIds.has(step.id)
    // A terminate end event cancels every other still-running branch too.
    const isTerminateEnd = isTerminal && step.event_trigger === 'terminate'
    const badge = bpmnBadge(step)
    const triggerText = describeTrigger(step)
    const startTriggerText = step.step_type === 'event_start' ? describeStartTrigger(step) : undefined
    const isEventStep = step.step_type === 'event_start'
      || step.step_type === 'event_intermediate'
      || step.step_type === 'event_end'
    const isPredecessor = !isHighlighted && (trace?.predecessorIds.has(step.id) ?? false)
    const isSuccessor = !isHighlighted && (trace?.successorIds.has(step.id) ?? false)

    const resolveSteps = (ids: string[]) =>
      ids.map(id => stepsById.get(id)).filter((s): s is ProcessStep => !!s)
    // A merge point's "Paths rejoin here" already names what feeds it.
    const comesFrom = mergeLabels && mergeLabels.length > 1
      ? []
      : resolveSteps(flow.forwardIncomingByStepId.get(step.id) ?? [])
    const branchHeads = branchHeadsByGatewayId.get(step.id)
    const leadsTo = resolveSteps(
      (flow.forwardOutgoingByStepId.get(step.id) ?? []).filter(id => !branchHeads?.has(id))
    )

    const attrs = bpmnAttrEntries(step)
    const unread = bpmnUnparsedChildNames(step)
    const designFields: [string, string | null | undefined][] = [
      ['Business objective', step.business_objective],
      ['Source requirement', step.source_requirement],
      ['Validity conditions', step.validity_conditions],
      ['Decision rights', step.decision_rights],
      ['Alternate / exception flows', step.alternate_flows],
      ['Provenance', step.provenance],
      ['Change rationale', step.change_rationale],
    ]
    const hasDesign = designFields.some(([, v]) => !!v)
    const designRows = [
      badge ? (
        <div key="bpmn-behaviour" className="step-field-row">
          <dt>How it behaves</dt>
          <dd>
            <span className={`step-bpmn-badge ${badge.unmapped ? 'step-bpmn-badge-unmapped' : ''}`}>{badge.text}</span>
          </dd>
        </div>
      ) : null,
      // Where a derived line already states the trigger in English, the raw
      // value is not repeated.
      step.event_trigger
        && !proseStates(
          startTriggerText ?? triggerText,
          step.event_trigger_detail ?? undefined,
          step.event_trigger === 'timer' && step.event_trigger_detail
            ? formatTimer(step.event_trigger_detail, step.event_trigger_subtype)
            : undefined,
        )
        ? textRow('Trigger detail', step.event_trigger_detail) : null,
      isEventStep && step.event_trigger
        ? textRow('Fires when', EVENT_TRIGGER_LABELS[step.event_trigger] ?? step.event_trigger) : null,
      textRow('Duration', step.expected_duration),
      textRow('Input data', step.input_description),
      textRow('Output data', step.output_description),
      textRow('Evidence', step.evidence_pointer),
      textRow('Prompts', step.prompts),
    ].filter(Boolean)

    return (
      <div
        key={step.id}
        id={`step-${step.id}`}
        className={[
          'step-row', isHighlighted ? 'step-row-highlighted' : '',
          marks?.[step.id] ? `step-row-run-${marks[step.id]}` : '',
        ].filter(Boolean).join(' ')}
        // A click selects the step, or lets go of it, from its header; a click inside its opened detail (a box being
        // typed in, a button, text being read) is not a click on the step.
        onClick={e => {
          if (e.target instanceof Element && e.target.closest('.step-detail-body')) return
          handleStepClick(step.id)
        }}
      >
        <button
          type="button"
          className="step-detail-toggle"
          aria-label={`${isDetailOpen ? 'Collapse' : 'Expand'} details for ${step.label || step.description.slice(0, 40)}`}
          aria-expanded={isDetailOpen}
          onClick={e => { e.stopPropagation(); toggleStepDetail(step.id) }}
        >
          {isDetailOpen ? '−' : '+'}
        </button>
        <div
          className={[
            'step-number',
            isTerminal ? 'step-number-end' : '',
            isTerminateEnd ? 'step-number-terminate' : '',
          ].filter(Boolean).join(' ')}
          title={isTerminateEnd ? 'Ends the whole process -- cancels every other running path' : (isTerminal ? 'End of this path' : undefined)}
        >
          {isTerminal ? '' : number}
          <span className="pv-visually-hidden">{isTerminal ? '' : `Step ${number}.`}</span>
        </div>
        {(isPredecessor || isSuccessor) && (
          <span
            className={`step-trace-arrow ${isPredecessor ? 'step-trace-arrow-up' : 'step-trace-arrow-down'}`}
            title={isPredecessor ? 'Comes before the selected step' : 'Comes after the selected step'}
          >
            {isPredecessor ? '↑' : '↓'}
          </span>
        )}
        <div className="step-content">
          <div className="step-title-row">
            {/* The heading stays a heading; the activator is a nested button. */}
            <TitleHeading className="step-title">
              <button type="button" className="step-title-editable">
                {isTerminateEnd && <span className="step-title-kind step-title-kind-terminate">Ends the whole process: </span>}
                {isTerminal && !isTerminateEnd && <span className="step-title-kind">End of this path: </span>}
                {step.label || step.description.slice(0, 60)}
              </button>
            </TitleHeading>
            {/* One group on the right, so a run's mark sits just left of the kind tag on every row. */}
            <div className={`step-title-tags${marks ? ' step-title-tags-run' : ''}`}>
              {marks?.[step.id] && (
                <span className={`step-run-mark step-run-mark-${marks[step.id]}`}>
                  {marks[step.id] === 'current' ? currentWord : 'Passed'}
                </span>
              )}
              {STEP_KIND_LABELS[step.step_type] && (
                <span className={`step-bpmn-badge${stepKindBadge(step.step_type) ? ` step-bpmn-badge-${stepKindBadge(step.step_type)}` : ''}`}>
                  {STEP_KIND_LABELS[step.step_type]}
                </span>
              )}
            </div>
          </div>

          {performerOnRows && step.performer && <p className="step-actor-line">{step.performer}</p>}

          {isDetailOpen && (
            <div className="step-detail-body">
              {/* Read from the graph, not stored on the step: where it sits in the flow. */}
              <div className="step-derived">
                {(comesFrom.length > 0 || loopsInto.length > 0) && (
                  <div className="step-derived-line">
                    ↳ Comes from:{' '}
                    {comesFrom.map(f => (
                      <span key={f.id} className="flow-target">
                        <button
                          type="button"
                          className="flow-target-jump"
                          aria-label={`Go to ${stepRef(f)}, which leads to ${stepRef(step)}`}
                          onClick={e => { e.stopPropagation(); jumpToStep(f.id) }}
                        >{stepRef(f)}</button>
                      </span>
                    ))}
                    {loopsInto.map(l => (
                      <span key={l.step.id} className="step-loop-ref">
                        {comesFrom.length > 0 ? ', or ' : ''}↻ back from{' '}
                        <button
                          type="button"
                          className="flow-target-jump"
                          aria-label={`Go to ${stepRef(l.step)}, the rework path from ${stepRef(step)}`}
                          onClick={e => { e.stopPropagation(); jumpToStep(l.step.id) }}
                        >{stepRef(l.step)}</button>
                        {l.label && <> — {l.label}</>}
                      </span>
                    ))}
                  </div>
                )}
                {(leadsTo.length > 0 || loopsOutOf.length > 0) && (
                  <div className="step-derived-line">
                    ↳ Leads to:{' '}
                    {leadsTo.map(t => (
                      <span key={t.id} className="flow-target">
                        <button
                          type="button"
                          className="flow-target-jump"
                          aria-label={`Go to ${stepRef(t)}, which ${stepRef(step)} leads to`}
                          onClick={e => { e.stopPropagation(); jumpToStep(t.id) }}
                        >{stepRef(t)}</button>
                      </span>
                    ))}
                    {loopsOutOf.map(l => (
                      <span key={l.step.id} className="step-loop-ref">
                        {leadsTo.length > 0 ? ', or ' : ''}↻ back to{' '}
                        <button
                          type="button"
                          className="flow-target-jump"
                          aria-label={`Go to ${stepRef(l.step)}, the rework path from ${stepRef(step)}`}
                          onClick={e => { e.stopPropagation(); jumpToStep(l.step.id) }}
                        >{stepRef(l.step)}</button>
                        {l.label && <> — {l.label}</>}
                      </span>
                    ))}
                  </div>
                )}
                {/* A decision's branches are named by their own sections. */}
                {branchSections.length > 0 && (
                  <div className="step-derived-line">
                    ↳ Leads to {step.step_type === 'gateway_parallel' ? 'all of' : 'one of'}:{' '}
                    {branchSections.map((sec, i) => (
                      <span key={sec.key}>
                        {i > 0 ? ', ' : ''}
                        <button
                          type="button"
                          className="flow-target-jump"
                          onClick={e => { e.stopPropagation(); jumpToSection(sec.key) }}
                        >
                          {numbering.pathNumberByBranchKey.get(sec.key)} · {sec.tag!.label}
                        </button>
                      </span>
                    ))}
                  </div>
                )}
                {escapeSections.length > 0 && (
                  <div className="step-derived-line">
                    ↳ Can also leave here if:{' '}
                    {escapeSections.map((sec, i) => (
                      <span key={sec.key}>
                        {i > 0 ? ', ' : ''}
                        <button
                          type="button"
                          className="flow-target-jump"
                          onClick={e => { e.stopPropagation(); jumpToSection(sec.key) }}
                        >
                          {numbering.pathNumberByBranchKey.get(sec.key)} · {sec.tag!.label}
                        </button>
                      </span>
                    ))}
                  </div>
                )}
                {mergeLabels && mergeLabels.length > 1 && step.step_type !== 'gateway_parallel' && (
                  <div className="step-derived-line">↳ Paths rejoin here: {mergeLabels.join(', ')}</div>
                )}
                {step.step_type === 'gateway_parallel' && mergeLabels && mergeLabels.length > 1 && (
                  <div className="step-derived-line">↳ Waits for all {mergeLabels.length} tracks to finish</div>
                )}
                {step.step_type === 'event_intermediate' && triggerText && (
                  <div className="step-derived-line">⏳ {triggerText}</div>
                )}
                {startTriggerText && (
                  <div className="step-derived-line">▶ Starts: {startTriggerText}</div>
                )}
                {step.bpmn_type === 'bpmn:callActivity' && (
                  <div className="step-derived-line">↳ Calls another process</div>
                )}
              </div>

              {isFork && step.decision_criteria && (
                <dl className="step-decision-criteria">
                  {textRow('Decision criteria', step.decision_criteria)}
                </dl>
              )}
              {/* Description -- shown when there's a separate label (otherwise description IS the title) */}
              {hasSeparateDescription && <p className="step-description">{step.description}</p>}
              {step.instructions && (
                <div className="step-description step-instructions">
                  <RichText text={step.instructions} />
                </div>
              )}

              {step.performer && !performerOnRows && (
                <div className="step-chips">
                  <span className="chip-group-label">Who</span>
                  <span className="step-chip step-chip-role">{step.performer}</span>
                </div>
              )}
              {step.action?.why && <p className="step-description">{step.action.why}</p>}
              {renderDetail?.(step)}

              {designRows.length > 0 && <dl className="step-design-body">{designRows}</dl>}

              {(attrs.length > 0 || unread.length > 0) && (
                <div className="step-design">
                  <button
                    type="button"
                    className="step-design-toggle"
                    onClick={e => { e.stopPropagation(); toggleIn(expandedRawSteps, setExpandedRawSteps, step.id) }}
                  >
                    {isRawOpen ? 'Hide other details from the file' : 'Other details from the file'}
                  </button>
                  {isRawOpen && (
                    <div className="step-bpmn-attrs">
                      {attrs.length > 0 && (
                        <>
                          <div className="step-bpmn-attrs-label">Kept from the file as it was written, not interpreted</div>
                          <pre className="step-bpmn-attrs-raw">
                            {attrs.map(({ key, value }) => `${key}: ${value}`).join('\n')}
                          </pre>
                        </>
                      )}
                      {unread.length > 0 && (
                        <p className="step-bpmn-attrs-note">
                          {attrs.length > 0 ? 'Plus ' : ''}
                          Not read from the file: {unread.join(', ')}.
                        </p>
                      )}
                    </div>
                  )}
                </div>
              )}

              {hasDesign && (
                <div className="step-design">
                  <button
                    type="button"
                    className="step-design-toggle"
                    onClick={e => { e.stopPropagation(); toggleIn(expandedDesignSteps, setExpandedDesignSteps, step.id) }}
                  >
                    {isDesignOpen ? 'Hide reasoning' : 'Why this step exists'}
                  </button>
                  {isDesignOpen && (
                    <dl className="step-design-body">
                      {designFields.map(([label, value]) => textRow(label, value))}
                    </dl>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    )
  }

  // What an escape path (a boundary event) says about itself, read-only.
  function renderEscapeDetail(event: ProcessStep, host: ProcessStep) {
    const hostNumber = numbering.numberByStepId.get(host.id) ?? ''
    return (
      <dl className="escape-detail">
        <div className="step-field-row">
          <dt>Name</dt>
          <dd>{event.label || event.description}</dd>
        </div>
        <div className="step-field-row">
          <dt>{`Effect on step ${hostNumber}`}</dt>
          <dd>{event.interrupting ? `Step ${hostNumber} stops` : `Step ${hostNumber} carries on as well`}</dd>
        </div>
        {event.event_trigger && (
          <div className="step-field-row">
            <dt>Fires when</dt>
            <dd>{describeTrigger(event) ?? EVENT_TRIGGER_LABELS[event.event_trigger] ?? event.event_trigger}</dd>
          </div>
        )}
        {event.event_trigger && event.event_trigger_detail && (
          <div className="step-field-row">
            <dt>Fires on</dt>
            <dd><code className="source-ref">{event.event_trigger_detail}</code></dd>
          </div>
        )}
      </dl>
    )
  }

  function renderSection(section: OutlineSection<ProcessStep>, body: React.ReactNode) {
    const tag = section.tag!
    const pathNumber = numbering.pathNumberByBranchKey.get(tag.branchKey)
    // Depth is stated in the breadcrumb, not indentation.
    const breadcrumbLabel = section.path.map(t => t.label).join(' → ')
    const generatedName = /^(Path|Track) \d+$/.test(tag.label)
    // Every path reads as the condition that takes you down it.
    const heading = tag.parallel && !tag.escape
      ? <>At the same time: <strong>{breadcrumbLabel}</strong></>
      : (generatedName || isDefaultPath(tag.label)) && !tag.escape
        ? <strong>{breadcrumbLabel}</strong>
        : <>If <strong>{embedCondition(breadcrumbLabel)}</strong></>
    const isOpen = !collapsedSections.has(section.key)
    // A collapsed branch gives no hint that a traced step is hiding inside it,
    // so the header carries the same up/down mark its traced children would.
    const sectionIsPredecessor = !isOpen && section.steps.some(s => trace?.predecessorIds.has(s.id) ?? false)
    const sectionIsSuccessor = !isOpen && section.steps.some(s => trace?.successorIds.has(s.id) ?? false)

    // A branch is a landmark with a name: a section labelled by its own
    // heading, holding a panel the header controls.
    const headingId = `branch-h-${uid}-${section.key}`
    const panelId = `branch-p-${uid}-${section.key}`

    // An escape path leaves via a boundary event, the source of the edge the branch is keyed on.
    const escapeEdge = tag.escape ? edges.find(e => e.id === tag.edgeId) : undefined
    const escapeEvent = escapeEdge ? stepsById.get(escapeEdge.source_step_id) : undefined
    const escapeHost = escapeEvent?.attached_to_step_id ? stepsById.get(escapeEvent.attached_to_step_id) : undefined

    return (
      <section key={section.key} className="outline-section" aria-labelledby={headingId}>
        <h3 className="outline-section-title" id={headingId}>
          <button
            type="button"
            className={[
              'outline-section-header',
              tag.parallel ? 'outline-section-parallel' : '',
            ].filter(Boolean).join(' ')}
            aria-expanded={isOpen}
            aria-controls={panelId}
            onClick={() => toggleSection(section.key)}
          >
            <span className="outline-section-fold" aria-hidden="true">{isOpen ? '−' : '+'}</span>
            {pathNumber && <span className="outline-section-number">{pathNumber}</span>}
            {sectionIsPredecessor && (
              <span className="step-trace-arrow step-trace-arrow-up" title="A step this branch leads to comes before the selected step">↑</span>
            )}
            {sectionIsSuccessor && (
              <span className="step-trace-arrow step-trace-arrow-down" title="A step in this branch comes after the selected step">↓</span>
            )}
            <span className="outline-section-heading">{heading}</span>
            {/* Every step wears a kind badge next to its title; a branch heading gets one too. */}
            <span className={[
              'step-bpmn-badge',
              'step-bpmn-badge-end',
              escapeEvent && stepKindBadge(escapeEvent.step_type) ? `step-bpmn-badge-${stepKindBadge(escapeEvent.step_type)}` : 'step-bpmn-badge-path',
            ].filter(Boolean).join(' ')}>
              {escapeEvent && STEP_KIND_LABELS[escapeEvent.step_type] ? STEP_KIND_LABELS[escapeEvent.step_type] : 'Path'}
            </span>
          </button>
        </h3>
        <div id={panelId} hidden={!isOpen}>
          {escapeEvent && renderEscapeDetail(escapeEvent, escapeHost ?? escapeEvent)}
          {section.steps.length === 0
            ? <p className="outline-section-empty">No steps documented for this path.</p>
            : body}
        </div>
      </section>
    )
  }

  if (steps.length === 0) return null

  return (
    // tabIndex -1: a click anywhere inside takes focus here, so the arrow keys and Escape reach this view's own
    // handler (and nobody else's).
    <div ref={rootRef} className="process-view" tabIndex={-1} onKeyDown={onRootKeyDown}>
      <section id={`flow-${uid}`}>
        <div className="section-header">
          <h2>Process flow</h2>
          <div className="section-header-actions">
            <button
              type="button"
              className="pv-btn pv-btn-quiet pv-btn-sm"
              onClick={() => setGraphExpanded(v => !v)}
            >{graphExpanded ? 'Collapse' : 'Full screen'}</button>
          </div>
        </div>
        <LivingFlow
          key={epoch}
          steps={steps}
          edges={edges}
          backEdgeIds={flow.backEdgeIds}
          live={live}
          expanded={graphExpanded}
          selectedId={highlightedStepId}
          trace={trace}
          numberById={numberById}
          marks={marks}
          skipPan={skipPan}
        />
      </section>

      {orderedSteps.length > 0 && (
        <section id={`steps-${uid}`}>
          <div className="section-header">
            <h2>Steps</h2>
            {/* The line is always in the layout and only its visibility changes,
                so the header never grows on the first click. */}
            <p
              className={`steps-keyhint ${highlightedStepId ? '' : 'steps-keyhint-idle'}`}
              aria-hidden={!highlightedStepId}
            >
              <kbd>↑</kbd><kbd>↓</kbd> move · <kbd>Esc</kbd> deselect
            </p>
            <div className="section-header-actions">
              {modeControl ?? <span className="mode-toggle">Reading</span>}
              <label className="show-details-toggle">
                <input type="checkbox" checked={allDetailsShown} onChange={toggleAllDetails} />
                Show details
              </label>
            </div>
          </div>
          <div className="step-list">
            {(() => {
              // A step, then whatever branches leave it, then the next step.
              // Every section renders at the same left edge with its full
              // breadcrumb, because depth is stated in words, not indentation.
              const emitted = new Set<string>()
              const stepsOf = (section: OutlineSection<ProcessStep>): React.ReactNode =>
                section.steps.map(s => (
                  <Fragment key={s.id}>
                    {renderStepRow(s, section.key === 'main' ? 3 : 4)}
                    {(sectionsByGatewayId.get(s.id) ?? []).map(child => sectionTree(child))}
                  </Fragment>
                ))
              // Guards against a malformed graph pointing a branch back into
              // its own subtree: a section is emitted at most once.
              const sectionTree = (section: OutlineSection<ProcessStep>): React.ReactNode => {
                if (emitted.has(section.key)) return null
                emitted.add(section.key)
                return <Fragment key={section.key}>{renderSection(section, stepsOf(section))}</Fragment>
              }
              const main = sections.find(s => s.key === 'main')
              // Eager: stepsOf populates `emitted`, so the orphan check below
              // sees everything the main line already placed.
              const mainNodes = main ? stepsOf(main) : null
              const orphans = orphanOrder.filter(s => !emitted.has(s.key)).map(sectionTree)
              return <>{mainNodes}{orphans}</>
            })()}
          </div>
        </section>
      )}
    </div>
  )
}
