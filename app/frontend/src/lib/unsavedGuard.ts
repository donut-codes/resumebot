import { useEffect } from "react";

// A screen with edits that are not saved yet tells the app so, and the app asks before they are lost: the browser asks
// before the page is closed or reloaded (beforeunload), and App.tsx asks before it moves to another address.

let unsaved = false;

export const LEAVE_QUESTION = "You have changes that aren't saved. Leave without saving them?";

/** Whether leaving the screen is fine: nothing is unsaved, or the person agrees to lose it. */
export function confirmLeave(): boolean {
  return !unsaved || window.confirm(LEAVE_QUESTION);
}

/** Makes the browser ask before the page is closed or reloaded, until the function it returns is called. Every call adds a
 * handler of its own: the browser counts a repeated add of one function as one, so a handler shared by two guards would be
 * taken from the one still holding when the other let go. */
function askBeforeUnload(): () => void {
  const warn = (e: BeforeUnloadEvent) => {
    e.preventDefault();
    e.returnValue = "";
  };
  window.addEventListener("beforeunload", warn);
  return () => window.removeEventListener("beforeunload", warn);
}

/** While `waiting`, the browser asks before the page is closed or reloaded, and nothing else asks. For an edit that only
 * the page holds but that is not lost by moving to another address, because something above the screens keeps sending it
 * (Search Settings' store: a save on its way, and the edits that wait for it). */
export function usePageLeaveGuard(waiting: boolean): void {
  useEffect(() => {
    if (!waiting) return undefined;
    return askBeforeUnload();
  }, [waiting]);
}

/** While `dirty`, the page asks before it is left. */
export function useUnsavedGuard(dirty: boolean): void {
  useEffect(() => {
    if (!dirty) return undefined;
    unsaved = true;
    const stopAsking = askBeforeUnload();
    return () => {
      unsaved = false;
      stopAsking();
    };
  }, [dirty]);
}
