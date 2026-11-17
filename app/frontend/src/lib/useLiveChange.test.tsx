import { act, renderHook } from "@testing-library/react";
import type { LiveArea } from "../api/types/pipeline";
import { fakeLiveHub, flushHub, liveWrapper } from "./liveTestHub";
import { useLiveChange } from "./useLiveChange";

function mount(area: LiveArea | undefined, options?: { id?: string; enabled?: boolean }) {
  const fake = fakeLiveHub();
  const onChange = vi.fn();
  const view = renderHook(() => useLiveChange(area, onChange, options), { wrapper: liveWrapper(fake.hub) });
  return { fake, onChange, view };
}

describe("useLiveChange", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("calls once per changed of its area and ignores another area", async () => {
    const { fake, onChange } = mount("gaps");
    await flushHub();
    expect(fake.streams).toHaveLength(1);
    expect(fake.streams[0].topics).toEqual(["changed"]);
    act(() => fake.push("changed", { area: "gaps", ids: ["g1"] }));
    expect(onChange).toHaveBeenCalledTimes(1);
    act(() => fake.push("changed", { area: "jobs", ids: [] }));
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("with an id, ignores a changed that names other ids and takes one that names it or none", async () => {
    const { fake, onChange } = mount("jobs", { id: "j1" });
    await flushHub();
    act(() => fake.push("changed", { area: "jobs", ids: ["j2"] }));
    expect(onChange).toHaveBeenCalledTimes(0);
    act(() => fake.push("changed", { area: "jobs", ids: ["j1", "j2"] }));
    expect(onChange).toHaveBeenCalledTimes(1);
    act(() => fake.push("changed", { area: "jobs", ids: [] }));
    expect(onChange).toHaveBeenCalledTimes(2);
  });

  it("calls once per resync", async () => {
    const { fake, onChange } = mount("gaps");
    await flushHub();
    act(() => fake.push("resync", {}));
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("calls once each time the stream opens again, not when it drops", async () => {
    const { fake, onChange } = mount("gaps");
    await flushHub();
    act(() => fake.setState("open"));
    expect(onChange).toHaveBeenCalledTimes(1);
    act(() => fake.setState("closed"));
    expect(onChange).toHaveBeenCalledTimes(1);
    act(() => fake.setState("open"));
    expect(onChange).toHaveBeenCalledTimes(2);
  });

  it("does not call when the stream was already open at mount", async () => {
    const first = mount("gaps");
    await flushHub();
    act(() => first.fake.setState("open"));
    const second = vi.fn();
    renderHook(() => useLiveChange("jobs", second), { wrapper: liveWrapper(first.fake.hub) });
    await flushHub();
    expect(second).not.toHaveBeenCalled();
    act(() => first.fake.setState("closed"));
    act(() => first.fake.setState("open"));
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("opens no stream when disabled or given no area", async () => {
    const off = mount("gaps", { enabled: false });
    const none = mount(undefined);
    await flushHub();
    expect(off.fake.streams).toHaveLength(0);
    expect(none.fake.streams).toHaveLength(0);
  });

  it("creates no timer", async () => {
    vi.useFakeTimers();
    const { onChange } = mount("gaps");
    await flushHub();
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(onChange).not.toHaveBeenCalled();
  });
});
