/**
 * Turns a process version's steps + edges into a readable, branch-aware outline.
 *
 * Deliberately free of React and API types so it can be exercised directly
 * against synthetic graphs. Canvas position is only a tie-breaker: order comes
 * from the graph itself, because sorting by position interleaves steps of
 * branches that sit side by side on the canvas.
 *
 * The outline is flat, not nested: the main flow is one list, and every branch
 * -- at whatever depth -- is its own separate list, headed by a breadcrumb and
 * a number handle ("2b" / "2b.1a"). Depth is stated in words, not indentation,
 * so nothing here needs a rendering tree, an indent budget, or a rule for which
 * branch is "important enough" to skip tinting. A branch with real steps on it
 * always gets a heading. A branch whose walk tagged nothing -- an "otherwise"
 * edge that lands straight on a step already reached another way -- gets no
 * heading at all: there is nothing under it to anchor one to, and a heading
 * with an empty list beneath it reads as a step that silently vanished. The
 * merge it skips to already names the path in its own "reached via" labels
 * (mergeLabelsByStepId), and the gateway's own edge keeps its label on the
 * canvas -- so the path is still documented, just not as a heading with
 * nothing under it.
 */

export interface FlowStep {
  id: string
  step_type: string
  position_x: number
  position_y: number
  /** Set on a boundary event: the activity it hangs off. */
  attached_to_step_id?: string | null
  /** false = the host activity keeps running as well (BPMN cancelActivity). */
  interrupting?: boolean
  event_trigger?: string | null
  event_trigger_detail?: string | null
  /** For a timer: 'duration', 'date' or 'cycle'. Absent for other triggers. */
  event_trigger_subtype?: string | null
}

/**
 * Steps that no longer hang off the flow at all.
 *
 * The outline is derived by walking edges forward from the start, so a step
 * with nothing leading into it is not rendered anywhere -- it is still in the
 * database, but it has silently left the document. That makes deleting an edge
 * a destructive act on a compliance record: repoint the only edge into a step
 * and the step disappears, with nothing on screen to undo it from. Callers use
 * this to refuse that edit rather than perform it.
 *
 * Reachability is measured from start events specifically, not from "whatever
 * has no incoming edge" -- under that definition the step you just orphaned
 * would qualify as a root and the check would always pass. A process with no
 * start event at all falls back to the no-incoming set, so an unimported or
 * half-built graph doesn't report every step as orphaned.
 *
 * A boundary event is exempt: it never has incoming flow by design, and rides
 * on its host's reachability.
 */
export function unreachableStepIds<S extends FlowStep>(steps: S[], edges: FlowEdge[]): Set<string> {
  const byId = new Map(steps.map(s => [s.id, s]))
  const outgoing = new Map<string, string[]>()
  const hasIncoming = new Set<string>()
  for (const e of edges) {
    if (!byId.has(e.source_step_id) || !byId.has(e.target_step_id)) continue
    outgoing.set(e.source_step_id, [...(outgoing.get(e.source_step_id) ?? []), e.target_step_id])
    hasIncoming.add(e.target_step_id)
  }

  const starts = steps.filter(s => s.step_type === 'event_start')
  const roots = starts.length
    ? starts
    : steps.filter(s => !hasIncoming.has(s.id) && !s.attached_to_step_id)

  const seen = new Set<string>()
  const queue = roots.map(r => r.id)
  while (queue.length) {
    const id = queue.shift()!
    if (seen.has(id)) continue
    seen.add(id)
    for (const target of outgoing.get(id) ?? []) queue.push(target)
  }
  // A boundary event is reached through the activity it hangs off, not through
  // the flow -- and its own escape path is walked from it once it is in.
  let grew = true
  while (grew) {
    grew = false
    for (const s of steps) {
      if (seen.has(s.id)) continue
      if (s.attached_to_step_id && seen.has(s.attached_to_step_id)) {
        seen.add(s.id)
        for (const target of outgoing.get(s.id) ?? []) queue.push(target)
        grew = true
      }
    }
    while (queue.length) {
      const id = queue.shift()!
      if (seen.has(id)) continue
      seen.add(id)
      for (const target of outgoing.get(id) ?? []) queue.push(target)
      grew = true
    }
  }

  return new Set(steps.filter(s => !seen.has(s.id)).map(s => s.id))
}

export interface FlowEdge {
  id: string
  source_step_id: string
  target_step_id: string
  label?: string | null
  condition?: string | null
}

export interface BranchTag {
  /** Unique per branch of a fork: `${gatewayId}:${edgeId}`. */
  branchKey: string
  gatewayId: string
  /**
   * The edge this branch leaves by. Its label is where the branch's name
   * lives -- "If Tax relevant" is the edge's label, not any step's field --
   * so renaming a path means patching this edge.
   */
  edgeId: string
  /** Condition text where the model has one, otherwise a generated name. */
  label: string
  /** Index of this branch within its fork — used for generated labels
      ('Path 2') and the number handle's letter. Not a colour key. */
  siblingIndex: number
  /** AND-split (all paths run) rather than XOR-split (one path runs). */
  parallel: boolean
  /**
   * The path leaves via a boundary event rather than a decision: something
   * happened (a timer expired, a signal arrived), nobody chose it. The
   * distinction matters in the heading -- "If ..." claims a choice was made
   * at the host step, which for an escape is simply untrue.
   */
  escape?: boolean
}

/** A branch whose target is immediately a merge point: a straight skip to the
 *  rejoin, not a bug, and not rendered as its own section (buildSections
 *  folds it away -- there is nothing under a heading for it to show). Still
 *  tracked here so numberSteps can give it an addressable path number and so
 *  the edge that leaves the gateway keeps its own label on the canvas. Carries
 *  its own parent path since, having no steps, it has no entry in
 *  branchPathByStepId. */
export interface EmptyBranch {
  tag: BranchTag
  parentPath: BranchTag[]
}

export interface FlowAnalysis<S extends FlowStep> {
  /** Topological order: every edge's source before its target. */
  order: S[]
  /** Outermost → innermost branch containing the step. Empty for main-line steps. */
  branchPathByStepId: Map<string, BranchTag[]>
  /** Branches with no steps of their own. */
  emptyBranches: EmptyBranch[]
  /** For steps where 2+ paths rejoin: which paths arrive there. */
  mergeLabelsByStepId: Map<string, string[]>
  /** Steps that something loops back to (rework cycles). */
  loopTargetIds: Set<string>
  /** Edges that close a cycle — excluded from ordering so loops can't deadlock it. */
  backEdgeIds: Set<string>
  /** Boundary events hanging off each activity, in canvas order. */
  boundaryEventsByHostId: Map<string, S[]>
  /** Steps nothing flows out of — where a path stops, not where it is interrupted. */
  terminalStepIds: Set<string>
  /** Forward adjacency, for tracing what leads to or follows from a step. */
  forwardOutgoingByStepId: Map<string, string[]>
  forwardIncomingByStepId: Map<string, string[]>
  /** Decisions -- walked through during a trace rather than treated as a one-hop stop; see traceFrom. */
  gatewayIds: Set<string>
}

function byPosition(a: FlowStep, b: FlowStep): number {
  return a.position_y - b.position_y || a.position_x - b.position_x
}

/** Which flat section a step belongs to -- two steps share one iff this matches. */
function pathKey(path: BranchTag[]): string {
  return path.map(t => t.branchKey).join('>')
}

/** ISO 8601 durations are what BPMN timers carry; say them in English. */
/**
 * A repeating interval: `R3/PT10M` is "every 10 minutes, 3 times", `R/P1D` is
 * "every day" with no limit. Read as a duration it says nothing at all, which
 * is what it used to be rendered as.
 */
export function formatCycle(raw: string): string {
  const match = /^R(\d*)\/(P.+)$/.exec(raw.trim())
  if (!match) return raw
  const [, count, period] = match
  const every = `every ${formatDuration(period)}`
  return count ? `${every}, ${count} time${count === '1' ? '' : 's'}` : every
}

/**
 * A timer's value stated the way its kind means it. All three used to be
 * described as a deadline, so a fixed date read as "not done within
 * 2026-12-31" and a recurrence as "not done within R3/PT10M".
 */
export function formatTimer(detail: string, subtype?: string | null): string {
  if (subtype === 'cycle') return formatCycle(detail)
  if (subtype === 'date') return detail.trim()
  return formatDuration(detail)
}

export function formatDuration(raw: string): string {
  const match = /^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$/.exec(raw.trim())
  if (!match) return raw
  const [, days, hours, minutes] = match
  const parts: string[] = []
  if (days) parts.push(`${days} day${days === '1' ? '' : 's'}`)
  if (hours) parts.push(`${hours} hour${hours === '1' ? '' : 's'}`)
  if (minutes) parts.push(`${minutes} minute${minutes === '1' ? '' : 's'}`)
  return parts.length ? parts.join(' ') : raw
}

/**
 * Whether a trigger detail is something a reader can understand, or a machine
 * identifier they can't.
 *
 * BPMN references a message or signal by id -- `messageRef="Message_1"` -- and
 * modelling tools generate those ids automatically, so most real files carry
 * `Message_1` or `Signal_a4f2` rather than anything a person wrote. Printing
 * one in a sentence produces "Starts: Message received: Message_1", which
 * tells a reader nothing and looks like the tool is leaking its own plumbing.
 *
 * The identity is already on the page: the step is titled "Invoice received"
 * or "Fraud flag raised". So the sentence only has to say what kind of thing
 * fires it, and an opaque ref is dropped rather than printed. Prose the author
 * actually typed -- anything with a space in it -- is kept, because then the
 * detail is the meaning rather than a pointer to it. The raw value is never
 * lost: it is still the editable field, shown while editing.
 */
/**
 * Whether a sentence already tells the reader what a field holds.
 *
 * The derived lines are a *rendering* of the step's own fields --
 * describeTrigger reads event_trigger, its detail and its subtype and returns
 * prose. So a field row beside its own sentence is the same datum twice: once
 * raw, once rendered. But which fields are covered is not fixed, because the
 * prose deliberately omits some of them (a generated ref never appears in a
 * sentence, and must therefore keep its row).
 *
 * Hence a comparison rather than a list of fields to hide. A hand-maintained
 * list rots the moment a sentence changes or a field is added; asking "does
 * the prose already contain this value" stays true on its own. The formatted
 * form counts too: "5 days" in the sentence covers a P5D field.
 */
export function proseStates(sentence: string | undefined, value: string | undefined, formatted?: string): boolean {
  if (!sentence || !value) return false
  const haystack = sentence.toLowerCase()
  const candidates = [value, formatted].filter((v): v is string => !!v && v.trim().length > 0)
  return candidates.some(v => haystack.includes(v.trim().toLowerCase()))
}

export function isReadableDetail(detail: string): boolean {
  // Anything with a space in it is a phrase somebody wrote.
  if (/\s/.test(detail)) return true
  // A single plain word is too ("Fraud"). Underscores and digits are what
  // separate a written name from a generated handle: `Signal_fraud`, `E_101`,
  // `Message_1` are all references, not descriptions.
  return /^[A-Za-z][A-Za-z-]*$/.test(detail)
}

/**
 * A readable name for what fires an event — used as the heading of the escape
 * path a boundary event opens, so the outline reads "When not done in 5 days".
 */
export function describeTrigger(step: FlowStep): string | undefined {
  const raw = step.event_trigger_detail?.trim()
  // A duration is meaningful once formatted, so it is exempt from the prose
  // test -- "P5D" is opaque, "5 days" is not.
  const detail = raw && isReadableDetail(raw) ? raw : undefined
  const subtype = step.event_trigger_subtype
  switch (step.event_trigger) {
    case 'timer':
      if (!raw) return 'Takes too long'
      // A deadline, an instant and a recurrence are three different things
      // happening, and only the first is "not done within".
      if (subtype === 'date') return `Not done by ${formatTimer(raw, subtype)}`
      if (subtype === 'cycle') return `Every time it comes round — ${formatTimer(raw, subtype)}`
      return `Not done within ${formatTimer(raw, subtype)}`
    // These read to whoever opens the process, not to whoever modelled it, so
    // they say what happens in words rather than naming the BPMN construct.
    case 'error':
      return detail ? `Something goes wrong: ${detail}` : 'Something goes wrong'
    case 'escalation':
      return detail ? `Escalated: ${detail}` : 'Someone escalates it'
    case 'message':
      return detail ? `A message arrives: ${detail}` : 'A message arrives'
    case 'signal':
      return detail ? `A signal is raised: ${detail}` : 'A signal is raised'
    case 'conditional':
      return detail ? `A condition is met: ${detail}` : 'A condition is met'
    case 'compensation':
      return 'Completed work has to be undone'
    case 'cancel':
      return 'The transaction is cancelled'
    // The importer can write these two (TRIGGER_ELEMENTS in bpmn_service.py)
    // and this switch did not know them, so they fell through to undefined --
    // no sentence at all for a value that was correctly imported.
    case 'terminate':
      return 'Everything stops here'
    case 'link':
      return 'Continues from elsewhere in the diagram'
    default:
      return undefined
  }
}

/**
 * What kicks the whole process off. Reuses describeTrigger's wording for the
 * cases that read the same from either end ("On message received" is true
 * whether it starts a process or interrupts an activity) -- except timer,
 * which describeTrigger phrases as a deadline ("Not done within 5 days").
 * That's wrong for a start: a start timer is a schedule, not a thing being
 * waited out. Error/escalation/compensation/cancel can't fire a start event
 * at all, so those fall through to undefined rather than showing nonsense.
 */
export function describeStartTrigger(step: FlowStep): string | undefined {
  if (step.event_trigger === 'timer') {
    const detail = step.event_trigger_detail?.trim()
    const subtype = step.event_trigger_subtype
    if (!detail) return 'On a schedule'
    // A start timer is most often a date or a cycle; a duration start means
    // "this long after the model says go", which is rare but expressible.
    if (subtype === 'date') return `On ${formatTimer(detail, subtype)}`
    if (subtype === 'cycle') return `On a schedule: ${formatCycle(detail)}`
    // Duration, or unstated -- which the migration backfills to duration, and
    // which is what every row held before the subtype existed. It must not
    // fall through to the cycle wording: that is the conflation being fixed,
    // and formatCycle would hand back the raw ISO string unformatted.
    return `${formatTimer(detail, 'duration')} after the process is due to begin`
  }
  if (step.event_trigger === 'message' || step.event_trigger === 'signal' || step.event_trigger === 'conditional') {
    return describeTrigger(step)
  }
  return undefined
}

export function analyzeFlow<S extends FlowStep>(steps: S[], edges: FlowEdge[]): FlowAnalysis<S> {
  const stepById = new Map(steps.map(s => [s.id, s]))

  // A boundary event hangs off an activity and has no incoming sequence flow.
  // It is never a row in the main flow and must never be seeded as a process
  // start -- it belongs to its host.
  const boundaryEventsByHostId = new Map<string, S[]>()
  for (const s of steps) {
    const host = s.attached_to_step_id
    if (!host || !stepById.has(host) || host === s.id) continue
    const list = boundaryEventsByHostId.get(host) ?? []
    list.push(s)
    boundaryEventsByHostId.set(host, list)
  }
  for (const list of boundaryEventsByHostId.values()) list.sort(byPosition)

  const attachedIds = new Set<string>()
  for (const list of boundaryEventsByHostId.values()) {
    for (const event of list) attachedIds.add(event.id)
  }

  // Flow leaving a boundary event is re-based onto its host, so the escape path
  // is reachable and reads as an alternate path out of the activity itself.
  const escapeEdgeOrigin = new Map<string, S>()
  const flowEdges: FlowEdge[] = []
  for (const e of edges) {
    if (!stepById.has(e.source_step_id) || !stepById.has(e.target_step_id)) continue
    if (attachedIds.has(e.target_step_id)) continue // nothing flows into a boundary event
    if (attachedIds.has(e.source_step_id)) {
      const event = stepById.get(e.source_step_id)!
      const host = event.attached_to_step_id!
      escapeEdgeOrigin.set(e.id, event)
      flowEdges.push({ ...e, source_step_id: host })
      continue
    }
    flowEdges.push(e)
  }

  const outgoing = new Map<string, FlowEdge[]>()
  const incoming = new Map<string, FlowEdge[]>()
  for (const e of flowEdges) {
    const outs = outgoing.get(e.source_step_id) ?? []
    outs.push(e)
    outgoing.set(e.source_step_id, outs)
    const ins = incoming.get(e.target_step_id) ?? []
    ins.push(e)
    incoming.set(e.target_step_id, ins)
  }

  const flowSteps = steps.filter(s => !attachedIds.has(s.id))

  // Branch order follows the canvas reading order of each branch's first step,
  // so sibling numbering matches what the author laid out.
  for (const outs of outgoing.values()) {
    outs.sort((x, y) => {
      const a = stepById.get(x.target_step_id)
      const b = stepById.get(y.target_step_id)
      if (!a || !b) return 0
      return byPosition(a, b)
    })
  }

  const backEdgeIds = findBackEdges(flowSteps, outgoing, incoming)

  const forwardOuts = (id: string) => (outgoing.get(id) ?? []).filter(e => !backEdgeIds.has(e.id))
  const forwardIns = (id: string) => (incoming.get(id) ?? []).filter(e => !backEdgeIds.has(e.id))

  const incomingCount = new Map<string, number>()
  for (const s of flowSteps) incomingCount.set(s.id, forwardIns(s.id).length)

  // Plain adjacency (step id -> neighbour ids), forward edges only, for tracing
  // what leads to or follows from an arbitrary step -- the thing a diagram
  // would show at a glance, and the flat outline can't without help, since a
  // fork's branches no longer sit visually near each other.
  const forwardOutgoingByStepId = new Map<string, string[]>()
  const forwardIncomingByStepId = new Map<string, string[]>()
  for (const s of flowSteps) {
    forwardOutgoingByStepId.set(s.id, forwardOuts(s.id).map(e => e.target_step_id))
    forwardIncomingByStepId.set(s.id, forwardIns(s.id).map(e => e.source_step_id))
  }

  // A valid topological order already visits every branch's own chain in its
  // correct relative sequence (Kahn's algorithm never emits an edge's target
  // before its source) -- so, unlike a nested rendering, the flat outline below
  // needs no further reshuffling to keep a branch's steps in order.
  const order = flowOrder(flowSteps, stepById, forwardOuts, incomingCount)
  const { branchPathByStepId, emptyBranches } = tagBranches(
    order, stepById, forwardOuts, forwardIns, incomingCount, escapeEdgeOrigin
  )

  const orderIndex = new Map(order.map((s, i) => [s.id, i]))
  const mergeLabelsByStepId = new Map<string, string[]>()
  const loopTargetIds = new Set<string>()
  for (const s of flowSteps) {
    const ins = incoming.get(s.id) ?? []
    if (ins.some(e => backEdgeIds.has(e.id))) loopTargetIds.add(s.id)
    const fwd = ins
      .filter(e => !backEdgeIds.has(e.id))
      // Read in the order the branches themselves appear in the outline.
      .sort((x, y) => (orderIndex.get(x.source_step_id) ?? 0) - (orderIndex.get(y.source_step_id) ?? 0))
    if (fwd.length < 2) continue
    // Name each arriving path relative to where this step sits, so a path that
    // starts several branches up still reads in context ("Sev 2 › Yes") rather
    // than as a bare condition the reader can't place.
    const ownPath = branchPathByStepId.get(s.id) ?? []
    const labels = fwd.map(e => {
      const srcPath = branchPathByStepId.get(e.source_step_id) ?? []
      const suffix = srcPath.slice(ownPath.length).map(t => t.label)
      if (suffix.length) return suffix.join(' › ')
      // A branch edge straight off the gateway carries the only naming there is.
      const event = escapeEdgeOrigin.get(e.id)
      const own = (event && describeTrigger(event)) || e.label?.trim() || e.condition?.trim()
      return own || 'Main path'
    })
    mergeLabelsByStepId.set(s.id, [...new Set(labels)])
  }

  const terminalStepIds = new Set<string>()
  for (const s of flowSteps) {
    if (forwardOuts(s.id).length === 0) terminalStepIds.add(s.id)
  }

  return {
    order, branchPathByStepId, emptyBranches, mergeLabelsByStepId, loopTargetIds,
    backEdgeIds, boundaryEventsByHostId, terminalStepIds,
    forwardOutgoingByStepId, forwardIncomingByStepId,
    gatewayIds: new Set(flowSteps.filter(s => s.step_type.startsWith('gateway')).map(s => s.id)),
  }
}

export interface StepTrace {
  predecessorIds: Set<string>
  successorIds: Set<string>
}

/**
 * The step(s) immediately before and after `stepId`, so a click can "light
 * up" a step's neighbours the way a diagram would -- useful specifically at a
 * fork or a merge, where the flat outline's sections no longer sit near each
 * other on the page. Direct neighbours only, not the whole up/downstream
 * chain: a click should show what's adjacent, not repaint half the process.
 *
 * One hop in each direction, gateways included.
 *
 * Successors used to be walked through a fork, on the reasoning that "the
 * diamond alone doesn't say which one matters" -- clicking the step before a
 * four-way split would otherwise light up only the tiny gateway between them.
 * That was true while a decision was an unnumbered router. It stopped being
 * true when a decision became real work with a number and a name: "Leads to:
 * 3 - Decision: Which checks apply?" is a complete answer, and step 3 states
 * its own branches directly beneath it.
 *
 * So the walk-through was buying a second hop the reader could already see,
 * and paying for it in noise -- selecting the step before a three-way fork
 * marked six rows, of which three were two hops away. That is the same
 * "unrelated extra hop" objection that already kept predecessors from
 * crossing a gateway; it applies identically in the other direction once the
 * gateway is a step in its own right. Adjacency now means adjacency, and the
 * rule is symmetric, which is also what it looks like on the page.
 */
export function traceFrom(
  stepId: string,
  forwardOutgoingByStepId: Map<string, string[]>,
  forwardIncomingByStepId: Map<string, string[]>,
): StepTrace {
  return {
    predecessorIds: new Set(forwardIncomingByStepId.get(stepId) ?? []),
    successorIds: new Set(forwardOutgoingByStepId.get(stepId) ?? []),
  }
}

/** Iterative DFS colouring: an edge into a step already on the stack closes a cycle. */
function findBackEdges<S extends FlowStep>(
  steps: S[],
  outgoing: Map<string, FlowEdge[]>,
  incoming: Map<string, FlowEdge[]>,
): Set<string> {
  const backEdgeIds = new Set<string>()
  const color = new Map<string, 0 | 1 | 2>()

  const roots = [
    ...steps.filter(s => (incoming.get(s.id) ?? []).length === 0).sort(byPosition),
    // Components made only of a cycle have no zero-incoming entry point.
    ...steps.slice().sort(byPosition),
  ]

  for (const root of roots) {
    if ((color.get(root.id) ?? 0) !== 0) continue
    const stack: { id: string; next: number }[] = [{ id: root.id, next: 0 }]
    color.set(root.id, 1)
    while (stack.length) {
      const frame = stack[stack.length - 1]
      const outs = outgoing.get(frame.id) ?? []
      if (frame.next >= outs.length) {
        color.set(frame.id, 2)
        stack.pop()
        continue
      }
      const edge = outs[frame.next++]
      const target = edge.target_step_id
      const c = color.get(target) ?? 0
      if (c === 1) backEdgeIds.add(edge.id)
      else if (c === 0) {
        color.set(target, 1)
        stack.push({ id: target, next: 0 })
      }
    }
  }
  return backEdgeIds
}

/**
 * Kahn's algorithm with a LIFO frontier: a step is emitted only once every
 * forward path into it has been emitted, and a fork's first branch is fully
 * explored before its second starts.
 */
function flowOrder<S extends FlowStep>(
  steps: S[],
  stepById: Map<string, S>,
  forwardOuts: (id: string) => FlowEdge[],
  incomingCount: Map<string, number>,
): S[] {
  const pending = new Map(incomingCount)
  const emitted: S[] = []
  const seen = new Set<string>()

  const stack = steps
    .filter(s => (pending.get(s.id) ?? 0) === 0)
    .sort(byPosition)
    .map(s => s.id)
    .reverse()

  while (stack.length) {
    const id = stack.pop()!
    if (seen.has(id)) continue
    const step = stepById.get(id)
    if (!step) continue
    seen.add(id)
    emitted.push(step)

    const ready: string[] = []
    for (const edge of forwardOuts(id)) {
      const target = edge.target_step_id
      if (!pending.has(target)) continue
      const left = (pending.get(target) ?? 0) - 1
      pending.set(target, left)
      if (left <= 0 && !seen.has(target)) ready.push(target)
    }
    // Reverse so the first branch of a fork is explored before the second.
    for (let i = ready.length - 1; i >= 0; i--) stack.push(ready[i])
  }

  // Steps stranded inside a cycle, or not connected to any start.
  const leftovers = steps.filter(s => !seen.has(s.id)).sort(byPosition)
  return [...emitted, ...leftovers]
}

/** Longest common prefix of several branch paths, compared by branchKey. */
function commonBranchPrefix(paths: BranchTag[][]): BranchTag[] {
  if (paths.length === 0) return []
  const first = paths[0]
  let shared = 0
  while (shared < first.length && paths.every(p => p[shared]?.branchKey === first[shared].branchKey)) {
    shared++
  }
  return first.slice(0, shared)
}

/**
 * Works out which branch (or nest of branches) each step sits inside, and
 * records any branch that never got a step of its own.
 *
 * Two rules, applied in flow order so a step's inputs are always resolved
 * first:
 *  - a fork tags the chain of steps reachable only through each of its
 *    branches, carrying the fork's own context down as the parent path;
 *  - any other step inherits the context common to every path arriving at it.
 *    That keeps a merge that only closes an inner fork inside the outer branch,
 *    while a merge where genuinely separate branches rejoin lands back on the
 *    main line.
 */
function tagBranches<S extends FlowStep>(
  order: S[],
  stepById: Map<string, S>,
  forwardOuts: (id: string) => FlowEdge[],
  forwardIns: (id: string) => FlowEdge[],
  incomingCount: Map<string, number>,
  escapeEdgeOrigin: Map<string, S>,
): { branchPathByStepId: Map<string, BranchTag[]>; emptyBranches: EmptyBranch[] } {
  const branchPathByStepId = new Map<string, BranchTag[]>()
  const emptyBranches: EmptyBranch[] = []

  for (const gateway of order) {
    if (!branchPathByStepId.has(gateway.id)) {
      const ins = forwardIns(gateway.id)
      if (ins.length) {
        const prefix = commonBranchPrefix(ins.map(e => branchPathByStepId.get(e.source_step_id) ?? []))
        if (prefix.length) branchPathByStepId.set(gateway.id, prefix)
      }
    }

    const outs = forwardOuts(gateway.id)
    const escapes = outs.filter(e => escapeEdgeOrigin.has(e.id))
    // An activity with a boundary event is not a fork in its own right: only the
    // escape paths branch off, while the ordinary continuation stays on the
    // main line. A real gateway forks on all of its outgoing flows.
    const branchEdges = escapes.length ? escapes : (outs.length >= 2 ? outs : [])
    if (!branchEdges.length) continue
    const gatewayIsParallel = gateway.step_type === 'gateway_parallel'
    const parentPath = branchPathByStepId.get(gateway.id) ?? []

    branchEdges.forEach((edge, index) => {
      const event = escapeEdgeOrigin.get(edge.id)
      // A non-interrupting boundary event leaves the host running too, which is
      // exactly the semantics the parallel heading already expresses.
      const parallel = event ? event.interrupting === false : gatewayIsParallel
      const label = event
        ? (describeTrigger(event) ||
           edge.label?.trim() ||
           `Interruption ${index + 1}`)
        : (edge.label?.trim() ||
           edge.condition?.trim() ||
           (parallel ? `Track ${index + 1}` : `Path ${index + 1}`))
      const tag: BranchTag = {
        branchKey: `${gateway.id}:${edge.id}`,
        gatewayId: gateway.id,
        edgeId: edge.id,
        label,
        siblingIndex: index,
        parallel,
        escape: !!event,
      }

      let cursor: string | undefined = edge.target_step_id
      let tagged = 0
      let guard = 0
      while (cursor && guard++ < 500) {
        // A step several paths reach is shared ground, not part of this branch.
        if ((incomingCount.get(cursor) ?? 0) >= 2) break
        if (branchPathByStepId.has(cursor)) break
        if (!stepById.has(cursor)) break
        branchPathByStepId.set(cursor, [...parentPath, tag])
        tagged++
        const next = forwardOuts(cursor)
        // Stop at a nested fork (it is tagged, but its own branches are handled
        // when that gateway comes up) or at a dead end.
        if (next.length !== 1) break
        cursor = next[0].target_step_id
      }

      if (tagged === 0) emptyBranches.push({ tag, parentPath })
    })
  }

  return { branchPathByStepId, emptyBranches }
}

export interface OutlineSection<S extends FlowStep> {
  /** 'main' for the top-level flow, else the innermost branch's key. */
  key: string
  /** undefined only for 'main'. */
  tag?: BranchTag
  /** Full outermost → innermost path, for a breadcrumb heading on nested branches. */
  path: BranchTag[]
  /** This branch's own steps, in reading order. Never empty -- a branch with
   *  nothing on it gets no section at all; see buildSections. */
  steps: S[]
}

/**
 * Groups steps into one flat section per path -- the main flow, plus one
 * section per branch, at whatever depth, in first-encountered order. A branch
 * is never split across two sections even if its steps are non-contiguous in
 * `order` (a branch that itself forks and later rejoins resumes as the same
 * section on the far side of its own nested fork). Each section's own heading
 * states its full breadcrumb, so nothing depends on physical position on the
 * page to be understood.
 *
 * `emptyBranches` (an "otherwise" edge whose walk tagged no step at all --
 * `tagBranches`'s ordinary "skip to the merge" shape) is accepted but not
 * rendered into a section: a heading with an empty list under it reads as a
 * step that quietly disappeared, on a normal, non-broken shape ("skip this
 * step, continue on"). Folding it away costs nothing a reader depended on --
 * the merge it rejoins already names the path in its own arrival labels
 * (`mergeLabelsByStepId`), and the edge that leaves the gateway keeps its own
 * label on the diagram itself. A branch that tags at least one real step
 * still gets its own section exactly as before.
 */
export function buildSections<S extends FlowStep>(
  order: S[],
  branchPathByStepId: Map<string, BranchTag[]>,
  _emptyBranches: EmptyBranch[],
): OutlineSection<S>[] {
  const sections = new Map<string, OutlineSection<S>>()
  const sectionOrder: string[] = []

  const ensureSection = (key: string, tag: BranchTag | undefined, path: BranchTag[]) => {
    let section = sections.get(key)
    if (!section) {
      section = { key, tag, path, steps: [] }
      sections.set(key, section)
      sectionOrder.push(key)
    }
    return section
  }

  ensureSection('main', undefined, [])

  for (const step of order) {
    const path = branchPathByStepId.get(step.id) ?? []
    const tag = path[path.length - 1]
    const key = tag ? tag.branchKey : 'main'
    ensureSection(key, tag, path).steps.push(step)
  }

  // Empty branches (_emptyBranches) never reach here as a section: nothing
  // was tagged for them, so there is nothing to head a section with. They
  // stay addressable via numberSteps and named via mergeLabelsByStepId and
  // the gateway edge's own label -- just not as a heading over an empty list.

  return sectionOrder.map(k => sections.get(k)!)
}

export interface OutlineNumbering {
  /** Display number per step: "3", "4a.1", "4a.2b.1". Absent for unnumbered steps. */
  numberByStepId: Map<string, string>
  /** Handle for each path: "4a", "4b" — shown on the path's heading. */
  pathNumberByBranchKey: Map<string, string>
}

/**
 * Numbers steps so the number states which path a step is on.
 *
 * A single running sequence across branches asserts an order that does not
 * exist: on a four-way split, "7" and "8" sit on mutually exclusive paths and
 * "9" is unreachable from either. So numbering restarts inside every path, and
 * the path carries a letter on its parent's number — `4a.1` rather than a
 * continuation of the main line. Two consequences matter: no number implies a
 * sequence that cannot happen, and path identity survives losing colour, which
 * is what print and colour-blind readers depend on.
 *
 * A decision is real work -- someone applies criteria and it gets a number
 * like any other step, and its branches hang their letter off that number
 * (`3a.1`, not `2a.1` off whatever preceded it). A terminal takes no number:
 * it's where a path stops, not a step, and numbering it invites reading the
 * last end state as the process's final step.
 */
export function numberSteps<S extends FlowStep>(
  order: S[],
  branchPathByStepId: Map<string, BranchTag[]>,
  emptyBranches: EmptyBranch[],
  isUnnumbered: (step: S) => boolean,
): OutlineNumbering {
  const numberByStepId = new Map<string, string>()
  const pathNumberByBranchKey = new Map<string, string>()
  const lastNumberByPathKey = new Map<string, string>()
  const counterByPathKey = new Map<string, number>()

  // What a gateway's branches hang their letter off: the last number seen in
  // the gateway's own context, snapshotted the moment the gateway itself is
  // visited. A topological order is free to reach a fast-converging sibling
  // (or a main-line step past the whole fork) before a slower one -- if the
  // base were read lazily off "whatever was last numbered when this branch
  // happened to be reached", that unrelated later step would shift the slow
  // sibling's letter. Freezing it at the gateway makes every sibling share
  // one base regardless of the order they're each first encountered in.
  const baseNumberByStepId = new Map<string, string>()

  const ensurePathNumber = (tag: BranchTag): string => {
    const existing = pathNumberByBranchKey.get(tag.branchKey)
    if (existing) return existing
    const base = baseNumberByStepId.get(tag.gatewayId) ?? ''
    const num = `${base}${String.fromCharCode(97 + tag.siblingIndex)}`
    pathNumberByBranchKey.set(tag.branchKey, num)
    return num
  }

  for (const step of order) {
    const path = branchPathByStepId.get(step.id) ?? []
    const key = pathKey(path)

    if (!isUnnumbered(step)) {
      const n = (counterByPathKey.get(key) ?? 0) + 1
      counterByPathKey.set(key, n)
      const tag = path[path.length - 1]
      const prefix = tag ? ensurePathNumber(tag) : ''
      const num = prefix ? `${prefix}.${n}` : String(n)
      numberByStepId.set(step.id, num)
      lastNumberByPathKey.set(key, num)
    }

    // Snapshot AFTER this step's own number (if any) is applied. A gateway
    // and a boundary event's host are both ordinary numbered steps now, so
    // their branches/escape paths hang off THEIR number, not off whatever
    // came immediately before them.
    baseNumberByStepId.set(step.id, lastNumberByPathKey.get(key) ?? '')
  }

  // Empty branches never appear in `order`, so their handle is never otherwise
  // requested -- their gateway was still visited, so its snapshot is there.
  for (const { tag } of emptyBranches) ensurePathNumber(tag)

  return { numberByStepId, pathNumberByBranchKey }
}
