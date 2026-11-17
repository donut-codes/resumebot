import { useState } from "react";
import GrowingText from "./GrowingText";

/** Separated by semicolons: for items that may hold commas themselves (job titles such as "Director, Example
 * Programs"). */
function toItems(text: string): string[] {
  return text
    .split(";")
    .map((w) => w.trim())
    .filter(Boolean);
}

/** A list on one line, items separated by semicolons, in a box that wraps and grows with it. */
export default function SemicolonInput({ id, items, onChange, label, describedBy, minRows, className = "fi-input" }: {
  id?: string; items: string[]; onChange: (items: string[]) => void; label?: string;
  /** The ids of the lines that describe the field (its hint), for a screen reader. */
  describedBy?: string;
  minRows?: number;
  /** The look of the box: a text field's, by default. */
  className?: string;
}) {
  // Kept as typed, so a trailing "; " isn't swallowed while writing the next item.
  const [text, setText] = useState(items.join("; "));
  // Items changed from outside (a posting read into them) show at once, unless the typed text already says them.
  const [seen, setSeen] = useState(items);
  if (items !== seen) {
    setSeen(items);
    const typed = toItems(text);
    if (typed.length !== items.length || typed.some((w, i) => w !== items[i])) setText(items.join("; "));
  }
  return (
    <GrowingText id={id} className={className} aria-label={label} aria-describedby={describedBy} minRows={minRows} value={text} oneLine
      onText={(t) => {
        setText(t);
        onChange(toItems(t));
      }} />
  );
}
