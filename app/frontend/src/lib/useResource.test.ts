import { act, renderHook, waitFor } from "@testing-library/react";
import { ApiError } from "../api/client";
import { useResource } from "./useResource";
import { fakeLiveHub, flushHub, liveWrapper } from "./liveTestHub";

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("useResource", () => {
  it("loads on mount and hands back the data", async () => {
    const { result } = renderHook(() => useResource(() => Promise.resolve("goals"), []));
    expect(result.current.data).toBeNull();
    await waitFor(() => expect(result.current.data).toBe("goals"));
    expect(result.current.error).toBeNull();
  });

  it("shows the server's words on a failed load, or the fallback, and clears them when a load works", async () => {
    let attempt = 0;
    const load = () => {
      attempt += 1;
      if (attempt === 1) return Promise.reject(new ApiError("Not yours to see.", 403));
      if (attempt === 2) return Promise.reject(new Error("boom"));
      return Promise.resolve("ok");
    };
    const { result } = renderHook(() => useResource(load, [], { fallback: "Couldn't load that." }));
    await waitFor(() => expect(result.current.error).toBe("Not yours to see."));
    act(() => result.current.reload());
    await waitFor(() => expect(result.current.error).toBe("Couldn't load that."));
    act(() => result.current.reload());
    await waitFor(() => expect(result.current.data).toBe("ok"));
    expect(result.current.error).toBeNull();
  });

  it("keeps a failure quiet when no fallback is given, and reports it to onError", async () => {
    const onError = vi.fn();
    const { result } = renderHook(() => useResource(() => Promise.reject(new Error("x")), [], { onError }));
    await waitFor(() => expect(onError).toHaveBeenCalledTimes(1));
    expect(result.current.error).toBeNull();
  });

  it("loads again when a dep changes and drops the answer that was still on its way", async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    const { result, rerender } = renderHook(({ id }) => useResource(() => (id === 1 ? first.promise : second.promise), [id]), {
      initialProps: { id: 1 },
    });
    rerender({ id: 2 });
    await act(async () => {
      second.resolve("second");
      await second.promise;
    });
    await act(async () => {
      first.resolve("first");
      await first.promise;
    });
    expect(result.current.data).toBe("second");
  });

  it("drops an answer that lands after the screen closed", async () => {
    const pending = deferred<string>();
    const onData = vi.fn();
    const { unmount } = renderHook(() => useResource(() => pending.promise, [], { onData }));
    unmount();
    await act(async () => {
      pending.resolve("late");
      await pending.promise;
    });
    expect(onData).not.toHaveBeenCalled();
  });

  it("reload keeps the data on screen and onData can ask whether its load is still current", async () => {
    const seen: boolean[] = [];
    let n = 0;
    const { result } = renderHook(() =>
      useResource(() => Promise.resolve(++n), [], { onData: (_d, live) => seen.push(live()) }),
    );
    await waitFor(() => expect(result.current.data).toBe(1));
    act(() => result.current.reload());
    expect(result.current.data).toBe(1);
    await waitFor(() => expect(result.current.data).toBe(2));
    expect(seen).toEqual([true, true]);
  });

  it("with reset, clears the old answer while a different one loads", async () => {
    const second = deferred<string>();
    const { result, rerender } = renderHook(
      ({ id }) => useResource(() => (id === 1 ? Promise.resolve("first") : second.promise), [id], { reset: true }),
      { initialProps: { id: 1 } },
    );
    await waitFor(() => expect(result.current.data).toBe("first"));
    rerender({ id: 2 });
    expect(result.current.data).toBeNull();
    await act(async () => {
      second.resolve("second");
      await second.promise;
    });
    expect(result.current.data).toBe("second");
  });

  it("without reset, keeps the old answer on screen while a new one loads", async () => {
    const second = deferred<string>();
    const { result, rerender } = renderHook(({ id }) => useResource(() => (id === 1 ? Promise.resolve("first") : second.promise), [id]), {
      initialProps: { id: 1 },
    });
    await waitFor(() => expect(result.current.data).toBe("first"));
    rerender({ id: 2 });
    expect(result.current.data).toBe("first");
    await act(async () => {
      second.resolve("second");
      await second.promise;
    });
    expect(result.current.data).toBe("second");
  });

  it("holds back until enabled", async () => {
    const load = vi.fn(() => Promise.resolve("x"));
    const { result, rerender } = renderHook(({ on }) => useResource(load, [], { enabled: on }), { initialProps: { on: false } });
    expect(load).not.toHaveBeenCalled();
    rerender({ on: true });
    await waitFor(() => expect(result.current.data).toBe("x"));
    expect(load).toHaveBeenCalledTimes(1);
  });

  describe("listening", () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    it("loads again once per changed of its area, once per reconnect and once per resync, and creates no timer", async () => {
      const fake = fakeLiveHub();
      const load = vi.fn(() => Promise.resolve("x"));
      renderHook(() => useResource(load, [], { liveArea: "gaps" }), { wrapper: liveWrapper(fake.hub) });
      await flushHub();
      expect(load).toHaveBeenCalledTimes(1);
      expect(vi.getTimerCount()).toBe(0);
      act(() => fake.push("changed", { area: "gaps", ids: ["g1"] }));
      expect(load).toHaveBeenCalledTimes(2);
      act(() => fake.push("changed", { area: "jobs", ids: [] }));
      expect(load).toHaveBeenCalledTimes(2);
      act(() => fake.setState("open"));
      expect(load).toHaveBeenCalledTimes(3);
      act(() => fake.push("resync", {}));
      expect(load).toHaveBeenCalledTimes(4);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(120_000);
      });
      expect(load).toHaveBeenCalledTimes(4);
    });

    it("with liveId, ignores a changed that names other ids", async () => {
      const fake = fakeLiveHub();
      const load = vi.fn(() => Promise.resolve("x"));
      renderHook(() => useResource(load, [], { liveArea: "prep", liveId: "p1" }), { wrapper: liveWrapper(fake.hub) });
      await flushHub();
      act(() => fake.push("changed", { area: "prep", ids: ["p2"] }));
      expect(load).toHaveBeenCalledTimes(1);
      act(() => fake.push("changed", { area: "prep", ids: ["p1"] }));
      expect(load).toHaveBeenCalledTimes(2);
    });

    it("holds the listening back until enabled", async () => {
      const fake = fakeLiveHub();
      const load = vi.fn(() => Promise.resolve("x"));
      renderHook(() => useResource(load, [], { liveArea: "gaps", enabled: false }), { wrapper: liveWrapper(fake.hub) });
      await flushHub();
      expect(fake.streams).toHaveLength(0);
    });
  });
});
