import { useCallback, useEffect, useRef, type RefObject } from "react";
import { useUnsavedGuard } from "./unsavedGuard";

/** How long "Saved" stays on the screen. */
export const SAVED_TOAST_MS = 2000;
/** How long the line a save says of what it changed stays on the screen ("Re-checked 120 postings and 30 roles: ..."):
 * longer than "Saved", since it is a sentence to read (docs/PRD.md 39.23). */
export const SAID_TOAST_MS = 8000;

interface Options {
  /** Whether anything on the page is unsaved. */
  dirty: boolean;
  busy: boolean;
  /** Saves the page (the bar's Save, and Ctrl or Cmd with S). */
  save: () => void;
  /** Gives keyboard focus a place to go when the bar it was in goes (the changes were saved or put back). */
  settleFocus: () => void;
}

/** What a page of edits does around its Save bar (What to look for, Hard pass, Prioritization and My Resume's pages): the page
 * asks before it is left while something is unsaved; the bar's height is kept in reserve (`html.has-save-bar` and
 * `--save-bar-height`, focus/me.css) so a field that takes focus is scrolled clear of it, and grows with an error line or on
 * a narrow screen; and Ctrl or Cmd with S saves, keeping the browser from offering to save the page.
 *
 * `barRef` goes on the bar. `keepFocus()` is asked before a save or an Undo: it notes whether focus is in the bar, and the
 * function it returns hands that focus to `settleFocus` once the change is made. What the page's draft is, how it is
 * merged with what the server says, and what is sent stay the page's own. */
export function useDraftPage({ dirty, busy, save, settleFocus }: Options) {
  useUnsavedGuard(dirty);
  const barRef: RefObject<HTMLFormElement | null> = useRef(null);

  useEffect(() => {
    const bar = barRef.current;
    if (!dirty || !bar) return undefined;
    const page = document.documentElement;
    const reserve = () => page.style.setProperty("--save-bar-height", `${bar.offsetHeight}px`);
    page.classList.add("has-save-bar");
    reserve();
    const watch = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(reserve);
    watch?.observe(bar);
    return () => {
      watch?.disconnect();
      page.classList.remove("has-save-bar");
      page.style.removeProperty("--save-bar-height");
    };
  }, [dirty]);

  const saveRef = useRef(save);
  saveRef.current = save;
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "s") {
        e.preventDefault();
        if (dirty && !busy) saveRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [dirty, busy]);

  const settleRef = useRef(settleFocus);
  settleRef.current = settleFocus;
  const keepFocus = useCallback(() => {
    const had = Boolean(barRef.current?.contains(document.activeElement));
    return () => {
      if (had) settleRef.current();
    };
  }, []);

  return { barRef, keepFocus };
}
