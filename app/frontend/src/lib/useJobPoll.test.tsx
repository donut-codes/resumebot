import { act, renderHook } from "@testing-library/react";
import type { LiveArea } from "../api/types/pipeline";
import { fakeLiveHub, flushHub, liveWrapper } from "./liveTestHub";
import { useJobPoll, type JobAnswer } from "./useJobPoll";

const words = { failed: "It failed.", lost: "It was lost.", touch: "No answer." };

function mount(area: LiveArea) {
  const fake = fakeLiveHub();
  const load = vi.fn(async (): Promise<JobAnswer> => ({ state: "running" }));
  const view = renderHook(() => useJobPoll(load, words, undefined, area), { wrapper: liveWrapper(fake.hub) });
  return { fake, load, view };
}

describe("useJobPoll", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("with an area, reads once at begin, then once per changed, per reconnect and per resync, with no check timer", async () => {
    const { fake, load, view } = mount("jobs");
    act(() => view.result.current.begin("j1"));
    await flushHub();
    expect(load).toHaveBeenCalledTimes(1);
    expect(load).toHaveBeenCalledWith("j1");
    // The seconds clock runs; nothing else asks.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(load).toHaveBeenCalledTimes(1);
    await act(async () => {
      fake.push("changed", { area: "jobs", ids: ["j1"] });
    });
    expect(load).toHaveBeenCalledTimes(2);
    await act(async () => {
      fake.push("changed", { area: "gaps", ids: [] });
    });
    expect(load).toHaveBeenCalledTimes(2);
    await act(async () => {
      fake.setState("open");
    });
    expect(load).toHaveBeenCalledTimes(3);
    await act(async () => {
      fake.push("resync", {});
    });
    expect(load).toHaveBeenCalledTimes(4);
  });

  it("stops reading once the job is done", async () => {
    const { fake, load, view } = mount("gaps");
    act(() => view.result.current.begin("g1"));
    await flushHub();
    load.mockResolvedValueOnce({ state: "done" });
    await act(async () => {
      fake.push("changed", { area: "gaps", ids: [] });
    });
    expect(view.result.current.status?.state).toBe("done");
    expect(load).toHaveBeenCalledTimes(2);
    await act(async () => {
      fake.push("changed", { area: "gaps", ids: [] });
      fake.setState("open");
    });
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("tells the failure of a failed job in the job's words", async () => {
    const { fake, load, view } = mount("jobs");
    act(() => view.result.current.begin("j1"));
    await flushHub();
    load.mockResolvedValueOnce({ state: "failed", error: null });
    await act(async () => {
      fake.push("changed", { area: "jobs", ids: [] });
    });
    expect(view.result.current.failure).toBe("It failed.");
  });
});
