import { act, renderHook } from "@testing-library/react";
import { api } from "../api/client";
import { fakeLiveHub, flushHub, liveWrapper } from "./liveTestHub";
import { useAgentReady } from "./useAgentReady";

describe("useAgentReady", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("asks once, again on changed ready, and sets no timer", async () => {
    vi.useFakeTimers();
    const ask = vi.spyOn(api, "agentReady").mockResolvedValue({ ready: true, why: null } as never);
    const fake = fakeLiveHub();
    renderHook(() => useAgentReady(), { wrapper: liveWrapper(fake.hub) });
    await flushHub();
    expect(ask).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    expect(ask).toHaveBeenCalledTimes(1);
    act(() => fake.push("changed", { area: "ready", ids: [] }));
    expect(ask).toHaveBeenCalledTimes(2);
    act(() => fake.push("changed", { area: "gaps", ids: [] }));
    expect(ask).toHaveBeenCalledTimes(2);
  });
});
