/**
 * Flow-analysis checks. Ported from process-wiki as-is:
 * the `check()` harness and every assertion below are unchanged. process-wiki
 * ran this standalone against esbuild + node with no test runner wired up;
 * The host app has vitest, so the whole body runs inside one `it()` and the
 * pass/fail tally is asserted at the end instead of setting process.exitCode
 * -- see PROVENANCE.md for that one wrapper change.
 */
import {
  analyzeFlow, buildSections, numberSteps, traceFrom, describeTrigger, describeStartTrigger, unreachableStepIds,
  formatCycle,
  proseStates,
  type FlowStep, type FlowEdge, type OutlineSection,
} from '../processFlow'

let failures = 0
let checks = 0

function check(name: string, actual: unknown, expected: unknown) {
  checks++
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  if (a !== e) {
    failures++
    console.log(`  FAIL ${name}\n       expected ${e}\n       actual   ${a}`)
  } else {
    console.log(`  ok   ${name}`)
  }
}

interface TS extends FlowStep { id: string; step_type: string; position_x: number; position_y: number }

/** Compact graph builder: steps are "id:type", edges are "src>tgt:label". */
function graph(stepSpec: string[], edgeSpec: string[]) {
  const steps: TS[] = stepSpec.map((spec, i) => {
    const [id, type = 'task', y, x] = spec.split(':')
    return {
      id,
      step_type: type,
      position_y: y !== undefined ? Number(y) : i * 100,
      position_x: x !== undefined ? Number(x) : 0,
    }
  })
  const edges: FlowEdge[] = edgeSpec.map((spec, i) => {
    const [pair, label] = spec.split(':')
    const [source_step_id, target_step_id] = pair.split('>')
    return { id: `e${i}`, source_step_id, target_step_id, label: label ?? null, condition: null }
  })
  return { steps, edges }
}

function ids(steps: TS[]) { return steps.map(s => s.id) }

/** One line per section: "<breadcrumb>: <step ids, or (empty)>". */
function sectionsText<S extends FlowStep & { id: string }>(sections: OutlineSection<S>[]): string[] {
  return sections.map(sec => {
    const heading = sec.tag ? sec.path.map(t => t.label).join(' > ') : 'main'
    const stepIds = sec.steps.map(s => s.id).join(',') || '(empty)'
    return `${heading}: ${stepIds}`
  })
}

function report<S extends FlowStep & { id: string }>(title: string, steps: S[], edges: FlowEdge[]) {
  const a = analyzeFlow(steps, edges)
  console.log(`\n=== ${title} ===`)
  console.log('order:  ', a.order.map(s => s.id).join(' → '))
  console.log('sections:')
  console.log(sectionsText(buildSections(a.order, a.branchPathByStepId, a.emptyBranches)).map(l => '  ' + l).join('\n'))
  if (a.mergeLabelsByStepId.size) {
    console.log('merges: ', [...a.mergeLabelsByStepId].map(([k, v]) => `${k}<=${v.join('|')}`).join(', '))
  }
  if (a.loopTargetIds.size) console.log('loops:  ', [...a.loopTargetIds].join(', '))
  if (a.backEdgeIds.size) console.log('backEdges:', [...a.backEdgeIds].join(', '))
  return a
}

it('flow analysis matches every exhaustive check-based assertion ported from process-wiki', () => {
// ---------------------------------------------------------------- 1. linear
{
  const { steps, edges } = graph(['s:event_start', 'a', 'b', 'e:event_end'], ['s>a', 'a>b', 'b>e'])
  const a = report('1. Linear', steps, edges)
  check('order', ids(a.order), ['s', 'a', 'b', 'e'])
  check('no branches', a.branchPathByStepId.size, 0)
  check('no merges', a.mergeLabelsByStepId.size, 0)
}

// ------------------------------------------- 2. simple fork that rejoins
{
  const { steps, edges } = graph(
    ['s:event_start', 'g:gateway_exclusive', 'a1', 'a2', 'b1', 'm', 'e:event_end'],
    ['s>g', 'g>a1:Approved', 'g>b1:Rejected', 'a1>a2', 'a2>m', 'b1>m', 'm>e'],
  )
  const a = report('2. Fork that rejoins', steps, edges)
  check('branch A contiguous', ids(a.order), ['s', 'g', 'a1', 'a2', 'b1', 'm', 'e'])
  check('a2 in Approved', a.branchPathByStepId.get('a2')?.map(t => t.label), ['Approved'])
  check('b1 in Rejected', a.branchPathByStepId.get('b1')?.map(t => t.label), ['Rejected'])
  check('merge untagged', a.branchPathByStepId.has('m'), false)
  check('merge labels', a.mergeLabelsByStepId.get('m'), ['Approved', 'Rejected'])
}

// --------------------------------- 3. branch that skips ahead (no own steps)
// The real Employee Onboarding shape: Rejected jumps straight to the last step.
{
  const { steps, edges } = graph(
    ['s:event_start', 'g:gateway_exclusive', 'a1', 'a2', 'last', 'e:event_end'],
    ['s>g', 'g>a1:Approved', 'g>last:Rejected', 'a1>a2', 'a2>last', 'last>e'],
  )
  const a = report('3. Short-circuit branch', steps, edges)
  check('order', ids(a.order), ['s', 'g', 'a1', 'a2', 'last', 'e'])
  check('approved group', ids(a.order.filter(s => a.branchPathByStepId.has(s.id))), ['a1', 'a2'])
  check('merge labels', a.mergeLabelsByStepId.get('last'), ['Rejected', 'Approved'])
  check('Rejected has no steps of its own', a.emptyBranches.map(b => b.tag.label), ['Rejected'])
}

// ------------------------------------------------- 4. three-way exclusive
{
  const { steps, edges } = graph(
    ['s:event_start', 'g:gateway_exclusive', 'a', 'b', 'c', 'm', 'e:event_end'],
    ['s>g', 'g>a:Low', 'g>b:Medium', 'g>c:High', 'a>m', 'b>m', 'c>m', 'm>e'],
  )
  const a = report('4. Three-way fork', steps, edges)
  check('order', ids(a.order), ['s', 'g', 'a', 'b', 'c', 'm', 'e'])
  check('three distinct branches', [
    a.branchPathByStepId.get('a')?.[0].label,
    a.branchPathByStepId.get('b')?.[0].label,
    a.branchPathByStepId.get('c')?.[0].label,
  ], ['Low', 'Medium', 'High'])
  check('distinct sibling indices', [
    a.branchPathByStepId.get('a')?.[0].siblingIndex,
    a.branchPathByStepId.get('b')?.[0].siblingIndex,
    a.branchPathByStepId.get('c')?.[0].siblingIndex,
  ], [0, 1, 2])
  check('merge labels', a.mergeLabelsByStepId.get('m'), ['Low', 'Medium', 'High'])
}

// ------------------------------------------------------- 5. nested gateway
{
  const { steps, edges } = graph(
    ['s:event_start', 'g1:gateway_exclusive', 'a1', 'g2:gateway_exclusive', 'x1', 'y1', 'am', 'b1', 'm', 'e:event_end'],
    ['s>g1', 'g1>a1:Approved', 'g1>b1:Rejected', 'a1>g2', 'g2>x1:Domestic', 'g2>y1:International',
     'x1>am', 'y1>am', 'am>m', 'b1>m', 'm>e'],
  )
  const a = report('5. Nested gateway', steps, edges)
  check('a1 depth 1', a.branchPathByStepId.get('a1')?.map(t => t.label), ['Approved'])
  check('g2 inside Approved', a.branchPathByStepId.get('g2')?.map(t => t.label), ['Approved'])
  check('x1 nested 2 deep', a.branchPathByStepId.get('x1')?.map(t => t.label), ['Approved', 'Domestic'])
  check('y1 nested 2 deep', a.branchPathByStepId.get('y1')?.map(t => t.label), ['Approved', 'International'])
  check('inner merge stays in Approved', a.branchPathByStepId.get('am')?.map(t => t.label), ['Approved'])
  check('inner merge labels', a.mergeLabelsByStepId.get('am'), ['Domestic', 'International'])
  check('outer merge labels', a.mergeLabelsByStepId.get('m'), ['Approved', 'Rejected'])
  // A branch that itself forks and later resumes (am, after Domestic/International)
  // stays one section -- not split by its own nested fork.
  check('sections', sectionsText(buildSections(a.order, a.branchPathByStepId, a.emptyBranches)), [
    'main: s,g1,m,e',
    'Approved: a1,g2,am',
    'Approved > Domestic: x1',
    'Approved > International: y1',
    'Rejected: b1',
  ])
}

// ------------------------------------------------------ 6. parallel (AND)
{
  const { steps, edges } = graph(
    ['s:event_start', 'g:gateway_parallel', 'p1', 'p2', 'j:gateway_parallel', 'e:event_end'],
    ['s>g', 'g>p1', 'g>p2', 'p1>j', 'p2>j', 'j>e'],
  )
  const a = report('6. Parallel split/join', steps, edges)
  check('order', ids(a.order), ['s', 'g', 'p1', 'p2', 'j', 'e'])
  check('parallel labels generated', [
    a.branchPathByStepId.get('p1')?.[0].label,
    a.branchPathByStepId.get('p2')?.[0].label,
  ], ['Track 1', 'Track 2'])
  check('flagged parallel', a.branchPathByStepId.get('p1')?.[0].parallel, true)
  check('join labels', a.mergeLabelsByStepId.get('j'), ['Track 1', 'Track 2'])
}

// --------------------------------------------------- 7. rework loop (cycle)
{
  const { steps, edges } = graph(
    ['s:event_start', 'draft', 'g:gateway_exclusive', 'fix', 'done', 'e:event_end'],
    ['s>draft', 'draft>g', 'g>done:Approved', 'g>fix:Needs work', 'fix>draft', 'done>e'],
  )
  const a = report('7. Rework loop', steps, edges)
  check('terminates, all steps ordered', ids(a.order).sort(), ['done', 'draft', 'e', 'fix', 'g', 's'])
  check('one back edge', a.backEdgeIds.size, 1)
  check('draft is loop target', [...a.loopTargetIds], ['draft'])
  check('fix tagged Needs work', a.branchPathByStepId.get('fix')?.map(t => t.label), ['Needs work'])
}

// ------------------------------- 8. side-by-side branches (position zig-zag)
// Branches laid out as parallel columns: sorting by Y alone would interleave
// a1,b1,a2,b2. Sections must still keep each branch's own steps together.
{
  const { steps, edges } = graph(
    ['s:event_start:0:0', 'g:gateway_exclusive:100:0',
     'a1:task:200:-200', 'b1:task:200:200',
     'a2:task:300:-200', 'b2:task:300:200',
     'm:task:400:0', 'e:event_end:500:0'],
    ['s>g', 'g>a1:Yes', 'g>b1:No', 'a1>a2', 'b1>b2', 'a2>m', 'b2>m', 'm>e'],
  )
  const a = report('8. Side-by-side columns', steps, edges)
  check('branches not interleaved', ids(a.order), ['s', 'g', 'a1', 'a2', 'b1', 'b2', 'm', 'e'])
  check('sections', sectionsText(buildSections(a.order, a.branchPathByStepId, a.emptyBranches)), [
    'main: s,g,m,e', 'Yes: a1,a2', 'No: b1,b2',
  ])
}

// ------------------------------------- 9. branches that never rejoin
{
  const { steps, edges } = graph(
    ['s:event_start', 'g:gateway_exclusive', 'a1', 'ae:event_end', 'b1', 'be:event_end'],
    ['s>g', 'g>a1:Accept', 'g>b1:Decline', 'a1>ae', 'b1>be'],
  )
  const a = report('9. Non-merging branches', steps, edges)
  check('order', ids(a.order), ['s', 'g', 'a1', 'ae', 'b1', 'be'])
  check('no merges', a.mergeLabelsByStepId.size, 0)
  check('separate end events tagged', [
    a.branchPathByStepId.get('ae')?.[0].label,
    a.branchPathByStepId.get('be')?.[0].label,
  ], ['Accept', 'Decline'])
}

// ------------------------------- 10. degenerate / hostile inputs
{
  const a = report('10a. Empty', [] as TS[], [])
  check('empty order', a.order.length, 0)

  const orphan = graph(['a', 'b'], [])
  const b = report('10b. No edges', orphan.steps, orphan.edges)
  check('both steps still listed', ids(b.order), ['a', 'b'])

  const dangling = graph(['a'], ['a>ghost', 'ghost>a'])
  const c = report('10c. Edges to missing steps', dangling.steps, dangling.edges)
  check('dangling edges ignored', ids(c.order), ['a'])

  const selfLoop = graph(['a', 'b'], ['a>a', 'a>b'])
  const d = report('10d. Self loop', selfLoop.steps, selfLoop.edges)
  check('self loop is a back edge', d.backEdgeIds.size, 1)
  check('still ordered', ids(d.order), ['a', 'b'])

  // A cycle with no entry point at all.
  const island = graph(['x', 'y'], ['x>y', 'y>x'])
  const f = report('10e. Cycle with no start', island.steps, island.edges)
  check('all steps still listed', ids(f.order).sort(), ['x', 'y'])

  // Two disconnected processes in one version.
  const two = graph(['s1:event_start', 'a', 's2:event_start', 'b'], ['s1>a', 's2>b'])
  const g = report('10f. Two disconnected chains', two.steps, two.edges)
  check('both chains present', ids(g.order).sort(), ['a', 'b', 's1', 's2'])
}

// -------------------------------- 11. wide + deep: 4-way with nesting inside
{
  const { steps, edges } = graph(
    ['s:event_start', 'g:gateway_exclusive',
     'w', 'x', 'gx:gateway_exclusive', 'x1', 'x2', 'xm', 'y', 'z', 'm', 'e:event_end'],
    ['s>g', 'g>w:W', 'g>x:X', 'g>y:Y', 'g>z:Z',
     'x>gx', 'gx>x1:X-a', 'gx>x2:X-b', 'x1>xm', 'x2>xm', 'xm>m',
     'w>m', 'y>m', 'z>m', 'm>e'],
  )
  const a = report('11. Four-way with nested fork', steps, edges)
  check('all steps ordered once', a.order.length, steps.length)
  check('x1 nested', a.branchPathByStepId.get('x1')?.map(t => t.label), ['X', 'X-a'])
  check('merge sees all four', a.mergeLabelsByStepId.get('m'), ['W', 'X', 'Y', 'Z'])
  check('sections', sectionsText(buildSections(a.order, a.branchPathByStepId, a.emptyBranches)), [
    'main: s,g,m,e', 'W: w', 'X: x,gx,xm', 'X > X-a: x1', 'X > X-b: x2', 'Y: y', 'Z: z',
  ])
}

// ---- 12. real shape from TEST Expense Approval: a branch's tail becomes
// reachable only after a main-line merge does. The branch must still read as
// one section (this no longer depends on physical order in `order` at all --
// sections group by branch key, so interleaving can't split one).
{
  const { steps, edges } = graph(
    ['s:event_start', 'check', 'g1:gateway_exclusive', 'auto', 'mgr', 'g2:gateway_exclusive',
     'finance', 'approve', 'reject', 'pay', 'e:event_end'],
    ['s>check', 'check>g1', 'g1>auto:Under 500', 'g1>mgr:500 or more', 'mgr>g2',
     'g2>finance:Approved', 'g2>reject:Rejected', 'finance>approve',
     'auto>pay', 'approve>pay', 'reject>e', 'pay>e'],
  )
  const a = report('12. Branch tail after a main-line merge', steps, edges)
  check('sections', sectionsText(buildSections(a.order, a.branchPathByStepId, a.emptyBranches)), [
    'main: s,check,g1,pay,e',
    'Under 500: auto',
    '500 or more: mgr,g2',
    '500 or more > Approved: finance,approve',
    '500 or more > Rejected: reject',
  ])
  check('rejoin at pay keeps context', a.mergeLabelsByStepId.get('pay'), ['Under 500', '500 or more › Approved'])
  check('rejoin at end names main path', a.mergeLabelsByStepId.get('e'), ['Main path', '500 or more › Rejected'])
}

// ---- 13. a main line that resumes after a merge stays one topological order
{
  const { steps, edges } = graph(
    ['s:event_start', 'g:gateway_exclusive', 'a', 'b', 'm', 'tail1', 'tail2', 'e:event_end'],
    ['s>g', 'g>a:Yes', 'g>b:No', 'a>m', 'b>m', 'm>tail1', 'tail1>tail2', 'tail2>e'],
  )
  const a = report('13. Main line after a merge', steps, edges)
  check('order', ids(a.order), ['s', 'g', 'a', 'b', 'm', 'tail1', 'tail2', 'e'])
}

// ============================ boundary events ============================
// steps take an extra spec field: "id:type:y:x:attachedTo:interrupting:trigger:detail"
function graphB(stepSpec: string[], edgeSpec: string[]) {
  const steps = stepSpec.map((spec, i) => {
    const [id, type = 'task', y, x, attached, interrupting, trigger, detail] = spec.split(':')
    return {
      id,
      step_type: type,
      position_y: y ? Number(y) : i * 100,
      position_x: x ? Number(x) : 0,
      attached_to_step_id: attached || null,
      interrupting: interrupting === undefined || interrupting === '' ? true : interrupting !== 'false',
      event_trigger: trigger || null,
      event_trigger_detail: detail || null,
    } as TS & { attached_to_step_id: string | null; interrupting: boolean; event_trigger: string | null; event_trigger_detail: string | null }
  })
  const edges: FlowEdge[] = edgeSpec.map((spec, i) => {
    const [pair, label] = spec.split(':')
    const [source_step_id, target_step_id] = pair.split('>')
    return { id: `e${i}`, source_step_id, target_step_id, label: label ?? null, condition: null }
  })
  return { steps, edges }
}

// ---- 14. interrupting boundary event whose escape path rejoins
{
  const { steps, edges } = graphB(
    ['s:event_start:0', 'review:task:100', 'to:event_boundary:150:80:review:true:timer:P5D',
     'escalate:task:200:200', 'approve:task:300', 'e:event_end:400'],
    ['s>review', 'review>approve', 'to>escalate', 'escalate>approve', 'approve>e'],
  )
  const a = report('14. Interrupting boundary event', steps, edges)
  check('event is not a flow row', ids(a.order).includes('to'), false)
  check('event is not a start', ids(a.order)[0], 's')
  check('host keeps the event', a.boundaryEventsByHostId.get('review')?.map(s => s.id), ['to'])
  check('escape path is a branch', a.branchPathByStepId.get('escalate')?.map(t => t.label), ['Not done within 5 days'])
  check('normal continuation stays on the main line', a.branchPathByStepId.has('approve'), false)
  check('sections', sectionsText(buildSections(a.order, a.branchPathByStepId, a.emptyBranches)), [
    'main: s,review,approve,e', 'Not done within 5 days: escalate',
  ])
  check('rejoin names the interruption', a.mergeLabelsByStepId.get('approve'), ['Main path', 'Not done within 5 days'])
}

// ---- 15. non-interrupting boundary event -> parallel semantics
{
  const { steps, edges } = graphB(
    ['s:event_start:0', 'work:task:100', 'flag:event_boundary:150:80:work:false:signal:Fraud',
     'notify:task:200:200', 'done:task:300', 'e:event_end:400'],
    ['s>work', 'work>done', 'flag>notify', 'notify>e', 'done>e'],
  )
  const a = report('15. Non-interrupting boundary event', steps, edges)
  check('branch flagged parallel', a.branchPathByStepId.get('notify')?.[0].parallel, true)
  check('branch label from signal -- "Fraud" is a word someone wrote, so it is kept',
    a.branchPathByStepId.get('notify')?.[0].label, 'A signal is raised: Fraud')
  check('event not in flow', ids(a.order).includes('flag'), false)
}

// ---- 16. two boundary events on one activity, one escape path terminates
{
  const { steps, edges } = graphB(
    ['s:event_start:0', 'task1:task:100',
     'b1:event_boundary:150:60:task1:true:timer:PT2H',
     'b2:event_boundary:150:120:task1:true:error:E_101',
     'retry:task:200:-200', 'fail:event_end:200:200', 'ok:task:300', 'e:event_end:400'],
    ['s>task1', 'task1>ok', 'b1>retry', 'retry>ok', 'b2>fail', 'ok>e'],
  )
  const a = report('16. Two boundary events', steps, edges)
  check('both attached to host', a.boundaryEventsByHostId.get('task1')?.map(s => s.id).sort(), ['b1', 'b2'])
  check('timer branch', a.branchPathByStepId.get('retry')?.[0].label, 'Not done within 2 hours')
  check('error branch -- "E_101" is a code, not a description, so it is dropped',
    a.branchPathByStepId.get('fail')?.[0].label, 'Something goes wrong')
  check('neither event is a row', ids(a.order).filter(i => i === 'b1' || i === 'b2'), [])
}

// ---- 17. boundary event on a task inside a branch -> its own section, headed
// by the full breadcrumb rather than nested under the branch's section.
{
  const { steps, edges } = graphB(
    ['s:event_start:0', 'g:gateway_exclusive:100', 'a1:task:200:-200',
     'bt:event_boundary:250:-120:a1:true:timer:P1D', 'chase:task:300:-350',
     'b1:task:200:200', 'm:task:400', 'e:event_end:500'],
    ['s>g', 'g>a1:Approved', 'g>b1:Rejected', 'a1>m', 'bt>chase', 'chase>m', 'b1>m', 'm>e'],
  )
  const a = report('17. Boundary event inside a branch', steps, edges)
  check('escape carries its parent branch in the breadcrumb', a.branchPathByStepId.get('chase')?.map(t => t.label),
    ['Approved', 'Not done within 1 day'])
  check('sections', sectionsText(buildSections(a.order, a.branchPathByStepId, a.emptyBranches)), [
    'main: s,g,m,e', 'Approved: a1', 'Approved > Not done within 1 day: chase', 'Rejected: b1',
  ])
}

// ---- 18. regression: an orphaned boundary event (host missing) must not crash
{
  const { steps, edges } = graphB(
    ['s:event_start:0', 'a:task:100', 'orphan:event_boundary:150:80:ghost:true:timer:P1D', 'e:event_end:200'],
    ['s>a', 'a>e'],
  )
  const a = report('18. Boundary event with a missing host', steps, edges)
  check('orphan still listed rather than dropped', ids(a.order).includes('orphan'), true)
  check('main flow intact', ids(a.order).slice(0, 2), ['s', 'a'])
}

// ---------------------- 19. a branch with no steps gets no section at all
// Local change from process-wiki: an "otherwise" edge that skips straight to
// a step already reached another way used to render as a heading over an
// empty list ("Rejected" with nothing under it) -- indistinguishable on the
// page from a real step that silently vanished. It is folded away instead;
// see PROVENANCE.md. The path is still named: the merge's own arrival labels
// carry it (mergeLabelsByStepId, test 3 above), and the gateway edge keeps
// its label on the canvas.
{
  const { steps, edges } = graph(
    ['s:event_start', 'g:gateway_exclusive', 'a1', 'a2', 'last', 'e:event_end'],
    ['s>g', 'g>a1:Approved', 'g>last:Rejected', 'a1>a2', 'a2>last', 'last>e'],
  )
  const a = report('19. Empty branch gets no section', steps, edges)
  const sections = buildSections(a.order, a.branchPathByStepId, a.emptyBranches)
  check('sections', sectionsText(sections), [
    'main: s,g,last,e', 'Approved: a1,a2',
  ])
  check('no section is headed "Rejected"', sections.some(s => s.tag?.label === 'Rejected'), false)
  check('a branch that does tag real steps still gets its own section',
    sections.some(s => s.tag?.label === 'Approved' && s.steps.length > 0), true)
}

// ---------------------- 19b. four otherwise branches that all skip to the
// same next gateway (the link-health shape) all fold away, none left dangling
{
  const { steps, edges } = graph(
    ['s:event_start', 'g1:gateway_exclusive', 'move', 'g2:gateway_exclusive', 'replace',
     'g3:gateway_exclusive', 'buy', 'g4:gateway_exclusive', 'record', 'done', 'e:event_end'],
    ['s>g1',
     'g1>move:Move it', 'g1>g2:Not a port move',
     'g2>replace:Replace it', 'g2>g3:Nothing to replace',
     'g3>buy:Buy a replacement', 'g3>g4:Nothing to buy',
     'g4>record:An action was taken', 'g4>done:Nothing to log',
     'move>g2', 'replace>g3', 'buy>g4', 'record>done', 'done>e'],
  )
  const a = report('19b. Four "otherwise" branches that skip ahead (link-health shape)', steps, edges)
  const sections = buildSections(a.order, a.branchPathByStepId, a.emptyBranches)
  const skippedLabels = ['Not a port move', 'Nothing to replace', 'Nothing to buy', 'Nothing to log']
  check('every "otherwise" branch is recognized as empty', a.emptyBranches.map(b => b.tag.label).sort(), skippedLabels.slice().sort())
  check('none of the skipped labels head a section', sections.filter(s => s.tag && skippedLabels.includes(s.tag.label)).length, 0)
  check('the branches that do have work still get sections', sections.map(s => s.tag?.label).filter(Boolean).sort(),
    ['An action was taken', 'Buy a replacement', 'Move it', 'Replace it'].sort())
}

// -------------- 20. every section is flat: none nests inside another
{
  const { steps, edges } = graph(
    ['s:event_start', 'g1:gateway_exclusive', 'a1', 'g2:gateway_exclusive', 'x1', 'y1', 'am', 'b1', 'm', 'e:event_end'],
    ['s>g1', 'g1>a1:Approved', 'g1>b1:Rejected', 'a1>g2', 'g2>x1:Domestic', 'g2>y1:International',
     'x1>am', 'y1>am', 'am>m', 'b1>m', 'm>e'],
  )
  const a = report('20. No section is nested inside another', steps, edges)
  const sections = buildSections(a.order, a.branchPathByStepId, a.emptyBranches)
  check('five flat sections, none a child of another', sections.length, 5)
  check('every section is a plain object with its own step list', sections.every(s => Array.isArray(s.steps)), true)
}

// ===================== branch-scoped numbering (P0) =========================
function numbersOf<S extends FlowStep & { id: string; step_type: string }>(steps: S[], edges: FlowEdge[]) {
  const a = analyzeFlow(steps, edges)
  const n = numberSteps(a.order, a.branchPathByStepId, a.emptyBranches, s =>
    a.terminalStepIds.has(s.id))
  const rendered = a.order.map(s => {
    const num = n.numberByStepId.get(s.id)
    const mark = s.step_type.startsWith('gateway') ? `<> ${num}` : (num ? num : 'END')
    return `${mark} ${s.id}`
  })
  return { a, n, rendered }
}

// ---- 21. the four-way split that shipped a false sequence
{
  const { steps, edges } = graph(
    ['s:event_start', 'triage', 'g:gateway_exclusive', 'p1', 'p2', 'p3', 'p4', 'post', 'e:event_end'],
    ['s>triage', 'triage>g', 'g>p1:Sev 1', 'g>p2:Sev 2', 'g>p3:Sev 3', 'g>p4:Sev 4',
     'p1>post', 'p2>post', 'p3>e', 'p4>e', 'post>e'],
  )
  const { n, rendered } = numbersOf(steps, edges)
  console.log('\n=== 21. Four-way split numbering ===')
  console.log(rendered.map(l => '  ' + l).join('\n'))
  check('main line numbers only main-line work', n.numberByStepId.get('triage'), '2')
  check('the decision itself is real work and gets the next number', n.numberByStepId.get('g'), '3')
  check('each path hangs its letter off the decision, not what preceded it', [
    n.numberByStepId.get('p1'), n.numberByStepId.get('p2'),
    n.numberByStepId.get('p3'), n.numberByStepId.get('p4'),
  ], ['3a.1', '3b.1', '3c.1', '3d.1'])
  check('no path step collides with another', new Set([
    n.numberByStepId.get('p1'), n.numberByStepId.get('p2'),
    n.numberByStepId.get('p3'), n.numberByStepId.get('p4'),
  ]).size, 4)
  check('paths are addressable', [
    ...n.pathNumberByBranchKey.values(),
  ].sort(), ['3a', '3b', '3c', '3d'])
  check('end event takes no number', n.numberByStepId.has('e'), false)
}

// ---- 22. nesting stays short and non-colliding
{
  const { steps, edges } = graph(
    ['s:event_start', 'check', 'g1:gateway_exclusive', 'auto', 'mgr', 'g2:gateway_exclusive',
     'fin', 'appr', 'rej', 'pay', 'e:event_end'],
    ['s>check', 'check>g1', 'g1>auto:Under 500', 'g1>mgr:500 or more', 'mgr>g2',
     'g2>fin:Approved', 'g2>rej:Rejected', 'fin>appr',
     'auto>pay', 'appr>pay', 'rej>e', 'pay>e'],
  )
  const { n, rendered } = numbersOf(steps, edges)
  console.log('\n=== 22. Nested numbering ===')
  console.log(rendered.map(l => '  ' + l).join('\n'))
  check('the outer decision gets step 3', n.numberByStepId.get('g1'), '3')
  check('first path off decision 3', n.numberByStepId.get('auto'), '3a.1')
  check('second path off decision 3', n.numberByStepId.get('mgr'), '3b.1')
  check('the nested decision gets its own number too', n.numberByStepId.get('g2'), '3b.2')
  check('nested path stays three segments', n.numberByStepId.get('fin'), '3b.2a.1')
  check('nested sibling', n.numberByStepId.get('rej'), '3b.2b.1')
  check('main line resumes after the fork', n.numberByStepId.get('pay'), '4')
}

// ---- 23. an exception path numbers off the activity it interrupts
{
  const { steps, edges } = graphB(
    ['s:event_start:0', 'review:task:100', 'to:event_boundary:150:80:review:true:timer:P5D',
     'escalate:task:200:200', 'approve:task:300', 'e:event_end:400'],
    ['s>review', 'review>approve', 'to>escalate', 'escalate>approve', 'approve>e'],
  )
  const { n, rendered } = numbersOf(steps, edges)
  console.log('\n=== 23. Exception path numbering ===')
  console.log(rendered.map(l => '  ' + l).join('\n'))
  check('host keeps its main-line number', n.numberByStepId.get('review'), '2')
  check('escape path hangs off the host', n.numberByStepId.get('escalate'), '2a.1')
  check('main line continues past the exception', n.numberByStepId.get('approve'), '3')
}

// ---- 24. every terminal is an end state, not the last integer
{
  const { steps, edges } = graph(
    ['s:event_start', 'g:gateway_exclusive', 'ok', 'settled:event_end', 'abandoned:event_end'],
    ['s>g', 'g>ok:Accept', 'g>abandoned:Decline', 'ok>settled'],
  )
  const { n } = numbersOf(steps, edges)
  check('both end states unnumbered', [
    n.numberByStepId.has('settled'), n.numberByStepId.has('abandoned'),
  ], [false, false])
  check('start event is numbered work', n.numberByStepId.get('s'), '1')
}

// ---- 25. an empty branch still gets a path number, for its section heading
{
  const { steps, edges } = graph(
    ['s:event_start', 'g:gateway_exclusive', 'a1', 'a2', 'last', 'e:event_end'],
    ['s>g', 'g>a1:Approved', 'g>last:Rejected', 'a1>a2', 'a2>last', 'last>e'],
  )
  const a = analyzeFlow(steps, edges)
  const n = numberSteps(a.order, a.branchPathByStepId, a.emptyBranches, s =>
    a.terminalStepIds.has(s.id))
  const rejectedTag = a.emptyBranches[0].tag
  check('empty branch is addressable', n.pathNumberByBranchKey.get(rejectedTag.branchKey), '2b')
}


// =========================== trace: adjacent nodes ==========================
// Direct predecessor(s)/successor(s) only -- not the whole up/downstream
// chain. A click should show what's next to this step, not repaint half the
// process; the interesting case is a fork, where "adjacent" can still be
// more than one step. Only the successor side walks through a gateway --
// predecessors stay a flat one hop, even onto a gateway, since a branch
// step's predecessor is always a single edge with nothing ambiguous to
// resolve on the other side of it.

// ---- 26. clicking a branch step lights up just its own neighbours
{
  const { steps, edges } = graph(
    ['s:event_start', 'g:gateway_exclusive', 'a1', 'a2', 'b1', 'm', 'e:event_end'],
    ['s>g', 'g>a1:Approved', 'g>b1:Rejected', 'a1>a2', 'a2>m', 'b1>m', 'm>e'],
  )
  const a = report('26. Trace from inside a branch', steps, edges)
  const t = traceFrom('a1', a.forwardOutgoingByStepId, a.forwardIncomingByStepId)
  check('predecessor is the gateway, one hop -- not walked through to what precedes it', [...t.predecessorIds], ['g'])
  check('successor is the next step one hop forward', [...t.successorIds], ['a2'])
  check('the sibling branch is neither', t.predecessorIds.has('b1') || t.successorIds.has('b1'), false)
}

// ---- 27. clicking a merge point lights up every branch that feeds it
{
  const { steps, edges } = graph(
    ['s:event_start', 'g:gateway_exclusive', 'a1', 'b1', 'm', 'e:event_end'],
    ['s>g', 'g>a1:Yes', 'g>b1:No', 'a1>m', 'b1>m', 'm>e'],
  )
  const a = report('27. Trace from a merge point', steps, edges)
  const t = traceFrom('m', a.forwardOutgoingByStepId, a.forwardIncomingByStepId)
  check('predecessors are both branch tails, not the whole chain back to start', [...t.predecessorIds].sort(), ['a1', 'b1'])
  check('successor is just the next step', [...t.successorIds], ['e'])
}

// ---- 28. tracing from a boundary event's escape path reaches its host
{
  const { steps, edges } = graphB(
    ['s:event_start:0', 'review:task:100', 'to:event_boundary:150:80:review:true:timer:P5D',
     'escalate:task:200:200', 'approve:task:300', 'e:event_end:400'],
    ['s>review', 'review>approve', 'to>escalate', 'escalate>approve', 'approve>e'],
  )
  const a = report('28. Trace through a boundary event escape path', steps, edges)
  const t = traceFrom('escalate', a.forwardOutgoingByStepId, a.forwardIncomingByStepId)
  check('predecessor is the host, not the boundary event itself', [...t.predecessorIds], ['review'])
  check('successor is where the escape path rejoins', [...t.successorIds], ['approve'])
}

// ---- 29. a rework loop's back edge does not appear as a successor
{
  const { steps, edges } = graph(
    ['s:event_start', 'draft', 'g:gateway_exclusive', 'fix', 'done', 'e:event_end'],
    ['s>draft', 'draft>g', 'g>done:Approved', 'g>fix:Needs work', 'fix>draft', 'done>e'],
  )
  const a = report('29. Trace excludes the back edge', steps, edges)
  const t = traceFrom('fix', a.forwardOutgoingByStepId, a.forwardIncomingByStepId)
  check('predecessor is the gateway, one hop -- not walked through to what precedes it', [...t.predecessorIds], ['g'])
  check('fix > draft is the back edge, so fix has no forward successor', [...t.successorIds], [])
}


// ---- 30. clicking the step before a wide fork reaches every branch it opens
// This used to assert the opposite: that a fork is walked through, so the step
// before a four-way split reached all four branches. That held while a decision
// was an unnumbered router. Now it is real work with a number and a name, so
// "leads to the decision" is a complete answer and the branches are stated
// under the decision itself -- the walk-through only added rows two hops away.
{
  const { steps, edges } = graph(
    ['s:event_start', 'triage', 'g:gateway_exclusive', 'p1', 'p2', 'p3', 'p4', 'post', 'e:event_end'],
    ['s>triage', 'triage>g', 'g>p1:Sev 1', 'g>p2:Sev 2', 'g>p3:Sev 3', 'g>p4:Sev 4',
     'p1>post', 'p2>post', 'p3>e', 'p4>e', 'post>e'],
  )
  const a = report('30. Trace stops at a wide fork', steps, edges)
  const t = traceFrom('triage', a.forwardOutgoingByStepId, a.forwardIncomingByStepId)
  check('successor is the gateway itself and stops there -- one hop', [...t.successorIds].sort(),
    ['g'])
  check('the fork still names its own branches when clicked directly',
    [...traceFrom('g', a.forwardOutgoingByStepId, a.forwardIncomingByStepId).successorIds].sort(),
    ['p1', 'p2', 'p3', 'p4'])
  check('predecessor does not run in reverse -- p1 stops at the gateway, not back at triage',
    [...traceFrom('p1', a.forwardOutgoingByStepId, a.forwardIncomingByStepId).predecessorIds],
    ['g'])
}

// ---- 31. a rework gateway is one hop like anything else
// Kept from when forks were walked through, because it still pins the part
// that matters: a back edge is not a successor, so the gateway's forward
// adjacency is exactly its one real exit.
{
  const { steps, edges } = graph(
    ['s:event_start', 'review', 'wait', 'g:gateway_exclusive', 'pay', 'e:event_end'],
    ['s>review', 'review>wait', 'wait>g', 'g>pay:Ready', 'g>review:Rework', 'pay>e'],
  )
  const a = report('31. Trace past a rework gateway', steps, edges)
  const t = traceFrom('wait', a.forwardOutgoingByStepId, a.forwardIncomingByStepId)
  check('stops at the gateway', [...t.successorIds], ['g'])
  check('the gateway still reaches its own single successor when clicked directly',
    [...traceFrom('g', a.forwardOutgoingByStepId, a.forwardIncomingByStepId).successorIds],
    ['pay'])
}


// ---- 32. trigger prose is written for a reader, not a modeller
// BPMN points at a message or signal by id, and modelling tools generate
// those ids, so most real files carry "Message_1" rather than anything a
// person wrote. Printing one produces "A message arrives: Message_1", which
// tells a reader nothing -- and the identity is already the step's own
// title. An opaque ref is dropped; prose someone actually typed is kept.
{
  const t = (trigger: string, detail?: string) =>
    describeTrigger({ id: 'x', event_trigger: trigger, event_trigger_detail: detail } as never)
  console.log('\n=== 32. Trigger prose ===')
  check('a generated message ref is dropped, not printed',
    t('message', 'Message_1'), 'A message arrives')
  check('a generated signal ref is dropped, not printed',
    t('signal', 'Signal_a4f2'), 'A signal is raised')
  check('prose the author typed is kept',
    t('signal', 'Fraud confirmed by the bank'), 'A signal is raised: Fraud confirmed by the bank')
  check('a duration is exempt -- opaque raw, meaningful once formatted',
    t('timer', 'P5D'), 'Not done within 5 days')
  check('no detail at all still reads as a sentence',
    t('message'), 'A message arrives')
  check('a single written word is kept -- it is a description, not a handle',
    t('signal', 'Fraud'), 'A signal is raised: Fraud')
  check('an error code is dropped -- E_101 describes nothing',
    t('error', 'E_101'), 'Something goes wrong')
  check('constructs a layman would not know are said in words',
    t('compensation'), 'Completed work has to be undone')
  // This expected "On a schedule: every 5 days", which read a duration as a
  // recurrence -- the conflation the timer subtype exists to fix. With no
  // subtype stated the value is a duration, and says so.
  check('a start timer with no stated kind reads as a duration',
    describeStartTrigger({ id: 'x', event_trigger: 'timer', event_trigger_detail: 'P5D' } as never),
    '5 days after the process is due to begin')
  check('a start message drops its generated ref too',
    describeStartTrigger({ id: 'x', event_trigger: 'message', event_trigger_detail: 'MsgDef_1' } as never),
    'A message arrives')
}


// ---- 33. orphan detection guards edge deletion
// Deleting an edge can silently remove a step from the document: the outline
// is walked forward from the start, so a step with nothing leading into it is
// never rendered. The naive check -- "roots are steps with no incoming edge"
// -- always passes, because the step just orphaned becomes a root under that
// definition. Reachability is measured from start events instead.
{
  console.log('\n=== 33. Orphan detection ===')
  const { steps, edges } = graph(
    ['s:event_start', 'a', 'b', 'e:event_end'],
    ['s>a', 'a>b', 'b>e'],
  )
  check('nothing orphaned in a connected chain',
    [...unreachableStepIds(steps, edges)], [])
  check('cutting a>b orphans b and everything past it',
    [...unreachableStepIds(steps, edges.filter(x => !(x.source_step_id === 'a' && x.target_step_id === 'b')))].sort(),
    ['b', 'e'])
  check('cutting the last edge orphans only the tail',
    [...unreachableStepIds(steps, edges.filter(x => x.target_step_id !== 'e'))], ['e'])
}

// A step reachable by a second route survives losing the first.
{
  const { steps, edges } = graph(
    ['s:event_start', 'g:gateway_exclusive', 'a', 'b', 'm', 'e:event_end'],
    ['s>g', 'g>a:Yes', 'g>b:No', 'a>m', 'b>m', 'm>e'],
  )
  check('m keeps one of its two routes, so nothing is orphaned',
    [...unreachableStepIds(steps, edges.filter(x => !(x.source_step_id === 'a' && x.target_step_id === 'm')))], [])
  check('losing both routes into m orphans m and the tail',
    [...unreachableStepIds(steps, edges.filter(x => x.target_step_id !== 'm'))].sort(), ['e', 'm'])
}

// A boundary event never has incoming flow, so it must not read as orphaned.
{
  const steps = [
    { id: 's', step_type: 'event_start', position_x: 0, position_y: 0 },
    { id: 'task', step_type: 'task', position_x: 0, position_y: 100 },
    { id: 'ev', step_type: 'event_boundary', position_x: 0, position_y: 150, attached_to_step_id: 'task', event_trigger: 'timer' },
    { id: 'esc', step_type: 'task', position_x: 0, position_y: 200 },
    { id: 'e', step_type: 'event_end', position_x: 0, position_y: 300 },
  ]
  const edges = [
    { id: 'e1', source_step_id: 's', target_step_id: 'task', label: null, condition: null },
    { id: 'e2', source_step_id: 'task', target_step_id: 'e', label: null, condition: null },
    { id: 'e3', source_step_id: 'ev', target_step_id: 'esc', label: null, condition: null },
  ]
  check('a boundary event and its escape path are reachable through the host',
    [...unreachableStepIds(steps, edges)], [])
  // The end event went too, and should have: its only route in ran through
  // the host. Losing one edge orphaning four steps is exactly why this check
  // exists rather than a simple "does the target still have an edge" test.
  check('detaching the host orphans the event, its escape path, and the tail',
    [...unreachableStepIds(steps, edges.filter(x => x.target_step_id !== 'task'))].sort(),
    ['e', 'esc', 'ev', 'task'])
}


// ---- 34. every value the importer can write has a sentence
// There is no enum behind event_trigger: the column is String(50) and the
// schema takes a bare str, so the vocabulary lives in three hand-written
// places -- TRIGGER_ELEMENTS in bpmn_service.py, this switch, and the
// dropdown on the read page. They drifted: terminate and link were imported
// correctly and then had no sentence and no option, so the one step with
// terminate displayed as "None" and would have lost the value if touched.
{
  console.log('\n=== 34. Trigger vocabulary ===')
  // Mirrors TRIGGER_ELEMENTS in backend/app/services/bpmn_service.py.
  const IMPORTER_WRITES = ['timer', 'error', 'escalation', 'message', 'signal',
    'conditional', 'compensation', 'terminate', 'cancel', 'link']
  const missing = IMPORTER_WRITES.filter(t =>
    !describeTrigger({ id: 'x', event_trigger: t } as never))
  check('no importable trigger is left without a sentence', missing, [])
  check('terminate reads as an outcome', describeTrigger({ id: 'x', event_trigger: 'terminate' } as never),
    'Everything stops here')
  check('link reads as a continuation', describeTrigger({ id: 'x', event_trigger: 'link' } as never),
    'Continues from elsewhere in the diagram')
}


// ---- 35. a timer's three kinds are three different facts
// timerEventDefinition holds exactly one of timeDuration, timeDate or
// timeCycle. The importer read whichever child had text and dropped the
// element name, so all three arrived as one undifferentiated string and were
// described as a deadline: a fixed date read "not done within 2026-12-31",
// a recurrence "not done within R3/PT10M".
{
  console.log('\n=== 35. Timer kinds ===')
  const t = (detail: string, subtype?: string) => describeTrigger(
    { id: 'x', event_trigger: 'timer', event_trigger_detail: detail, event_trigger_subtype: subtype } as never)
  const start = (detail: string, subtype?: string) => describeStartTrigger(
    { id: 'x', event_trigger: 'timer', event_trigger_detail: detail, event_trigger_subtype: subtype } as never)
  check('a duration is a window', t('P5D', 'duration'), 'Not done within 5 days')
  check('a date is a deadline, not a window', t('2026-12-31', 'date'), 'Not done by 2026-12-31')
  check('a cycle is a recurrence, not a limit',
    t('R3/PT10M', 'cycle'), 'Every time it comes round — every 10 minutes, 3 times')
  check('an unstated kind still reads as a duration, as it always did',
    t('P5D'), 'Not done within 5 days')
  check('a start date reads as the date', start('2026-12-31', 'date'), 'On 2026-12-31')
  check('a start cycle reads as a schedule', start('R/P1D', 'cycle'), 'On a schedule: every 1 day')
  check('an unbounded cycle omits the count', formatCycle('R/PT2H'), 'every 2 hours')
  check('a bounded cycle states it', formatCycle('R4/PT30M'), 'every 30 minutes, 4 times')
  check('an unparseable cycle is returned rather than mangled',
    formatCycle('every other Tuesday'), 'every other Tuesday')
}


// ---- 36. the prose is a rendering of the fields, so it can be compared
// The derived sentences are produced from event_trigger, its detail and its
// subtype, so a field row beside its own sentence is one datum shown twice.
// Which fields are covered is not fixed -- a generated ref never reaches a
// sentence and must keep its row -- so this is a comparison rather than a
// list of fields to hide, which would rot the moment a sentence changed.
{
  console.log('\n=== 36. Prose covers a value ===')
  check('an identical rendering is covered',
    proseStates('A message arrives', 'A message arrives'), true)
  check('a formatted duration is covered by the words in the sentence',
    proseStates('Not done within 5 days', 'P5D', '5 days'), true)
  check('the raw form alone is not found in prose that formatted it',
    proseStates('Not done within 5 days', 'P5D'), false)
  check('a generated ref the prose omits keeps its row',
    proseStates('A signal is raised', 'Signal_fraud'), false)
  check('case does not decide it',
    proseStates('If tax relevant', 'Tax relevant'), true)
  check('no sentence covers nothing', proseStates(undefined, 'P5D'), false)
  check('no value is not covered', proseStates('Not done within 5 days', ''), false)
}

console.log(`\n${checks - failures}/${checks} checks passed`)
if (failures) console.log(`${failures} FAILURES`)
// Ported from process-wiki's standalone esbuild+node harness (see the file
// header): that harness set process.exitCode on failure instead of
// asserting per-check, so it runs the same way here -- one vitest
// assertion covering every `check()` call made above.
expect(failures).toBe(0)
})
