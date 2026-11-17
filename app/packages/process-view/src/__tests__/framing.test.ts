// The Process flow board's first fit and its reveal (../framing.ts). Resumebot divergence: see app/PROVENANCE.md.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  FLOW_MAX_ZOOM, FLOW_MIN_ZOOM, FRAME_FALLBACK_MS, FRAME_MAX_FRAMES, fitWhole, frameWhenMeasured, nodesMeasured,
} from '../framing'
import { NUM_MIN_PX, numberBoxPx, numberFontPx } from '../livingGraphKit'

type Bounds = { x: number; y: number; width: number; height: number }
type Fake = Parameters<typeof frameWhenMeasured>[0] & { setViewport: ReturnType<typeof vi.fn> }

/** A stand-in for React Flow's instance: controlled nodes (no `measured`, as React Flow leaves them) and internal
 *  nodes that report a size once `measured()` says so. */
function fakeInstance(opts: { ids?: string[]; bounds?: Bounds; measured?: () => boolean } = {}): Fake {
  const ids = opts.ids ?? ['a', 'b']
  const measured = opts.measured ?? (() => true)
  return {
    getNodes: () => ids.map(id => ({ id, position: { x: 0, y: 0 }, data: {} })),
    getInternalNode: (id: string) =>
      ids.includes(id) ? { measured: { width: measured() ? 46 : undefined, height: measured() ? 46 : undefined } } : undefined,
    getNodesBounds: () => opts.bounds ?? { x: 0, y: 0, width: 400, height: 200 },
    setViewport: vi.fn(async () => true),
  } as unknown as Fake
}

const size = { width: 1000, height: 600 }
const FRAMES = ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame'] as const

describe('nodesMeasured', () => {
  it('reads the size from the internal node, not the controlled one (which React Flow never writes)', () => {
    expect(nodesMeasured(fakeInstance({ measured: () => true }))).toBe(true)
    expect(nodesMeasured(fakeInstance({ measured: () => false }))).toBe(false)
  })

  it('is false for a graph with no nodes', () => {
    expect(nodesMeasured(fakeInstance({ ids: [] }))).toBe(false)
  })
})

describe('fitWhole', () => {
  const viewportOf = (instance: Fake) => (instance.setViewport.mock.calls[0] as unknown as [{ x: number; y: number; zoom: number }])[0]

  it('centres the whole graph in the board', () => {
    const instance = fakeInstance({ bounds: { x: 100, y: 50, width: 400, height: 300 } })
    expect(fitWhole(instance, size)).toBe(true)
    const { x, y, zoom } = viewportOf(instance)
    expect((size.width / 2 - x) / zoom).toBeCloseTo(100 + 200, 5)
    expect((size.height / 2 - y) / zoom).toBeCloseTo(50 + 150, 5)
    // The whole graph is inside the board, with room round it.
    expect(400 * zoom).toBeLessThan(size.width)
    expect(300 * zoom).toBeLessThan(size.height)
  })

  it("fits a tall graph below React Flow's default floor of 0.5, and no further than the floor", () => {
    const instance = fakeInstance({ bounds: { x: 0, y: 0, width: 200, height: 4000 } })
    fitWhole(instance, size)
    const { zoom } = viewportOf(instance)
    expect(zoom).toBeLessThan(0.5)
    expect(zoom).toBeGreaterThanOrEqual(FLOW_MIN_ZOOM)
    expect(4000 * zoom).toBeLessThan(size.height)
  })

  it('does not blow a small graph up past the maximum zoom', () => {
    const instance = fakeInstance({ bounds: { x: 0, y: 0, width: 60, height: 60 } })
    fitWhole(instance, size)
    expect(viewportOf(instance).zoom).toBe(FLOW_MAX_ZOOM)
  })

  it('does nothing without nodes, without bounds or in a board with no size', () => {
    const none = fakeInstance({ ids: [] })
    const flat = fakeInstance({ bounds: { x: 0, y: 0, width: 0, height: 0 } })
    const hidden = fakeInstance()
    expect(fitWhole(none, size)).toBe(false)
    expect(fitWhole(flat, size)).toBe(false)
    expect(fitWhole(hidden, { width: 0, height: 0 })).toBe(false)
    expect(none.setViewport).not.toHaveBeenCalled()
    expect(flat.setViewport).not.toHaveBeenCalled()
    expect(hidden.setViewport).not.toHaveBeenCalled()
  })
})

describe('frameWhenMeasured', () => {
  let realRaf: typeof requestAnimationFrame
  let realCancel: typeof cancelAnimationFrame

  beforeEach(() => {
    realRaf = globalThis.requestAnimationFrame
    realCancel = globalThis.cancelAnimationFrame
  })
  afterEach(() => {
    vi.useRealTimers()
    globalThis.requestAnimationFrame = realRaf
    globalThis.cancelAnimationFrame = realCancel
  })

  it('shows the board from a timer when the tab never runs a frame (a hidden or throttled tab)', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    globalThis.requestAnimationFrame = () => 1 // a callback that is never called
    globalThis.cancelAnimationFrame = () => {}
    const instance = fakeInstance({ measured: () => false })
    const reveal = vi.fn()
    frameWhenMeasured(instance, { size: () => size, reveal })
    vi.advanceTimersByTime(FRAME_FALLBACK_MS - 1)
    expect(reveal).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(reveal).toHaveBeenCalledTimes(1)
    // And it was fitted, on the sizes the nodes were given.
    expect(instance.setViewport).toHaveBeenCalledTimes(1)
  })

  it('fits as soon as the nodes are measured, before the timer, and only once', () => {
    vi.useFakeTimers({ toFake: [...FRAMES] })
    let frames = 0
    const instance = fakeInstance({ measured: () => frames >= 3 })
    const reveal = vi.fn()
    frameWhenMeasured(instance, { size: () => size, reveal })
    for (frames = 1; frames <= 2; frames++) {
      vi.advanceTimersByTime(16)
      expect(reveal).not.toHaveBeenCalled()
    }
    frames = 3
    vi.advanceTimersByTime(16)
    expect(reveal).toHaveBeenCalledTimes(1)
    expect(instance.setViewport).toHaveBeenCalledTimes(1)
    // The timer was cleared with the wait: nothing happens again.
    vi.advanceTimersByTime(5000)
    expect(reveal).toHaveBeenCalledTimes(1)
    expect(instance.setViewport).toHaveBeenCalledTimes(1)
  })

  it('still shows the board when nodes never report a size, after the frames it allows', () => {
    vi.useFakeTimers({ toFake: [...FRAMES] })
    const instance = fakeInstance({ measured: () => false })
    const reveal = vi.fn()
    // The frame bound (about 660ms of frames) is past the 400ms timer, so make the timer long to reach it.
    frameWhenMeasured(instance, { size: () => size, reveal }, { fallbackMs: 60_000 })
    vi.advanceTimersByTime(16 * (FRAME_MAX_FRAMES + 2))
    expect(reveal).toHaveBeenCalledTimes(1)
  })

  it('fits again when the tab becomes visible, if the first fit was made on nodes that were never measured', () => {
    vi.useFakeTimers({ toFake: [...FRAMES] })
    let measured = false
    const instance = fakeInstance({ measured: () => measured })
    frameWhenMeasured(instance, { size: () => size, reveal: () => {} }, { fallbackMs: 100 })
    vi.advanceTimersByTime(100)
    expect(instance.setViewport).toHaveBeenCalledTimes(1)
    measured = true
    document.dispatchEvent(new Event('visibilitychange'))
    vi.advanceTimersByTime(16)
    expect(instance.setViewport).toHaveBeenCalledTimes(2)
  })

  it('does nothing once cancelled', () => {
    vi.useFakeTimers({ toFake: [...FRAMES] })
    const instance = fakeInstance({ measured: () => false })
    const reveal = vi.fn()
    const cancel = frameWhenMeasured(instance, { size: () => size, reveal })
    cancel()
    vi.advanceTimersByTime(10_000)
    expect(reveal).not.toHaveBeenCalled()
    expect(instance.setViewport).not.toHaveBeenCalled()
  })
})

describe('the step number on a node', () => {
  // The number is broken after each dot; these are the lines it makes at a size (the em the sizing uses).
  function lines(num: string, px: number, side: number): string[] {
    const perLine = Math.floor(side / (0.62 * px))
    const out: string[] = []
    for (const chunk of num.split(/(?<=\.)/)) {
      if (out.length > 0 && out[out.length - 1].length + chunk.length <= perLine) out[out.length - 1] += chunk
      else out.push(chunk)
    }
    return out
  }

  it.each(['4b.1', '4b.2a.1', '4b.2a.2b.1', '4b.2a.2b.4c.1'])('sizes "%s" so its lines fit the smallest node', num => {
    const diameter = 46
    const side = numberBoxPx(diameter)
    const px = numberFontPx(num, diameter)
    expect(px).toBeGreaterThanOrEqual(NUM_MIN_PX)
    const made = lines(num, px, side)
    for (const line of made) expect(line.length * 0.62 * px).toBeLessThanOrEqual(side + 0.001)
    expect(made.length * px * 1.1).toBeLessThanOrEqual(side + 0.001)
  })

  it('leaves a short number its full size and gives a longer one a smaller one', () => {
    expect(numberFontPx('1', 46)).toBeGreaterThanOrEqual(16)
    expect(numberFontPx('4b.2a.2b.4c.1', 46)).toBeLessThan(numberFontPx('4b.1', 46))
    expect(numberFontPx('4b.2a.2b.4c.1', 46)).toBeLessThan(numberFontPx('4b.2a.2b.4c.1', 88))
  })
})
