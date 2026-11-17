// Fitting the Process flow graph to its board, and showing the board once it is fitted.
//
// Resumebot divergence (app/PROVENANCE.md, "process-view framing"). The first version waited for React Flow to
// report each node's size (`getNodes()[i].measured`), which React Flow never writes onto a controlled node, so it
// always waited the full 41 frames and then fitted on nodes it could not see; and it showed the board only from a
// requestAnimationFrame callback, which a hidden or throttled tab never runs, leaving the board an empty dotted canvas
// (the viewport is held at opacity 0 until the first fit lands).
//
// Now: the measured size is read where React Flow keeps it (the internal node), the fit is computed from the board's
// own size and the nodes' bounds (which use the size each node is given, so they exist before any measurement), and a
// timer always ends the wait, so the board is shown whatever the tab does.
import { getViewportForBounds, type ReactFlowInstance } from '@xyflow/react'

// A tall graph (a long process drawn top to bottom) needs a zoom far below React Flow's default floor of 0.5 to fit.
export const FLOW_MIN_ZOOM = 0.05
// A small graph must not be blown up to fill the board: the text in it would be larger than the page's own.
export const FLOW_MAX_ZOOM = 1.5
// Share of the board's width and height kept clear round the graph.
export const FIT_PADDING = 0.12
// How long the first fit waits for measured nodes before it shows the board anyway. A timer, not a frame: a hidden tab
// runs timers (slowly) and no frames.
export const FRAME_FALLBACK_MS = 400
// Frames to wait for measurement in a tab that does run them.
export const FRAME_MAX_FRAMES = 40

export interface BoardSize { width: number; height: number }

type Measuring = Pick<ReactFlowInstance, 'getNodes' | 'getInternalNode'>
type Fitting = Measuring & Pick<ReactFlowInstance, 'getNodesBounds' | 'setViewport'>

/** True when React Flow has measured every node (its size is on the internal node, never on the controlled one). */
export function nodesMeasured(instance: Measuring): boolean {
  const nodes = instance.getNodes()
  return nodes.length > 0 && nodes.every(n => (instance.getInternalNode(n.id)?.measured.width ?? 0) > 0)
}

/** Sets the viewport so the whole graph sits in the board, centred, within the zoom limits. False when there is
 *  nothing to fit to (no nodes, or a board with no size). */
export function fitWhole(instance: Fitting, size: BoardSize, duration = 0): boolean {
  const nodes = instance.getNodes()
  if (nodes.length === 0 || size.width <= 0 || size.height <= 0) return false
  const bounds = instance.getNodesBounds(nodes)
  if (bounds.width <= 0 || bounds.height <= 0) return false
  const viewport = getViewportForBounds(bounds, size.width, size.height, FLOW_MIN_ZOOM, FLOW_MAX_ZOOM, FIT_PADDING)
  void instance.setViewport(viewport, { duration })
  return true
}

export interface FrameHooks {
  /** The board's size now. */
  size: () => BoardSize
  /** Shows the board (called once the fit is made, or the wait is over). */
  reveal: () => void
}

export interface FrameTiming { fallbackMs?: number; maxFrames?: number }

/**
 * Waits for the nodes to be measured, fits the whole graph with no animation and shows the board. Always shows it: a
 * timer ends the wait if the frames never come (a hidden tab) or the nodes never report a size. A fit made on
 * unmeasured nodes (the sizes the nodes were given) is made again when the tab becomes visible. Returns what cancels it.
 */
export function frameWhenMeasured(instance: Fitting, hooks: FrameHooks, timing: FrameTiming = {}): () => void {
  const fallbackMs = timing.fallbackMs ?? FRAME_FALLBACK_MS
  const maxFrames = timing.maxFrames ?? FRAME_MAX_FRAMES
  let raf: number | null = null
  let timer: ReturnType<typeof setTimeout> | null = null
  let stopped = false
  let again: (() => void) | null = null
  let onVisible: (() => void) | null = null

  const stopWaiting = () => {
    if (raf !== null) cancelAnimationFrame(raf)
    if (timer !== null) clearTimeout(timer)
    raf = null
    timer = null
  }
  const settle = (measured: boolean) => {
    if (stopped) return
    stopWaiting()
    stopped = true
    fitWhole(instance, hooks.size())
    hooks.reveal()
    if (measured || typeof document === 'undefined') return
    onVisible = () => {
      if (document.hidden) return
      document.removeEventListener('visibilitychange', onVisible!)
      onVisible = null
      again = frameWhenMeasured(instance, hooks, timing)
    }
    document.addEventListener('visibilitychange', onVisible)
  }

  let frames = 0
  const step = () => {
    raf = null
    if (nodesMeasured(instance)) return settle(true)
    if (++frames > maxFrames) return settle(false)
    raf = requestAnimationFrame(step)
  }
  raf = requestAnimationFrame(step)
  timer = setTimeout(() => settle(nodesMeasured(instance)), fallbackMs)

  return () => {
    stopped = true
    stopWaiting()
    if (onVisible) document.removeEventListener('visibilitychange', onVisible)
    onVisible = null
    again?.()
    again = null
  }
}
