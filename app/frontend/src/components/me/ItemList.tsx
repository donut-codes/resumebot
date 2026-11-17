import { useEffect, useRef, useState, type ReactNode } from "react";
import { PlusIcon } from "../focus/icons";
import { freshId } from "./meParts";

/** A few words of a line, for naming its buttons ("Remove Led the risk working group…"). */
export function nameOf(words: string, fallback: string): string {
  const text = words.replace(/\s+/g, " ").trim();
  if (!text) return fallback;
  return text.length > 50 ? `${text.slice(0, 50).trimEnd()}…` : text;
}

interface Props<T extends { id: string }> {
  items: T[];
  onChange: (items: T[]) => void;
  /** What one is called: "bullet", "rule". The add button reads "Add a bullet". */
  noun: string;
  /** A new line's fields, empty. */
  blank: () => Omit<T, "id">;
  /** A few words naming one line, for its buttons. */
  describe: (item: T) => string;
  /** One line's fields. */
  render: (item: T, index: number, set: (patch: Partial<T>) => void) => ReactNode;
  /** The lines to draw; the others are kept, only not drawn (a search). */
  show?: (item: T) => boolean;
  /** The words of the add button, when "Add a <noun>" is not it. */
  addLabel?: string;
  /** An extra class on the list, for a layout of its own (a `fi-me-cols-*` that sets the columns of one line's fields, or
   * `fi-me-flush` for lines that are rows opening in place and pad themselves). */
  className?: string;
  /** The names of the columns, drawn once above the lines that are laid out in `className`'s columns. A narrow screen
   * stacks a line's fields and draws each one's own label instead. */
  heads?: string[];
}

/** A list of lines edited where they stand, divided by hairlines: each line's fields in a row, with Remove at its right
 * (always shown), and "Add a <noun>" under the list. A line added takes keyboard focus; one
 * removed leaves focus on the add button. An Undo in the save bar puts a removed line back. There is no Up or Down: the
 * order of the lines changes nothing that comes out (the writer picks and orders lines for each posting), so it is the
 * order they were added in. */
export default function ItemList<T extends { id: string }>({ items, onChange, noun, blank, describe, render, show, addLabel, className = "", heads }: Props<T>) {
  const listRef = useRef<HTMLUListElement>(null);
  const addRef = useRef<HTMLButtonElement>(null);
  const [fresh, setFresh] = useState<string | null>(null);

  // The line just added takes focus, on its first field.
  useEffect(() => {
    if (!fresh) return;
    const row = Array.from(listRef.current?.children ?? []).find((li) => (li as HTMLElement).dataset.item === fresh);
    row?.querySelector<HTMLElement>("textarea, input:not([type=checkbox]), select")?.focus();
    setFresh(null);
  }, [fresh]);

  const set = (i: number, patch: Partial<T>) => onChange(items.map((it, n) => (n === i ? { ...it, ...patch } : it)));
  const remove = (i: number) => {
    onChange(items.filter((_, n) => n !== i));
    addRef.current?.focus();
  };
  const add = () => {
    const item = { ...blank(), id: freshId() } as unknown as T;
    onChange([...items, item]);
    setFresh(item.id);
  };

  return (
    <div className={`fi-me-list ${className}`.trim()}>
      {items.length > 0 && heads && (
        <div className="fi-me-item fi-me-cols-head" aria-hidden="true">
          <div className="fi-me-item-body">
            {heads.map((h) => (
              <span key={h}>{h}</span>
            ))}
          </div>
          <div className="fi-me-item-tools" />
        </div>
      )}
      {items.length > 0 && (
        <ul ref={listRef} className="fi-me-items">
          {items.map((item, i) => {
            if (show && !show(item)) return null;
            const name = describe(item);
            return (
              <li key={item.id} className="fi-me-item" data-item={item.id}>
                <div className="fi-me-item-body">{render(item, i, (patch) => set(i, patch))}</div>
                <div className="fi-me-item-tools">
                  <button type="button" className="fi-link fi-me-remove" aria-label={`Remove ${name}`} onClick={() => remove(i)}>
                    Remove
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
      <div className="fi-me-list-foot">
        <button ref={addRef} type="button" className="fi-btn" onClick={add}>
          <PlusIcon className="fi-btn-icon" />
          {addLabel ?? `Add ${/^[aeiou]/i.test(noun) ? "an" : "a"} ${noun}`}
        </button>
      </div>
    </div>
  );
}
