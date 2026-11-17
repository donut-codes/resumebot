import { useMemo, useRef, useState } from "react";
import { useAction } from "../../lib/useAction";
import { SAVED_TOAST_MS, useDraftPage } from "../../lib/useDraftPage";
import { useToast } from "../toast/ToastProvider";
import { namesInWords } from "../../lib/text";
import type { PartDef } from "./meParts";

interface Options<T extends object, V> {
  /** The document as it is saved. */
  saved: T;
  /** The version (or versions: `V` is whatever the page needs) of the document as it is saved, as the page last loaded it. */
  version: V;
  parts: PartDef[];
  /** What is left out of a save (lines added and never written). */
  prune: (doc: T) => T;
  /** Sends the pruned document, with why, naming the version the draft was made from (`base`): the server refuses it, in
   * words, when a newer one was saved since. Resolves to the version now in force; rejects with words when it can't. */
  save: (doc: T, why: string, base: V) => Promise<V>;
  /** The page's frame hears what was saved, and the version now in force, to show them at once. */
  onSaved: (doc: T, version: V) => void;
  /** A draft the page opens with, when something was filled in for it before it opened (the contact details an imported
   * resume gave): unsaved, like any draft, and put back by Undo. */
  start?: T | null;
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** One Me page's edits, kept as a draft until its one Save (the save bar of What to look for): `doc` is what shows (the
 * draft, or the saved document while there is none), `edit` takes the whole next document, and `dirtyIds` names the
 * parts that differ from what is saved. Ctrl or Cmd with S saves; Undo puts every part back; the page asks before it is
 * left with changes. A save that is refused keeps the edits and says why in the bar. A draft remembers the version it was
 * started from, and every save names it, so a page left open while the document was saved elsewhere cannot write its
 * old copy over the newer one: the save is refused and the bar offers Reload. */
export function useMeDoc<T extends object, V = number>({ saved, version, parts, prune, save, onSaved, start = null }: Options<T, V>) {
  const { showToast } = useToast();
  const [draft, setDraft] = useState<T | null>(start);
  const [why, setWhy] = useState("");
  const doc = draft ?? saved;

  const dirtyIds = useMemo(() => {
    if (!draft) return [];
    const cleaned = prune(draft) as unknown as Record<string, unknown>;
    const base = saved as unknown as Record<string, unknown>;
    return parts.filter((p) => p.keys.some((k) => !same(cleaned[k], base[k]))).map((p) => p.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, saved]);
  const dirty = dirtyIds.length > 0;

  const sendRef = useRef({ draft, why, version });
  sendRef.current = { draft, why, version };
  // The version the draft was started from: what was in force when its first edit was made (not what the page has heard
  // since), so a document saved elsewhere meanwhile refuses the save instead of being written over.
  const originRef = useRef<V | null>(start ? version : null);

  const sendAction = useAction(
    async () => {
      const { draft: sent, why: reason } = sendRef.current;
      if (!sent) return;
      const clean = prune(sent);
      const now = await save(clean, reason.trim(), originRef.current ?? sendRef.current.version);
      onSaved(clean, now);
      // What was sent is saved; anything typed while it was on its way stays a draft, made from what was just saved.
      originRef.current = sendRef.current.draft === sent ? null : now;
      setDraft((d) => (d === sent ? null : d));
      setWhy("");
      showToast("Saved", SAVED_TOAST_MS);
    },
    { fallback: "Couldn't save that." },
  );

  const runRef = useRef<() => void>(() => {});
  /** The first part's heading takes focus that was in the bar, instead of it dropping to the page. */
  const settleFocus = () => document.querySelector<HTMLElement>(".me-part-main button[aria-expanded]")?.focus();
  const { barRef, keepFocus } = useDraftPage({ dirty, busy: sendAction.busy, save: () => runRef.current(), settleFocus });

  runRef.current = () => {
    const settle = keepFocus();
    void sendAction.run().then(settle);
  };

  const undo = () => {
    const settle = keepFocus();
    setDraft(null);
    originRef.current = null;
    setWhy("");
    sendAction.setError(null);
    settle();
  };

  const titles = parts.filter((p) => dirtyIds.includes(p.id)).map((p) => p.title);
  return {
    doc,
    edit: (next: T) => {
      if (originRef.current === null) originRef.current = version;
      setDraft(next);
    },
    dirty,
    dirtyIds,
    bar: {
      ref: barRef,
      what: namesInWords(titles),
      why,
      onWhy: setWhy,
      busy: sendAction.busy,
      error: sendAction.error,
      stale: sendAction.conflict,
      onUndo: undo,
      onSave: () => runRef.current(),
    },
  };
}
