import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { confirmLeave, usePageLeaveGuard, useUnsavedGuard } from "./unsavedGuard";

// The page-leave guard asks only the browser, only while something is waiting, and never makes the app's own moves between
// addresses ask (`confirmLeave`): the edit it guards is kept by a store above the screens.

const leaving = () => {
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
};

describe("the page-leave guard", () => {
  it("asks before the page is closed or reloaded while something waits, and stops when it is done", () => {
    const { rerender, unmount } = renderHook(({ waiting }) => usePageLeaveGuard(waiting), { initialProps: { waiting: false } });
    expect(leaving()).toBe(false);
    rerender({ waiting: true });
    expect(leaving()).toBe(true);
    rerender({ waiting: false });
    expect(leaving()).toBe(false);
    rerender({ waiting: true });
    unmount();
    expect(leaving()).toBe(false);
  });

  it("does not make a move to another address ask", () => {
    renderHook(() => usePageLeaveGuard(true));
    expect(confirmLeave()).toBe(true);
  });
});

// Both guards ask the browser through the same event. Found by the review of the page-leave guard: they shared one listener,
// the browser counts a repeated add of one function as one, and either's cleanup took it away from the other.
describe("the two guards together", () => {
  it("keeps asking for an unsaved draft after the page-leave guard lets go", () => {
    renderHook(() => useUnsavedGuard(true));
    const { rerender } = renderHook(({ waiting }) => usePageLeaveGuard(waiting), { initialProps: { waiting: true } });
    expect(leaving()).toBe(true);
    rerender({ waiting: false });
    expect(leaving()).toBe(true);
  });

  it("keeps asking for a waiting Search Settings edit after the draft is saved", () => {
    const { rerender } = renderHook(({ dirty }) => useUnsavedGuard(dirty), { initialProps: { dirty: true } });
    renderHook(() => usePageLeaveGuard(true));
    expect(leaving()).toBe(true);
    rerender({ dirty: false });
    expect(leaving()).toBe(true);
  });
});
