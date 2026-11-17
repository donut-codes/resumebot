// "Living" mode reskins the read page's flow diagram as an actual node-link
// graph -- the visual language of Obsidian's graph view, D3's force-directed
// graph examples, and network diagrams generally -- rather than animating
// the BPMN flowchart's own rectangles in place. That means: circles instead
// of label-filled boxes, sized by how connected a step is (fan-in + fan-out)
// rather than a uniform size, colour still carrying the step's kind; thin
// straight lines instead of thick square-cornered connectors; and a layout
// that genuinely organizes itself from those same edges -- link pulls
// connected steps together, charge pushes everything else apart, nothing
// holds a step to its dagre position. On a topology with real branching
// (a step several paths converge on, a decision with more than two ways
// out) that settles into visible hub shapes; on a nearly-linear process it
// settles into something chain-like, because that's what the real
// connectivity is -- not a bug, just an honest layout of a simple graph.
//
// Unlike layoutNotes (a one-shot settle -- 300 ticks, then stop, because
// there's a single non-overlapping answer worth finding once), this
// simulation is never meant to reach zero energy: alphaTarget keeps a
// small amount of it in the system indefinitely, which is what makes the
// loop keep running for as long as the mode stays on.
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type RefObject } from 'react'
import {
  forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY,
  type Simulation, type SimulationNodeDatum,
} from 'd3-force'
import {
  BaseEdge, Handle, Position, getStraightPath,
  type Edge, type EdgeProps, type EdgeTypes, type Node, type NodeProps, type NodeTypes,
} from '@xyflow/react'
import { byAttrIn } from './dom'
import { BLOCK_W, BLOCK_H, type BlockKind } from './processGraphKit'

// ---------------------------------------------------------------------------
// Degree -- how connected a step is, the same "importance" signal a network
// diagram's node size usually encodes (see the sales-rep / recruitment
// diagram this feature was modelled on: dot size is sales volume there,
// fan-in/fan-out here -- whichever metric the domain actually has).
// ---------------------------------------------------------------------------

export interface DegreeInfo {
  degreeById: Map<string, number>
  maxDegree: number
}

export function computeDegrees(edges: Edge[]): DegreeInfo {
  const degreeById = new Map<string, number>()
  const bump = (id: string) => degreeById.set(id, (degreeById.get(id) ?? 0) + 1)
  for (const e of edges) {
    bump(e.source)
    bump(e.target)
  }
  let maxDegree = 1
  for (const d of degreeById.values()) if (d > maxDegree) maxDegree = d
  return { degreeById, maxDegree }
}

export const LIVING_NODE_MIN = 46
export const LIVING_NODE_MAX = 88

export function diameterFromDegree(degree: number, maxDegree: number): number {
  const t = maxDegree <= 0 ? 0 : degree / maxDegree
  return LIVING_NODE_MIN + t * (LIVING_NODE_MAX - LIVING_NODE_MIN)
}

export function nodeDiameter(id: string, degrees: DegreeInfo): number {
  return diameterFromDegree(degrees.degreeById.get(id) ?? 0, degrees.maxDegree)
}

// ---------------------------------------------------------------------------
// The step number a node wears. Resumebot divergence (app/PROVENANCE.md, "process-view framing"): a deep step's number
// ("4b.2a.2b.4c.1") is wider than the node, so it overflowed. It breaks after each dot and is sized to fit the square
// inside the node: the largest size at which its lines fit, down to a floor.
// ---------------------------------------------------------------------------

const NUM_CHAR_EM = 0.62 // widest digit, in em: a measured 0.63 for a "1", rounded up a little
const NUM_LINE_EM = 1.1
export const NUM_MAX_PX = 20
export const NUM_MIN_PX = 4.5

/** The side of the square a node's number must sit in: the square inscribed in the circle, less the border and a margin. */
export function numberBoxPx(diameter: number): number {
  return Math.max(diameter * 0.72 - 8, 8)
}

/** A number's chunks, each ending at a dot (where it may break): "4b.2a.1" is "4b.", "2a.", "1". */
function numberChunks(num: string): string[] {
  return num.split(/(?<=\.)/)
}

/** The font size (px) at which `num` fits the node of this diameter, broken after dots, never above NUM_MAX_PX. */
export function numberFontPx(num: string, diameter: number): number {
  const side = numberBoxPx(diameter)
  const chunks = numberChunks(num)
  for (let size = NUM_MAX_PX; size > NUM_MIN_PX; size -= 0.5) {
    const perLine = Math.floor(side / (NUM_CHAR_EM * size))
    if (perLine < 1) continue
    let lines = 0
    let used = 0
    let fits = true
    for (const chunk of chunks) {
      if (chunk.length > perLine) { fits = false; break }
      if (lines === 0 || used + chunk.length > perLine) { lines += 1; used = chunk.length } else used += chunk.length
    }
    if (fits && lines * size * NUM_LINE_EM <= side) return size
  }
  return NUM_MIN_PX
}

function NodeNumber({ num, diameter }: { num: string; diameter: number }) {
  const style = { '--pw-num-fit': `${numberFontPx(num, diameter)}px`, '--pw-num-w': `${numberBoxPx(diameter)}px` } as CSSProperties
  return (
    <span className="pw-living-node-num" style={style}>
      {numberChunks(num).map((chunk, i) => <Fragment key={i}>{chunk}<wbr /></Fragment>)}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Node -- a coloured dot (kind still carries the same meaning the old
// flowchart block's fill did), the step's title wrapped inside it (small, clamped to a few
// lines) rather than beside it. A label sitting outside the circle isn't
// part of what collision keeps clear of -- it can only overlap the very
// thing it's naming. Teaching the physics about each label's own
// (variable-width, off-centre) footprint would work too, but a circle
// sized to hold its own title sidesteps that complexity by construction:
// the collision radius other steps already respect is the same shape the
// text has to fit inside, so nothing extra needs to know about text at
// all.
// ---------------------------------------------------------------------------

function kindFor(stepType: string): BlockKind {
  switch (stepType) {
    case 'gateway_exclusive':
    case 'gateway_parallel':
      return 'decision'
    case 'event_end':
      return 'terminal'
    case 'subprocess':
      return 'subprocess'
    case 'event_start':
    case 'event_intermediate':
    case 'event_boundary':
      return 'event'
    default:
      return 'task'
  }
}

interface LivingNodeData {
  stepType: string
  label: string
  num?: string
  degree?: number
  maxDegree?: number
  // Addition to the vendored code (PROVENANCE.md): the old static flowchart block
  // carried a gate's "?" / a remembered "★" badge, a hover tooltip
  // (the step's own why), and a lane sub-label underneath the title. The
  // living node is small (a circle, not a box) so the sub-label has
  // nowhere to sit without crowding the title -- it's dropped here and
  // stays available in the accessible step list below the canvas instead
  // -- but the badge and tooltip carry real status a reader shouldn't lose
  // just because Living mode is on, so both are kept.
  badge?: string
  tooltip?: string
  // Addition to the vendored code (PROVENANCE.md): where a running instance is, drawn on the step it is at or has passed.
  mark?: 'current' | 'passed'
}

export function LivingBlockNode({ data, selected }: NodeProps) {
  const { stepType, label, num, degree, maxDegree, badge, tooltip, mark } = data as unknown as LivingNodeData
  const kind = kindFor(stepType)
  const size = diameterFromDegree(degree ?? 0, maxDegree ?? 1)
  return (
    <div
      className={`pw-block pw-living-node pw-block-${kind}${selected ? ' selected' : ''}${mark ? ` pw-run-${mark}` : ''}`}
      style={{ width: size, height: size }}
      title={tooltip}
    >
      <Handle type="target" position={Position.Top} className="pw-block-handle-side" />
      {num && <NodeNumber num={num} diameter={size} />}
      {badge && <span className="pw-block-badge pw-living-node-badge" aria-hidden="true">{badge}</span>}
      <span className="pw-living-node-label">{label}</span>
      <Handle type="source" position={Position.Bottom} className="pw-block-handle-side" />
      <Handle type="source" position={Position.Left} id="note-out" className="pw-block-handle-side" />
    </div>
  )
}

// A boundary event has no incoming sequence flow -- offering a target
// handle in a builder would let a drag create the exact incoming edge that
// rule exists to rule out. This
// component only ever renders on the read page's Living canvas, though,
// which wires no onConnect and sets nodesDraggable/elementsSelectable to
// false -- nothing here can act on a user-drawn connection -- so it's safe
// to give it a target handle anyway, for the attachment edge (host ->
// boundary event) drawn onto the canvas to actually hug this node with:
// React Flow silently drops any edge whose target resolves to a node with
// zero target handles at all (logged only as a console warning, "Couldn't
// create edge for target handle id: null"), rather than falling back to
// drawing from the node's centre the way LivingBlockEdge's own geometry
// math does. The handle exists purely so the library agrees to render the
// edge; LivingBlockEdge never reads its position.
function LivingBoundaryNode(props: NodeProps) {
  const { stepType, label, num, degree, maxDegree, mark } = props.data as unknown as LivingNodeData
  const kind = kindFor(stepType)
  const size = diameterFromDegree(degree ?? 0, maxDegree ?? 1)
  return (
    <div
      className={`pw-block pw-living-node pw-block-${kind}${props.selected ? ' selected' : ''}${mark ? ` pw-run-${mark}` : ''}`}
      style={{ width: size, height: size }}
    >
      {num && <NodeNumber num={num} diameter={size} />}
      <span className="pw-living-node-label">{label}</span>
      <Handle type="target" position={Position.Top} className="pw-block-handle-side" />
      <Handle type="source" position={Position.Bottom} className="pw-block-handle-side" />
      <Handle type="source" position={Position.Left} id="note-out" className="pw-block-handle-side" />
    </div>
  )
}

export const livingNodeTypes: NodeTypes = {
  task: LivingBlockNode,
  gateway_exclusive: LivingBlockNode,
  gateway_parallel: LivingBlockNode,
  event_start: LivingBlockNode,
  event_end: LivingBlockNode,
  subprocess: LivingBlockNode,
  event_intermediate: LivingBlockNode,
  event_boundary: LivingBoundaryNode,
}

// ---------------------------------------------------------------------------
// Edge -- a thin straight line with a small arrowhead, not the flowchart's
// thick square-cornered connector. A network diagram draws every
// connection as a straight line between wherever the two nodes actually
// sit, loops included -- the old flowchart's lane routing is meaningless
// once node positions are free (and has been removed).
//
// Not drawn from fixed Top/Bottom handles, as the old flowchart blocks were. Those made
// sense when every edge ran strictly downward through a rank; once a
// neighbour can be in any direction, a line still forced out through a
// single fixed point looks kinked right at the node -- it has to travel
// to that point before it can head toward wherever the target actually
// is. Instead this draws a straight line clipped to both circles' edges
// -- the standard "floating edge" a network diagram actually uses,
// leaving in whatever direction the target really is.
//
// The centre/radius for each end comes through `data` (set by whoever
// builds the edge list, from the exact same position values the node
// itself is rendering from this render) rather than from React Flow's own
// internal node store via useInternalNode. That store updates through its
// own effect cycle, one render behind the node's position prop -- so an
// edge computed from it lagged the node it's attached to by a frame,
// visible as the line detaching from the circle on every position commit.
// Reading the same numbers the node used, in the same render, can't lag.
// ---------------------------------------------------------------------------

function circleEdgePoint(cx: number, cy: number, r: number, towardX: number, towardY: number) {
  const dx = towardX - cx
  const dy = towardY - cy
  const dist = Math.hypot(dx, dy) || 1
  return { x: cx + (dx / dist) * r, y: cy + (dy / dist) * r }
}

export interface LivingEdgeGeometry {
  x: number
  y: number
  r: number
}

export function LivingBlockEdge({ data, selected, markerEnd }: EdgeProps) {
  const isBack = Boolean((data as { isBack?: boolean } | undefined)?.isBack)
  const isAttachment = Boolean((data as { isAttachment?: boolean } | undefined)?.isAttachment)
  const sourceGeom = (data as { livingSource?: LivingEdgeGeometry } | undefined)?.livingSource
  const targetGeom = (data as { livingTarget?: LivingEdgeGeometry } | undefined)?.livingTarget
  if (!sourceGeom || !targetGeom) return null

  const from = circleEdgePoint(sourceGeom.x, sourceGeom.y, sourceGeom.r, targetGeom.x, targetGeom.y)
  const to = circleEdgePoint(targetGeom.x, targetGeom.y, targetGeom.r, sourceGeom.x, sourceGeom.y)
  const [path] = getStraightPath({ sourceX: from.x, sourceY: from.y, targetX: to.x, targetY: to.y })

  return (
    <BaseEdge
      path={path}
      // Nothing "flows" through an attachment -- it's BPMN structure, not
      // sequence -- so it gets no arrowhead at all, same as UML's
      // aggregation/composition lines carrying a relationship without
      // implying direction.
      markerEnd={isAttachment ? undefined : markerEnd}
      className={`pw-living-edge${isAttachment ? ' pw-living-edge-attachment' : ''}${selected ? ' selected' : ''}`}
      // A back-edge's dash is inline, not a shared CSS class -- a
      // class-based stroke-dasharray risked also matching the arrowhead
      // marker's own path (a marker is itself a stroked/filled SVG shape,
      // and a rework loop's arrowhead was rendering invisibly, exactly
      // what a dashed marker outline would look like at this size). An
      // attachment carries no marker at all, so it's not at risk the same
      // way -- its dash lives in the pw-living-edge-attachment class
      // instead, applied via the className above.
      style={isBack ? { strokeDasharray: '2 4' } : undefined}
    />
  )
}

export const livingEdgeTypes: EdgeTypes = { block: LivingBlockEdge }

// ---------------------------------------------------------------------------
// Layout -- genuinely self-organizing: link pulls connected steps together,
// charge pushes everything else apart, collide keeps circles from
// overlapping, and that's what decides where a step ends up. No anchor
// holding it near its dagre position. Sequence doesn't need pixel rank to
// stay legible here -- the edges are directional (arrowheads), and tracing
// "this leads to that" is exactly the same act as tracing an edge, force
// layout or not. dagre's position is used only to seed each step's very
// first frame, so the graph doesn't open from a dead stack at the origin;
// nothing pulls it back there afterward. forceCenter keeps the whole
// cluster's centroid from drifting off-canvas over time -- it translates
// every node by the same amount, so it can't distort the shape link/charge
// settle on, only keep it in view.
// ---------------------------------------------------------------------------

interface LivingSimNode extends SimulationNodeDatum {
  id: string
  r: number
}

// Addition to the vendored code (PROVENANCE.md): the board is wider than it is tall, and dagre lays a process out top to bottom,
// so a long chain seeded from it settled as a tall, thin column that a wide board could only show tiny. When the
// seed is taller than it is wide, turn it on its side (swap x and y of each block's centre) before the simulation
// starts, so the chain settles left to right and fills the board. Only the seed turns; the forces decide the rest.
export function seedForWideBoard(nodes: Node[]): Node[] {
  if (nodes.length === 0) return nodes
  const xs = nodes.map(n => n.position.x + BLOCK_W / 2)
  const ys = nodes.map(n => n.position.y + BLOCK_H / 2)
  const width = Math.max(...xs) - Math.min(...xs)
  const height = Math.max(...ys) - Math.min(...ys)
  if (height <= width) return nodes
  return nodes.map((n, i) => ({
    ...n,
    position: { x: ys[i] - BLOCK_W / 2, y: xs[i] - BLOCK_H / 2 },
  }))
}

const CHARGE_STRENGTH = -70
// Addition to the vendored code: how hard each step is pulled to the middle (see the 'x'/'y' forces).
const GATHER_STRENGTH = 0.04
const LINK_DISTANCE = 110
const LINK_STRENGTH = 0.5
const VELOCITY_DECAY = 0.32
// Never lets alpha decay all the way to zero -- zero is where d3-force's
// internal timer stops calling tick() at all, which would freeze the
// layout the moment it first settled.
const RESTING_ALPHA = 0.05

// Ticks run before anything is painted, so the first frame shows the settled
// shape instead of dagre's tall column flying apart while the camera chases
// it. d3-force's own documented technique: tick() advances the simulation
// without dispatching events, so this costs no renders. 300 is d3's suggested
// number for a converged layout and, at this graph's size, takes under a
// frame.
const PREWARM_TICKS = 300
// d3-force ticks at ~60fps, but committing a fresh position to React on
// every one of those starves React Flow's node measurement/initialization
// under its controlled `nodes` prop -- confirmed empirically (nodes stayed
// present in the DOM, fully correct position data, but never rendered).
// Every 6th tick (~10fps) is still smooth for this kind of motion and
// leaves React Flow room to settle between updates.
const COMMIT_EVERY_N_TICKS = 6
// A commit is skipped while no step has moved this far (in px) from where React was last told it is. The simulation
// keeps a little energy in it for good (so a drag stays live), and eases into its shape over a few seconds; the paint
// loop below draws every frame of that regardless. What React is told only serves hit-testing, panning to a step
// and fitting the frame, which a pixel or two does not change, so a graph that has all but stopped is left alone
// instead of being drawn ten times a second for nothing.
const COMMIT_MIN_MOVE = 2

/**
 * The Living layout: a force simulation that places the steps and, every frame, paints them straight onto the
 * graph's elements. `rootRef` is the element the graph is drawn in; the paint loop looks only inside it, so two
 * views on one page never move each other's circles. Call this from a component that holds nothing else: the
 * positions are React state, and whatever owns them re-renders when they commit.
 */
export function useLivingLayout(
  anchorNodes: Node[], flowEdges: Edge[], degrees: DegreeInfo, enabled: boolean, rootRef: RefObject<HTMLElement | null>,
) {
  const [positions, setPositions] = useState<Map<string, { x: number; y: number }>>(new Map())
  const simRef = useRef<Simulation<LivingSimNode, undefined> | null>(null)
  const lastDragRef = useRef<Map<string, { x: number; y: number }>>(new Map())
  // React Flow's own drag system moves the grabbed node by writing its
  // transform directly, on its own, independent of anything here. The paint
  // loop below writes that same property every frame from the simulation --
  // so while a drag was in flight the two fought over one DOM property, the
  // paint loop won (it runs last, every frame), and the node snapped back
  // out from under the cursor: "I can't move 3c.1". The dragged node is the
  // one node the simulation doesn't get to paint.
  const draggingIdRef = useRef<string | null>(null)
  const dragVelocityRef = useRef<Map<string, { x: number; y: number }>>(new Map())

  useEffect(() => {
    if (!enabled || anchorNodes.length === 0) {
      simRef.current?.stop()
      simRef.current = null
      // A functional updater that bails to the same reference when already
      // empty -- `anchorNodes` can be a fresh-but-still-empty array every
      // render while the page is still loading (a pre-existing `?? []`
      // upstream, harmless on its own), and this effect's dependency on
      // that reference would otherwise re-fire every render, each time
      // handing React a "new" empty Map and forcing another render, in a
      // loop with no way to break out on its own.
      setPositions(prev => (prev.size === 0 ? prev : new Map()))
      return
    }

    // Only used to seed the very first frame -- the mean of dagre's own
    // positions, so the graph opens roughly where the static diagram would
    // have shown it, without that becoming an ongoing pull.
    const seedCenter = anchorNodes.reduce(
      (acc, n) => ({ x: acc.x + n.position.x + BLOCK_W / 2, y: acc.y + n.position.y + BLOCK_H / 2 }),
      { x: 0, y: 0 }
    )
    seedCenter.x /= anchorNodes.length
    seedCenter.y /= anchorNodes.length

    const simNodes: LivingSimNode[] = anchorNodes.map(n => ({
      id: n.id,
      r: nodeDiameter(n.id, degrees) / 2,
      x: n.position.x + BLOCK_W / 2,
      y: n.position.y + BLOCK_H / 2,
    }))
    const idSet = new Set(simNodes.map(n => n.id))
    const links = flowEdges
      .filter(e => idSet.has(e.source) && idSet.has(e.target))
      .map(e => ({ id: e.id, source: e.source, target: e.target, isAttachment: e.id.startsWith('attach-') }))

    const sim = forceSimulation(simNodes)
      .force('charge', forceManyBody().strength(CHARGE_STRENGTH))
      .force('link', forceLink<LivingSimNode, { id: string; source: string; target: string; isAttachment: boolean }>(links).id(d => d.id)
        // An attachment link's two ends should read as touching, the way a
        // BPMN boundary event sits right on its host's border -- not resting
        // at the same arm's-length distance a real sequence-flow edge does.
        // d3-force resolves source/target to the actual node objects (with
        // their real, degree-based radii) before this runs each tick, so
        // "touching" can be computed exactly rather than guessed as a
        // constant that's only right for one pair of sizes.
        .distance(d => d.isAttachment
          ? (d.source as unknown as LivingSimNode).r + (d.target as unknown as LivingSimNode).r + 2
          : LINK_DISTANCE)
        .strength(d => d.isAttachment ? 1 : LINK_STRENGTH))
      .force('center', forceCenter<LivingSimNode>(seedCenter.x, seedCenter.y))
      // Addition to the vendored code (PROVENANCE.md): a gentle pull to the middle. forceCenter only moves the whole graph, so a
      // process with a part not joined to the rest (a periodic pass with its own start) was pushed away by the
      // charge until the pieces sat at opposite corners of the board.
      .force('x', forceX<LivingSimNode>(seedCenter.x).strength(GATHER_STRENGTH))
      .force('y', forceY<LivingSimNode>(seedCenter.y).strength(GATHER_STRENGTH))
      .force('collide', forceCollide<LivingSimNode>().radius(d => d.r + 6).strength(0.85))
      .velocityDecay(VELOCITY_DECAY)
      .alphaTarget(RESTING_ALPHA)
      .alpha(0.9)

    let tickCount = 0
    let committed: Map<string, { x: number; y: number }> | null = null
    const pushPositions = () => {
      const next = new Map<string, { x: number; y: number }>()
      for (const n of simNodes) next.set(n.id, { x: (n.x ?? 0) - n.r, y: (n.y ?? 0) - n.r })
      tickCount++
      // Tried dropping this to every tick for the duration of a drag, on the
      // theory that the starvation below is a first-render-only problem once
      // React Flow has measured everything. It is not: the dragged node went
      // invisible immediately, the same signature as the original bug. The
      // throttle holds at all times, drag included.
      if (tickCount % COMMIT_EVERY_N_TICKS !== 0) return
      // Settled: nothing has moved far enough to be worth a render.
      if (committed) {
        const was = committed
        let moved = false
        for (const [id, p] of next) {
          const q = was.get(id)
          if (!q || Math.abs(p.x - q.x) >= COMMIT_MIN_MOVE || Math.abs(p.y - q.y) >= COMMIT_MIN_MOVE) {
            moved = true
            break
          }
        }
        if (!moved) return
      }
      committed = next
      setPositions(next)
    }
    // Settle first, paint second. The simulation used to start hot at
    // alpha 0.9 with the nodes still on their dagre seed positions, so the
    // opening moments of every process were a column visibly exploding into
    // a web while the viewport zoomed to keep up.
    sim.stop()
    for (let i = 0; i < PREWARM_TICKS; i++) sim.tick()

    sim.on('tick', pushPositions)
    pushPositions()
    // Back to the resting simmer that makes a drag feel live. alpha is spent
    // by now, so nothing lurches -- this only keeps the sim responsive.
    sim.alpha(RESTING_ALPHA).restart()
    simRef.current = sim

    // Smooth motion can't come from committing every tick to React state --
    // that's the exact thing COMMIT_EVERY_N_TICKS exists to avoid, confirmed
    // empirically (nodes present in the DOM, correct data, never rendered).
    // It can't come from a CSS transition on the node either -- tried once,
    // reverted: an edge computes its own path from data.livingSource/Target
    // every render, which doesn't know a CSS transition is mid-flight on the
    // node's paint, so the line visibly detaches from the circle.
    //
    // This paints both, imperatively, every animation frame, straight from
    // the simulation's own live x/y -- bypassing React entirely for the hot
    // path. Node and edge read the exact same numbers in the exact same
    // frame (no second data source, no interpolation lag between them), so
    // they can't desync the way the CSS attempt did, and nothing here ever
    // touches React Flow's `nodes`/`edges` props, so the measurement-
    // starvation bug can't recur either. The low-frequency setPositions
    // above is still what React Flow's own model (drag hit-testing,
    // centerOnStep, everything that reads a node's `position` prop) sees;
    // this is purely the paint layer sitting on top of it.
    const nodeElCache = new Map<string, HTMLElement>()
    const edgePathElCache = new Map<string, SVGPathElement>()
    // Looked up inside this graph's own element, with the id escaped: a second view on the page is not touched, and
    // an id with a quote in it cannot break the selector.
    function nodeEl(id: string): HTMLElement | null {
      const cached = nodeElCache.get(id)
      if (cached && cached.isConnected) return cached
      const el = byAttrIn(rootRef.current, '.react-flow__node', 'data-id', id)
      if (el) nodeElCache.set(id, el)
      return el
    }
    function edgePathEl(id: string): SVGPathElement | null {
      const cached = edgePathElCache.get(id)
      if (cached && cached.isConnected) return cached
      const edge = byAttrIn(rootRef.current, '.react-flow__edge', 'data-id', id)
      const el = edge?.querySelector<SVGPathElement>('path.react-flow__edge-path') ?? null
      if (el) edgePathElCache.set(id, el)
      return el
    }

    let rafId = requestAnimationFrame(paint)
    function paint() {
      for (const n of simNodes) {
        if (n.id === draggingIdRef.current) continue
        const el = nodeEl(n.id)
        if (el) el.style.transform = `translate(${(n.x ?? 0) - n.r}px, ${(n.y ?? 0) - n.r}px)`
      }
      for (const link of links) {
        // d3-force's forceLink resolves these from the id strings passed in
        // to the actual matched LivingSimNode objects, in place, once the
        // simulation initializes -- by the time this runs every frame,
        // link.source/target already are the live nodes, not their ids.
        const source = link.source as unknown as LivingSimNode
        const target = link.target as unknown as LivingSimNode
        const path = edgePathEl(link.id)
        if (!path || source.x == null || target.x == null) continue
        const from = circleEdgePoint(source.x, source.y ?? 0, source.r, target.x, target.y ?? 0)
        const to = circleEdgePoint(target.x, target.y ?? 0, target.r, source.x, source.y ?? 0)
        const [d] = getStraightPath({ sourceX: from.x, sourceY: from.y, targetX: to.x, targetY: to.y })
        path.setAttribute('d', d)
      }
      rafId = requestAnimationFrame(paint)
    }

    return () => {
      cancelAnimationFrame(rafId)
      sim.stop()
      simRef.current = null
    }
    // anchorNodes'/flowEdges'/degrees' identity changes whenever steps or
    // edges get re-laid-out -- exactly when the resting points and the
    // connectivity driving link/charge should reset too.
  }, [enabled, anchorNodes, flowEdges, degrees])

  // Two things have to happen the instant a drag starts, for two different
  // reasons. Hand the node over to React Flow's own drag rendering (the
  // paint loop skips whatever id is in draggingIdRef, so the two stop
  // fighting over one transform). And force an immediate full position
  // commit: onNodeDrag derives its (x, y) from React Flow's last-committed
  // node state, which is up to COMMIT_EVERY_N_TICKS stale by design, while
  // the paint loop has been drawing the live position every frame -- so
  // without this the drag's own math starts from wherever React last heard
  // the node was, not where it is, and onDrag writes that stale point
  // straight onto the sim node, which collide/charge then answer by
  // shoving its neighbours.
  const onDragStart = useCallback((id: string) => {
    draggingIdRef.current = id
    lastDragRef.current.delete(id)
    const sim = simRef.current
    if (!sim) return
    const next = new Map<string, { x: number; y: number }>()
    for (const n of sim.nodes() as LivingSimNode[]) next.set(n.id, { x: (n.x ?? 0) - n.r, y: (n.y ?? 0) - n.r })
    setPositions(next)
  }, [])

  // Held exactly where the pointer is, via fx/fy -- d3-force's own
  // convention for this. Setting x/y alone (what this did before) leaves
  // charge and collide still applying force to the node every tick while
  // you're holding it, so the pointer and the simulation pull against each
  // other and the node feels rubbery. fx/fy pins it outright: d3 clamps
  // x to fx each tick and stops integrating velocity for it, so nothing
  // fights the pointer until the drag ends.
  const onDrag = useCallback((id: string, x: number, y: number) => {
    const sim = simRef.current
    if (!sim) return
    const node = sim.nodes().find(n => n.id === id) as LivingSimNode | undefined
    if (!node) return
    const cx = x + node.r
    const cy = y + node.r
    const last = lastDragRef.current.get(id)
    // Pinning zeroes the node's velocity every tick, so the throw can't
    // accumulate on the node itself -- it's kept here and handed back at
    // release instead.
    if (last) dragVelocityRef.current.set(id, { x: (cx - last.x) * 0.6, y: (cy - last.y) * 0.6 })
    node.fx = cx
    node.fy = cy
    node.x = cx
    node.y = cy
    lastDragRef.current.set(id, { x: cx, y: cy })
    sim.alpha(Math.max(sim.alpha(), 0.3))
  }, [])

  const onDragStop = useCallback((id: string) => {
    // Unpin, hand back the velocity the drag built up, and let the paint
    // loop resume drawing this node from the next frame on -- released
    // with a throw rather than dropped dead.
    if (draggingIdRef.current === id) draggingIdRef.current = null
    lastDragRef.current.delete(id)
    const sim = simRef.current
    if (!sim) return
    const node = sim.nodes().find(n => n.id === id) as LivingSimNode | undefined
    if (node) {
      node.fx = null
      node.fy = null
      const thrown = dragVelocityRef.current.get(id)
      if (thrown) {
        node.vx = thrown.x
        node.vy = thrown.y
      }
    }
    dragVelocityRef.current.delete(id)
    sim.alpha(Math.max(sim.alpha(), 0.4))
  }, [])

  return { positions, onDragStart, onDrag, onDragStop }
}

// ---------------------------------------------------------------------------
// Addition to the vendored code (PROVENANCE.md): a one-shot settle, not vendored from
// the upstream project. useLivingLayout above is built for a *live* canvas -- a
// reader can drag a step, so the simulation has to keep running
// (alphaTarget never reaches zero) and repaint every frame via the
// requestAnimationFrame loop so a drag still feels responsive. The
// goal/run diagram has always been decorative and read-only (aria-hidden,
// nodesDraggable={false} -- WorkflowDiagram's own doc comment: the ordered
// step list beneath it is the real, keyboard-reachable view), so there's
// nothing for an ongoing simulation to stay responsive *to*. This ticks the
// exact same link/charge/collide/center forces to convergence once, the
// same PREWARM_TICKS budget useLivingLayout uses just to prewarm its first
// frame, then stops -- no perpetual timer, no RAF paint loop, nothing left
// running once this returns. The same "settle once, because there's a
// single non-overlapping answer worth finding" call the upstream project's own
// layoutNotes makes for its (also static) sticky notes.
// ---------------------------------------------------------------------------

export function useSettledLivingLayout(
  anchorNodes: Node[], flowEdges: Edge[], degrees: DegreeInfo, enabled: boolean,
): Map<string, { x: number; y: number }> {
  return useMemo(() => {
    if (!enabled || anchorNodes.length === 0) return new Map<string, { x: number; y: number }>()

    const seedCenter = anchorNodes.reduce(
      (acc, n) => ({ x: acc.x + n.position.x + BLOCK_W / 2, y: acc.y + n.position.y + BLOCK_H / 2 }),
      { x: 0, y: 0 }
    )
    seedCenter.x /= anchorNodes.length
    seedCenter.y /= anchorNodes.length

    const simNodes: LivingSimNode[] = anchorNodes.map(n => ({
      id: n.id,
      r: nodeDiameter(n.id, degrees) / 2,
      x: n.position.x + BLOCK_W / 2,
      y: n.position.y + BLOCK_H / 2,
    }))
    const idSet = new Set(simNodes.map(n => n.id))
    const links = flowEdges
      .filter(e => idSet.has(e.source) && idSet.has(e.target))
      .map(e => ({ id: e.id, source: e.source, target: e.target, isAttachment: e.id.startsWith('attach-') }))

    const sim = forceSimulation(simNodes)
      .force('charge', forceManyBody().strength(CHARGE_STRENGTH))
      .force('link', forceLink<LivingSimNode, { id: string; source: string; target: string; isAttachment: boolean }>(links).id(d => d.id)
        .distance(d => d.isAttachment
          ? (d.source as unknown as LivingSimNode).r + (d.target as unknown as LivingSimNode).r + 2
          : LINK_DISTANCE)
        .strength(d => d.isAttachment ? 1 : LINK_STRENGTH))
      .force('center', forceCenter<LivingSimNode>(seedCenter.x, seedCenter.y))
      // Addition to the vendored code (PROVENANCE.md): a gentle pull to the middle. forceCenter only moves the whole graph, so a
      // process with a part not joined to the rest (a periodic pass with its own start) was pushed away by the
      // charge until the pieces sat at opposite corners of the board.
      .force('x', forceX<LivingSimNode>(seedCenter.x).strength(GATHER_STRENGTH))
      .force('y', forceY<LivingSimNode>(seedCenter.y).strength(GATHER_STRENGTH))
      .force('collide', forceCollide<LivingSimNode>().radius(d => d.r + 6).strength(0.85))
      .velocityDecay(VELOCITY_DECAY)
      .stop()

    for (let i = 0; i < PREWARM_TICKS; i++) sim.tick()

    const positions = new Map<string, { x: number; y: number }>()
    for (const n of simNodes) positions.set(n.id, { x: (n.x ?? 0) - n.r, y: (n.y ?? 0) - n.r })
    return positions
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, anchorNodes, flowEdges, degrees])
}
