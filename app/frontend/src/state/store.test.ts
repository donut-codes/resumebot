import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { counted, createState, optimistic, useSelect } from "./store";

// The machinery every screen's store stands on: a state with readers, a counted start, an optimistic change that puts the
// old value back when the server refuses, and a hook that redraws only when its slice changes.

describe("createState", () => {
  it("changes part of the state, keeps the rest, and tells every reader", () => {
    const s = createState({ a: 1, b: "x" });
    const heard = vi.fn();
    const off = s.subscribe(heard);
    s.set({ a: 2 });
    s.set((now) => ({ b: `${now.b}y` }));
    expect(s.getState()).toEqual({ a: 2, b: "xy" });
    expect(heard).toHaveBeenCalledTimes(2);
    off();
    s.set({ a: 3 });
    expect(heard).toHaveBeenCalledTimes(2);
  });
});

describe("counted", () => {
  it("begins with the first user and ends with the last, once each, however often a user stops", () => {
    const end = vi.fn();
    const begin = vi.fn(() => end);
    const life = counted(begin);
    const stopA = life.start();
    const stopB = life.start();
    expect(begin).toHaveBeenCalledTimes(1);
    expect(life.users()).toBe(2);
    stopA();
    stopA();
    expect(end).not.toHaveBeenCalled();
    stopB();
    expect(end).toHaveBeenCalledTimes(1);
    expect(life.users()).toBe(0);
    life.start();
    expect(begin).toHaveBeenCalledTimes(2);
  });
});

describe("optimistic", () => {
  it("shows the change at once and takes the server's answer", async () => {
    const s = createState({ on: false });
    let resolve!: (v: { on: boolean }) => void;
    const done = optimistic(
      () => s.set({ on: true }),
      () => new Promise<{ on: boolean }>((r) => (resolve = r)),
      () => s.set({ on: false }),
      (answer) => s.set(answer),
    );
    expect(s.getState().on).toBe(true);
    resolve({ on: true });
    expect(await done).toBe(true);
  });

  it("puts the old value back when the server refuses, and says it did not go", async () => {
    const s = createState({ on: false, said: "" });
    const ok = await optimistic(
      () => s.set({ on: true }),
      () => Promise.reject(new Error("no")),
      (err) => s.set({ on: false, said: (err as Error).message }),
    );
    expect(ok).toBe(false);
    expect(s.getState()).toEqual({ on: false, said: "no" });
  });
});

describe("useSelect", () => {
  it("redraws only when its slice changes", () => {
    const s = createState({ a: 1, b: 1 });
    let draws = 0;
    const { result } = renderHook(() => {
      draws += 1;
      return useSelect(s, (x) => x.a);
    });
    expect(result.current).toBe(1);
    const before = draws;
    act(() => s.set({ b: 2 }));
    expect(draws).toBe(before);
    act(() => s.set({ a: 5 }));
    expect(result.current).toBe(5);
  });
});
