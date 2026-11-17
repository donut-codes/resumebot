import { useSyncExternalStore } from "react";

/** Whether a media query matches now; the component redraws when that changes. */
export function useMedia(query: string): boolean {
  return useSyncExternalStore(
    (on) => {
      if (typeof window.matchMedia !== "function") return () => {};
      const m = window.matchMedia(query);
      m.addEventListener("change", on);
      return () => m.removeEventListener("change", on);
    },
    () => typeof window.matchMedia === "function" && window.matchMedia(query).matches,
  );
}

/** Below this the open item is a page of its own over the list, not a pane beside it. */
export const NARROW = "(max-width: 1100px)";
