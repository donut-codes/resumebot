// The "Process flow" graph: the Living node-link graph on its board, with the zoom controls.
//
// It is a component of its own, apart from the Steps list, on purpose. The layout's positions are React state that
// changes while the circles move (and again whenever one is dragged); whatever holds that state re-renders on each
// commit. Held here, it is the graph and nothing else that re-renders: not the Steps list, not an open editor.
//
// Split out of ProcessView.tsx (2026-10-08 review, 3.7); the logic is the one ProcessView had, in the same order.
// See PROVENANCE.md.
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type MutableRefObject } from 'react'
import { ReactFlow, Background, Controls, MarkerType, type Edge, type Node, type ReactFlowInstance } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import './processView.css'
import type { ProcessEdge, ProcessStep } from './types'
import type { StepTrace } from './processFlow'
import {
  layoutWithDagre, styleFlowEdges, stepToNode, edgeToFlow, nodeAttachedHost, BLOCK_W, BLOCK_H,
} from './processGraphKit'
import { FLOW_MIN_ZOOM, fitWhole, frameWhenMeasured, type BoardSize } from './framing'
import {
  useLivingLayout, useSettledLivingLayout, seedForWideBoard, computeDegrees, diameterFromDegree, livingNodeTypes,
  livingEdgeTypes,
} from './livingGraphKit'

// Hoisted: React Flow redoes its bookkeeping when these objects change identity.
const NODE_TYPES = livingNodeTypes
const EDGE_TYPES = livingEdgeTypes
const PRO_OPTIONS = { hideAttribution: true }

// Living's thin lines want a small arrowhead; a big blocky one would be the
// loudest thing on the canvas.
const EDGE_DEFAULT_OPTIONS = { markerEnd: { type: MarkerType.ArrowClosed, width: 17, height: 17, color: 'var(--soft, #a8aeb6)' } }

// Local addition (PROVENANCE.md): React Flow's default minZoom is 0.5, and `fitView` never goes below it. A tall
// graph (a long process drawn top to bottom: about 1400px for 23 steps) needs about 0.3 to fit a 600px board, so at
// the default the fit stopped at 0.5 and both ends ran off the box. The floor sits well below any fit. (It lives in
// ./framing now, with the rest of the fitting; exported from here as before.)
export { FLOW_MIN_ZOOM }

export interface LivingFlowProps {
  steps: ProcessStep[]
  edges: ProcessEdge[]
  /** The lines that loop back (from the Steps list's own reading of the flow), drawn dashed. */
  backEdgeIds: Set<string>
  /** true: the simulation keeps running, so a step can be dragged. false: it settles once and stays still. */
  live: boolean
  /** The board is full screen. */
  expanded: boolean
  /** The selected step, which the graph pans to. */
  selectedId: string | null
  /** The steps just before and after the selected one, marked on the graph too. */
  trace: StepTrace | null
  /** The number each step wears, the same as its row in the list. */
  numberById: Map<string, string>
  marks?: Record<string, 'current' | 'passed'>
  /** Set by whoever selected a step without a click (a run, an added step): the graph stays fitted instead. */
  skipPan: MutableRefObject<boolean>
}

export default function LivingFlow({
  steps, edges, backEdgeIds, live, expanded, selectedId, trace, numberById, marks, skipPan,
}: LivingFlowProps) {
  const boardRef = useRef<HTMLDivElement>(null)
  const instanceRef = useRef<ReactFlowInstance | null>(null)
  // What cancels the first fit's wait (./framing).
  const framing = useRef<(() => void) | null>(null)
  // Held back until the first fit lands, so the correction is never on screen.
  const [framed, setFramed] = useState(false)

  // The diagram is laid out fresh from steps + edges by dagre, which only seeds the Living layout's first frame.
  // The layout is only laid out again when the shape of the process changes (a step or line added, removed or
  // rewired), not when a step's words are edited, so the graph stays where it is while someone types.
  const shapeKey = steps.map(s => `${s.id}:${s.step_type}:${s.attached_to_step_id ?? ''}`).join('|')
    + '||' + edges.map(e => `${e.id}:${e.source_step_id}>${e.target_step_id}`).join('|')
  const edgesShape = useMemo(() => edges.map(edgeToFlow), [shapeKey]) // eslint-disable-line react-hooks/exhaustive-deps
  const nodesShape = useMemo(() => layoutWithDagre(steps.map(stepToNode), edgesShape), [shapeKey, edgesShape]) // eslint-disable-line react-hooks/exhaustive-deps
  // The words on each node are current even when the shape is the one laid out.
  const nodesRaw = useMemo(() => {
    const byId = new Map(steps.map(s => [s.id, s]))
    return nodesShape.map(n => {
      const step = byId.get(n.id)
      return step ? { ...n, data: { ...n.data, ...stepToNode(step).data } } : n
    })
  }, [nodesShape, steps])
  const degrees = useMemo(() => computeDegrees(edgesShape), [edgesShape])
  // A boundary event's only real connection is its host; these synthetic
  // links exist only for the physics (never rendered, never counted in degree).
  const livingLinks = useMemo(() => {
    const attachmentLinks: Edge[] = []
    for (const n of nodesShape) {
      const host = nodeAttachedHost(n)
      if (host) attachmentLinks.push({ id: `attach-${n.id}`, source: host, target: n.id })
    }
    return attachmentLinks.length === 0 ? edgesShape : [...edgesShape, ...attachmentLinks]
  }, [edgesShape, nodesShape])
  const seedNodes = useMemo(() => seedForWideBoard(nodesShape), [nodesShape])
  const liveLayout = useLivingLayout(seedNodes, livingLinks, degrees, live, boardRef)
  const settledPositions = useSettledLivingLayout(seedNodes, livingLinks, degrees, !live)
  const positions = live ? liveLayout.positions : settledPositions

  // Every step's current centre + radius, from the same position values the
  // node renders from this render -- edges read this instead of React Flow's
  // own node store, which lags a render behind.
  const geometry = useMemo(() => {
    const m = new Map<string, { x: number; y: number; r: number }>()
    for (const n of nodesRaw) {
      const pos = positions.get(n.id) ?? n.position
      const r = diameterFromDegree(degrees.degreeById.get(n.id) ?? 0, degrees.maxDegree) / 2
      m.set(n.id, { x: pos.x + r, y: pos.y + r, r })
    }
    return m
  }, [nodesRaw, positions, degrees])
  const flowEdges = useMemo(
    () => styleFlowEdges(edgesShape, backEdgeIds).map(e => ({
      ...e,
      data: { ...e.data, livingSource: geometry.get(e.source), livingTarget: geometry.get(e.target) },
    })),
    [edgesShape, backEdgeIds, geometry]
  )
  // Drawn separately: an attachment isn't a sequence-flow edge, but it still
  // gets a thin, arrowhead-less line so the relationship doesn't disappear.
  const attachmentEdges = useMemo(() => {
    const out: Edge[] = []
    for (const n of nodesRaw) {
      const host = nodeAttachedHost(n)
      if (!host) continue
      out.push({
        id: `attach-${n.id}`,
        source: host,
        target: n.id,
        type: 'block',
        selectable: false,
        data: { isAttachment: true, livingSource: geometry.get(host), livingTarget: geometry.get(n.id) },
      })
    }
    return out
  }, [geometry, nodesRaw])
  // One array, rebuilt only when one of its two halves is: React Flow does its bookkeeping again for a new one.
  const allEdges = useMemo(() => [...flowEdges, ...attachmentEdges], [flowEdges, attachmentEdges])

  // The diagram mirrors whichever step is selected in the list, and every circle wears the same number its row wears.
  const flowNodes = useMemo<Node[]>(
    () => nodesRaw.map(n => ({
      ...n,
      position: positions.get(n.id) ?? n.position,
      // The size the circle is drawn at, given to React Flow so the graph has bounds before (or without) any
      // measurement: a hidden tab never measures.
      width: diameterFromDegree(degrees.degreeById.get(n.id) ?? 0, degrees.maxDegree),
      height: diameterFromDegree(degrees.degreeById.get(n.id) ?? 0, degrees.maxDegree),
      draggable: live,
      selected: n.id === selectedId,
      data: {
        ...n.data,
        mark: marks?.[n.id],
        num: numberById.get(n.id),
        traced: Boolean(trace && (trace.predecessorIds.has(n.id) || trace.successorIds.has(n.id))),
        degree: degrees.degreeById.get(n.id) ?? 0,
        maxDegree: degrees.maxDegree,
      },
    })),
    [nodesRaw, selectedId, trace, numberById, live, positions, degrees, marks]
  )

  // Anchored a quarter of the way down, so the steps that follow the
  // selected one are in view too.
  function centerOnStep(stepId: string | undefined, opts?: { duration?: number }) {
    const node = nodesRaw.find(n => n.id === stepId)
    const instance = instanceRef.current
    const board = boardRef.current
    if (!node || !instance || !board) return
    const { width, height } = board.getBoundingClientRect()
    const zoom = instance.getZoom()
    const position = positions.get(node.id) ?? node.position
    const centerX = position.x + BLOCK_W / 2
    const centerY = position.y + BLOCK_H / 2
    instance.setViewport(
      { x: width / 2 - centerX * zoom, y: height * 0.25 - centerY * zoom, zoom },
      { duration: opts?.duration ?? 0 }
    )
  }

  const boardSize = (): BoardSize => {
    const rect = boardRef.current?.getBoundingClientRect()
    return { width: rect?.width ?? 0, height: rect?.height ?? 0 }
  }
  const hooks = { size: boardSize, reveal: () => setFramed(true) }

  // Waits for React Flow to measure the nodes, fits the whole graph once with no animation, and only then shows the
  // board; a timer shows it whatever happens (./framing).
  function frame(instance: ReactFlowInstance) {
    framing.current?.()
    framing.current = frameWhenMeasured(instance, hooks)
  }
  useEffect(() => () => { framing.current?.() }, [])

  // When the shape of the process changes under the reader (a step added, taken out or moved in the editor), the
  // layout moves the circles about; fit the whole graph once it has had a moment to settle.
  const shapeSeen = useRef(shapeKey)
  useEffect(() => {
    if (shapeSeen.current === shapeKey) return
    shapeSeen.current = shapeKey
    const timer = setTimeout(() => {
      if (instanceRef.current) fitWhole(instanceRef.current, boardSize(), 300)
    }, 700)
    return () => clearTimeout(timer)
  }, [shapeKey])

  // Whichever step is selected in the list, pan the diagram to it too.
  useEffect(() => {
    if (!selectedId) return
    if (skipPan.current) skipPan.current = false
    else centerOnStep(selectedId, { duration: 300 })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId])

  // Local addition (PROVENANCE.md): a settled (read-only) graph is fitted again whenever its layout changes after
  // the first fit -- a run's view, or a different process in the same board -- so the whole graph always sits in the
  // box instead of keeping the first fit's frame.
  // Resumebot divergence (PROVENANCE.md): a live graph is fitted once too, when its first positions arrive. Until then
  // the circles stand where dagre put them (a tall column), so the first fit, made as soon as React Flow was up, fitted
  // the wrong shape; the old 41-frame wait only hid that.
  const liveFitted = useRef(false)
  useEffect(() => {
    if (live) {
      if (positions.size === 0) liveFitted.current = false
      if (positions.size === 0 || liveFitted.current) return
      liveFitted.current = true
    }
    if (instanceRef.current) frame(instanceRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, positions])

  // Resumebot divergence (PROVENANCE.md): entering or leaving full screen fits the whole graph to the board's new size
  // (it had re-anchored on the first or selected step, which left the graph off-centre). Done in the same commit that
  // changes the board's size, from the board's own size, so it needs no wait for React Flow's resize observer.
  const expandedSeen = useRef(expanded)
  useLayoutEffect(() => {
    if (expandedSeen.current === expanded) return
    expandedSeen.current = expanded
    if (instanceRef.current) fitWhole(instanceRef.current, boardSize())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded])

  return (
    <div ref={boardRef} className={`read-flow-board${expanded ? ' read-flow-board-expanded' : ''}${framed ? '' : ' pw-flow-framing'}`}>
      <ReactFlow
        // Living's nodes are circles sized to their own connectivity, not
        // fixed-width blocks -- this opts the canvas out of the 160px
        // min-width those blocks need.
        className="pw-living-flow"
        nodes={flowNodes}
        edges={allEdges}
        minZoom={FLOW_MIN_ZOOM}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        defaultEdgeOptions={EDGE_DEFAULT_OPTIONS}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        onNodeDragStart={(_event, node) => {
          // Held state goes on the board as a plain class, not React state.
          boardRef.current?.classList.add('pw-grabbing')
          if (live) liveLayout.onDragStart(node.id)
        }}
        onNodeDrag={(_event, node) => {
          if (live) liveLayout.onDrag(node.id, node.position.x, node.position.y)
        }}
        onNodeDragStop={(_event, node) => {
          boardRef.current?.classList.remove('pw-grabbing')
          if (live) liveLayout.onDragStop(node.id)
        }}
        // React Flow's own `fitView` prop fits on the first render, when
        // nodes have no measured size yet. So wait for real measurements,
        // fit once with no animation, and only then show the board.
        onInit={instance => {
          instanceRef.current = instance
          frame(instance)
        }}
        proOptions={PRO_OPTIONS}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}
