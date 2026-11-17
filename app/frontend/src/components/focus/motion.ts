// The Focus Inbox's motion: rows glide to their new places (first, last, invert, play), a row that leaves folds out,
// and nothing moves for a person who asked for less motion.

import { useLayoutEffect, useRef, useState, type RefObject } from "react";

export function prefersLessMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return true;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

const GLIDE = { duration: 360, easing: "cubic-bezier(.2,.9,.25,1)" };

/** After each draw, every `[data-flip]` child that moved glides from where it was; a new one fades in. A new `resetKey`
 * (another stage, another search) draws without motion. */
export function useFlip(container: RefObject<HTMLElement | null>, resetKey: string) {
  const tops = useRef(new Map<string, number>());
  const lastKey = useRef(resetKey);
  useLayoutEffect(() => {
    const el = container.current;
    if (!el) return;
    const fresh = lastKey.current !== resetKey;
    lastKey.current = resetKey;
    const still = prefersLessMotion();
    const next = new Map<string, number>();
    el.querySelectorAll<HTMLElement>("[data-flip]").forEach((n) => {
      const id = n.dataset.flip as string;
      const top = n.offsetTop;
      next.set(id, top);
      if (fresh || still || typeof n.animate !== "function") return;
      const was = tops.current.get(id);
      if (was === undefined) {
        if (tops.current.size) n.animate([{ opacity: 0, transform: "translateY(-8px)" }, { opacity: 1, transform: "none" }], { duration: 260, easing: "ease-out" });
        return;
      }
      const dy = was - top;
      if (dy) n.animate([{ transform: `translateY(${dy}px)` }, { transform: "none" }], GLIDE);
    });
    tops.current = next;
  });
}

/** The items to draw: the current ones, and for a moment the ones that just left (marked `leaving`), where they were,
 * so they can fold out. */
export function useLeaving<T extends { id: string }>(items: T[], resetKey: string, ms = 420): { item: T; leaving: boolean }[] {
  const prev = useRef({ key: resetKey, items });
  const [gone, setGone] = useState<{ item: T; index: number }[]>([]);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  useLayoutEffect(() => {
    const before = prev.current;
    prev.current = { key: resetKey, items };
    if (before.key !== resetKey) {
      setGone([]);
      return;
    }
    if (prefersLessMotion()) return;
    const ids = new Set(items.map((i) => i.id));
    const left = before.items.map((item, index) => ({ item, index })).filter((x) => !ids.has(x.item.id));
    if (!left.length) return;
    setGone((g) => [...g.filter((x) => !left.some((l) => l.item.id === x.item.id)), ...left]);
    timers.current.push(setTimeout(() => setGone((g) => g.filter((x) => !left.some((l) => l.item.id === x.item.id))), ms));
  }, [items, resetKey, ms]);
  useLayoutEffect(() => () => timers.current.forEach(clearTimeout), []);

  const out = items.map((item) => ({ item, leaving: false }));
  const ids = new Set(items.map((i) => i.id));
  for (const g of [...gone].sort((a, b) => a.index - b.index)) {
    if (ids.has(g.item.id)) continue;
    out.splice(Math.min(g.index, out.length), 0, { item: g.item, leaving: true });
  }
  return out;
}
