// What the Process flow graph is built from: the nodes and edges React Flow is given, a dagre layout to seed the
// Living layout's first frame, and the kind of each step in words. The Living kit (livingGraphKit) draws them.
//
// This file used to hold the flowchart (square blocks, thick connectors routed along lanes) that the Living graph
// replaced; that drawing code is gone, and only what the Living graph still reads is kept. See PROVENANCE.md.
import type { Edge, Node } from '@xyflow/react'
import dagre from 'dagre'
import type { ProcessStep, ProcessEdge } from './types'

export const BLOCK_W = 190
export const BLOCK_H = 84

export type BlockKind = 'task' | 'decision' | 'event' | 'terminal' | 'subprocess'

// The Steps list states in words the same "what kind of thing is this" the
// diagram answers with colour or shape. This is the one place that mapping
// is named directly, so the outline's badge can't drift from what the
// Living graph actually renders (livingGraphKit's kindFor). Every kind the
// diagram distinguishes at all gets its matching badge treatment here too:
// decision and event by colour, terminal (a thick border) and subprocess (a
// double ring) by the same non-colour treatment the diagram itself uses for
// them. Only task gets nothing, because the diagram gives task nothing
// either.
export function stepKindBadge(stepType: string): 'decision' | 'event' | 'terminal' | 'subprocess' | 'task' | null {
  switch (stepType) {
    case 'gateway_exclusive':
    case 'gateway_parallel':
      return 'decision'
    case 'event_intermediate':
    case 'event_boundary':
      return 'event'
    case 'event_end':
      return 'terminal'
    case 'subprocess':
      return 'subprocess'
    case 'event_start':
      return 'event'
    case 'task':
      return 'task'
    default:
      return null
  }
}

// A boundary event has no real sequence-flow edge to its host (BPMN reaches
// it by attachment, not by an arrow), so this is the only relationship
// connecting it to the rest of the process at all. layoutWithDagre uses it
// for ranking; Living's force simulation needs the same thing so an
// attached event doesn't drift away as an unconnected component with
// nothing pulling it back.
export function nodeAttachedHost(n: Node): string | undefined {
  return (n.data as { step?: { attached_to_step_id?: string | null } } | undefined)?.step?.attached_to_step_id ?? undefined
}

// Every edge is drawn by the Living graph's one edge type. A back edge --
// one whose target already precedes its source in the flow, from the Steps
// list's own analysis -- gets a dashed stroke; nothing else about it changes,
// so a loop still reads as a loop without a special colour.
export function styleFlowEdges(edges: Edge[], backEdgeIds: Set<string>): Edge[] {
  return edges.map(e => ({
    ...e,
    type: 'block',
    data: { ...e.data, isBack: backEdgeIds.has(e.id) },
  }))
}

// A layered-graph layout of the nodes, used only to seed the first frame of
// the Living layout (ranking, ordering and separation are dagre's job).
export function layoutWithDagre(nodes: Node[], edges: Edge[]): Node[] {
  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'TB', ranker: 'longest-path', nodesep: 40, ranksep: 90, edgesep: 24 })
  g.setDefaultEdgeLabel(() => ({}))
  nodes.forEach(n => g.setNode(n.id, { width: BLOCK_W, height: BLOCK_H }))
  edges.forEach(e => { if (!g.hasEdge(e.source, e.target)) g.setEdge(e.source, e.target) })
  // A boundary event carries no sequence-flow edge of its own into it -- it
  // is reached by attachment, not by an arrow -- so dagre had nothing
  // tying it to the activity it hangs off and ranked it whatever its own
  // escape path happened to imply, often nowhere near its host. This edge
  // is for ranking only: it is never added to the real edge list, so
  // nothing renders it.
  nodes.forEach(n => {
    const host = nodeAttachedHost(n)
    if (host && g.hasNode(host) && !g.hasEdge(host, n.id)) g.setEdge(host, n.id)
  })
  dagre.layout(g)

  return nodes.map(n => {
    const p = g.node(n.id)
    if (!p) return n
    return { ...n, position: { x: p.x - BLOCK_W / 2, y: p.y - BLOCK_H / 2 } }
  })
}

export function stepToNode(step: ProcessStep): Node {
  return {
    id: step.id,
    type: step.step_type,
    position: { x: step.position_x, y: step.position_y },
    data: {
      label: step.label || step.description || 'Untitled',
      stepType: step.step_type,
      step,
    },
  }
}

export function edgeToFlow(edge: ProcessEdge): Edge {
  return {
    id: edge.id,
    source: edge.source_step_id,
    target: edge.target_step_id,
    label: edge.label || edge.condition || undefined,
    data: { edge },
  }
}
