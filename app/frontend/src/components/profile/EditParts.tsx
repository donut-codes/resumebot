import { useId, useState } from "react";
import type { ReactNode } from "react";
import type { Ask } from "../../api/types/profile";
import { fractionOf, percentOf } from "../../lib/percent";
import { CLASS_WORDS, EMPLOYER_FACTS, FACT_OPS, factName } from "./profileSpec";

/** The small pieces Hard pass and Prioritization are written with: a field under its label and one plain hint, a tick, the
 * boxes for a number inside a sentence. Nothing here saves: a Draft (search/common.tsx) hands each change on. */

/** Compares two documents as they would be saved: keys in any order, a key left empty (null) the same as one never set. */
export function canon(value: unknown): string {
  const sort = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(sort);
    if (v && typeof v === "object") {
      return Object.fromEntries(
        Object.entries(v as Record<string, unknown>)
          .filter(([, x]) => x !== null && x !== undefined)
          .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
          .map(([k, x]) => [k, sort(x)]),
      );
    }
    return v;
  };
  return JSON.stringify(sort(value));
}

/** The id of a part's one plain line, so the part (and a field in it) can name it in `aria-describedby`. */
const partHintId = (id: string) => `${id}-hint`;

/** A labelled input with an optional hint; `children` gets the id to put on the input and the id of the hint to name in
 * its `aria-describedby`. */
export function EditField({
  label, hint, className = "", children,
}: {
  label: string;
  hint?: ReactNode;
  className?: string;
  children: (id: string, describedBy: string | undefined) => ReactNode;
}) {
  const id = useId();
  return (
    <div className={`fi-field ${className}`.trim()}>
      <label htmlFor={id}>{label}</label>
      {hint && (
        <p className="fi-field-hint" id={partHintId(id)}>
          {hint}
        </p>
      )}
      {children(id, hint ? partHintId(id) : undefined)}
    </div>
  );
}

/** A checkbox with its words. `label` is its accessible name; `text` what is seen beside it. */
export function Tick({ label, checked, onChange, text }: { label: string; checked: boolean; onChange: (on: boolean) => void; text: string }) {
  return (
    <label className="fi-ss-tick">
      <input type="checkbox" aria-label={label} checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span>{text}</span>
    </label>
  );
}

/** "The employer's [fact] [is not] [value]", on one line while there is room: an employer fact a rule or an ask is decided
 * from. A rule about the employer on Hard pass and an ask the app decides from an employer fact on Prioritization are both
 * drawn with it, so they read and edit alike. A change goes up as the one field that changed. */
export function FactFields({
  fact, op, value, onChange,
}: {
  fact: string | null | undefined;
  op: string | null | undefined;
  value: unknown;
  onChange: (change: { fact?: string; op?: string; value?: string }) => void;
}) {
  return (
    <div className="fi-ss-fact">
      <span>The employer&rsquo;s</span>
      <select aria-label="Employer fact" className="fi-input fi-ss-fact-pick" value={fact ?? ""} onChange={(e) => onChange({ fact: e.target.value })}>
        <option value="">(choose)</option>
        {EMPLOYER_FACTS.map(([v, l]) => (
          <option key={v} value={v}>{factName(l)}</option>
        ))}
      </select>
      <select aria-label="Must be" className="fi-input" value={op ?? "="} onChange={(e) => onChange({ op: e.target.value })}>
        {FACT_OPS.map(([v, l]) => (
          <option key={v} value={v}>{l}</option>
        ))}
      </select>
      <input type="text" className="fi-input" aria-label="This value" value={String(value ?? "")} onChange={(e) => onChange({ value: e.target.value })} />
    </div>
  );
}

/** A number in the middle of a line of words (an amount, a number of days, a limit): kept as typed while editing, and
 * only a number is passed on, so a half-typed or emptied box never makes the value nothing. With `allowEmpty`, an emptied
 * box is no value (null). Left, it shows the value that stands. With `percent` the value is a share kept as a fraction
 * (0.85) and shown as a percent (85); what is typed goes back by the decimal point (lib/percent.ts), so the value kept is
 * exactly the one the digits say. `min` and `max` are in what is shown; `invalid` marks a value outside them. `onLeave`
 * hears the box lose focus, after it shows the value that stands. */
export function InlineNumber({
  value, label, onChange, allowEmpty = false, percent = false, step, min, max, invalid = false, signed = false, onLeave,
}: {
  value: number | null | undefined;
  label: string;
  onChange: (next: number | null) => void;
  allowEmpty?: boolean;
  percent?: boolean;
  step?: number | "any";
  min?: number;
  max?: number;
  invalid?: boolean;
  /** The number is points: it takes the ink of its sign, green for plus and oxblood for minus, as the ask rows' points do. */
  signed?: boolean;
  onLeave?: () => void;
}) {
  const shown = (n: number | null | undefined): string => (n === null || n === undefined ? "" : String(percent ? percentOf(n) : n));
  const kept = (typed: string): number | null => (typed === "" ? null : percent ? fractionOf(typed) : Number(typed));
  const [text, setText] = useState(shown(value));
  // Changed from outside (put back by Undo): show it, unless what is typed already says it.
  const [seen, setSeen] = useState(value);
  if (value !== seen) {
    setSeen(value);
    if (kept(text) !== (value ?? null)) setText(shown(value));
  }
  return (
    <input
      type="number"
      step={step ?? (percent ? "any" : 1)}
      min={min}
      max={max}
      className="fi-input fi-ss-num"
      aria-label={label}
      aria-invalid={invalid || undefined}
      data-sign={signed ? ((value ?? 0) > 0 ? "plus" : (value ?? 0) < 0 ? "minus" : "none") : undefined}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        const next = kept(e.target.value);
        if (next === null) {
          if (allowEmpty) onChange(null);
          return;
        }
        if (Number.isFinite(next)) onChange(next);
      }}
      onBlur={() => {
        setText(shown(value));
        onLeave?.();
      }}
    />
  );
}

/** Whole points, kept as typed while editing; an empty box is 0, and a minus is 0 too: Prioritization only adds points
 * (docs/PRD.md 30.10). A line saved with minus points shows what is stored until it is changed. An ask's, a level's and an
 * any-of box's points are all written with it, in the ruled column of points. */
export function PointsInput({ value, label, onChange }: { value: number; label: string; onChange: (points: number) => void }) {
  const [text, setText] = useState(String(value));
  // Changed from outside (put back by Undo): show it, unless what is typed already says it.
  const [seen, setSeen] = useState(value);
  if (value !== seen) {
    setSeen(value);
    if (Number(text) !== value) setText(String(value));
  }
  return (
    <input
      type="number"
      step={1}
      min={0}
      inputMode="numeric"
      className="fi-input fi-ss-pts"
      data-sign={value > 0 ? "plus" : value < 0 ? "minus" : "none"}
      aria-label={label}
      value={text}
      onChange={(e) => {
        const n = Math.trunc(Number(e.target.value));
        const points = e.target.value !== "" && Number.isFinite(n) ? Math.max(0, n) : 0;
        setText(points === 0 && n < 0 ? "0" : e.target.value);
        onChange(points);
      }}
    />
  );
}

/** What the app decides an ask (or a level) from, as the one value that decides it, inline in its own line (as a rule about the
 * employer is on Hard pass): an employer fact is three fields, a pay tier the amount, the posting's age the days, and an
 * ask counted for each item found the limit. An ask decided from the analyst's requirement classes says its meaning in
 * plain words. Nothing else of an ask is hidden: there is no Details. */
export function AskFacts({ ask, name, onChange }: { ask: Ask; name: string; onChange: (next: Ask) => void }) {
  const lines: ReactNode[] = [];
  if (ask.fact !== null && ask.fact !== undefined) {
    lines.push(<FactFields key="fact" fact={ask.fact} op={ask.op} value={ask.value} onChange={(change) => onChange({ ...ask, ...change })} />);
  }
  // The direction of a pay tier or a posting's age is the one stored (`above`), said in words: it is not a choice on the line.
  const down = ask.above === false || String(ask.above) === "false";
  if (ask.amount !== null && ask.amount !== undefined) {
    lines.push(
      <p key="amount" className="fi-ss-sentence">
        The likely ask is {down ? "under" : "over"}{" "}
        <InlineNumber value={ask.amount} label={`Pay a year for ${name}`} onChange={(amount) => amount !== null && onChange({ ...ask, amount })} /> a year
      </p>,
    );
  }
  if (ask.days !== null && ask.days !== undefined) {
    // Older than the days, or (stored with `above` false) posted within the last of them: under days x 24 hours old.
    lines.push(
      <p key="days" className="fi-ss-sentence">
        {down ? "Posted within the last" : "The posting is older than"}{" "}
        <InlineNumber value={ask.days} label={`Days for ${name}`} onChange={(days) => days !== null && onChange({ ...ask, days })} />{" "}
        {down && ask.days === 1 ? "day" : "days"}
      </p>,
    );
  }
  if (ask.from_classes) {
    lines.push(
      <p key="classes" className="fi-ss-sentence">
        {CLASS_WORDS[ask.from_classes] ?? ask.from_classes}
      </p>,
    );
  }
  if (ask.per_item) {
    lines.push(
      <p key="each" className="fi-ss-sentence">
        For each item found, {Number(ask.points) < 0 ? "down" : "up"} to{" "}
        <InlineNumber value={ask.limit} label={`Limit for ${name}`} allowEmpty onChange={(limit) => onChange({ ...ask, limit })} />
      </p>,
    );
  }
  return lines.length > 0 ? <div className="fi-ss-facts">{lines}</div> : null;
}
