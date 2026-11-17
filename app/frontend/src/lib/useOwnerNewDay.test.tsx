import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useOwnerNewDay } from "./useOwnerNewDay";

// The owner's zone is Pacific/Kiritimati (UTC+14, test-setup.ts): their midnight is 10:00 UTC. The browser's own midnight
// (Pacific/Pago_Pago, 11:00 UTC the next day) is a day away, so a timer set by the browser's clock would not fire.
describe("the owner's new day", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-01T09:59:30Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("is told at the owner's midnight, once", () => {
    const onNewDay = vi.fn();
    renderHook(() => useOwnerNewDay(onNewDay));
    act(() => vi.advanceTimersByTime(29_000));
    expect(onNewDay).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(2_000));
    expect(onNewDay).toHaveBeenCalledTimes(1);
    act(() => vi.advanceTimersByTime(3_600_000));
    expect(onNewDay).toHaveBeenCalledTimes(1);
  });

  it("is told when the tab becomes visible on a new day, though its timer slept through the midnight", () => {
    const onNewDay = vi.fn();
    renderHook(() => useOwnerNewDay(onNewDay));
    vi.setSystemTime(new Date("2026-10-01T10:05:00Z"));
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(onNewDay).toHaveBeenCalledTimes(1);
  });

  it("is not told when the tab becomes visible on the same day", () => {
    const onNewDay = vi.fn();
    renderHook(() => useOwnerNewDay(onNewDay));
    vi.setSystemTime(new Date("2026-10-01T09:59:50Z"));
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(onNewDay).not.toHaveBeenCalled();
  });
});
