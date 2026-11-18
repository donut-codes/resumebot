import { useId, useState } from "react";
import type { MissionNumber } from "../../api/types/agents";

/** What a setting is saved as. */
export type NumberValue = number | boolean | string | null;

/** A count limit's value for no limit (backend limits.py `NO_LIMIT`). */
export const NO_LIMIT = -1;

/** One line of a mission's numbers for its card: "A posting is stale after 30 days", "Days of mail to look at not set",
 * "Sites to learn a night: no limit". */
export function numbersWords(numbers: MissionNumber[]): string {
  return numbers
    .map((n) => {
      if (n.kind === "flag") return `${n.label}: ${n.value ? "yes" : "no"}`;
      if (n.value === null || n.value === "") return `${n.label} not set`;
      if (n.unlimited && n.value === NO_LIMIT) return `${n.label}: no limit`;
      return `${n.label} ${n.value}${n.unit}`;
    })
    .join(" · ");
}

/** What an input holds while it is edited: text for everything but a yes or no. */
function start(n: MissionNumber): string | boolean {
  if (n.kind === "flag") return Boolean(n.value);
  return n.value === null || n.value === undefined ? "" : String(n.value);
}

/** What the input holds, as the server takes it (a blank number is null: the server says if that is allowed). */
function sent(n: MissionNumber, v: string | boolean): NumberValue {
  if (typeof v === "boolean") return v;
  if (n.kind === "whole" || n.kind === "fraction") return v.trim() === "" ? null : Number(v);
  return v.trim();
}

function Field({ n, value, onChange }: { n: MissionNumber; value: string | boolean; onChange: (v: string | boolean) => void }) {
  const id = useId();
  if (n.kind === "flag") {
    return (
      <div className="mission-number">
        <label htmlFor={id} className="mission-tick">
          <input id={id} type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} /> {n.label}
        </label>
        <p className="fi-note">{n.help}</p>
      </div>
    );
  }
  const text = String(value);
  return (
    <div className="mission-number">
      <label htmlFor={id}>{n.label}</label>
      <span className="mission-number-input">
        {n.kind === "time" ? (
          <input id={id} className="fi-input" type="time" value={text} onChange={(e) => onChange(e.target.value)} />
        ) : n.kind === "zone" || n.kind === "words" ? (
          <input id={id} className="fi-input" type="text" value={text} onChange={(e) => onChange(e.target.value)} />
        ) : (
          <input
            id={id}
            className="fi-input"
            type="number"
            inputMode="decimal"
            min={n.unlimited ? NO_LIMIT : (n.low ?? undefined)}
            max={n.high ?? undefined}
            step={n.kind === "fraction" ? 0.05 : 1}
            value={text}
            onChange={(e) => onChange(e.target.value)}
          />
        )}
        {n.unit && <span className="fi-note">{n.unit}</span>}
      </span>
      <p className="fi-note">{n.help}</p>
    </div>
  );
}

function Form({ numbers, onSave, busy, saveLabel, name }: Props) {
  const [values, setValues] = useState<Record<string, string | boolean>>(() => Object.fromEntries(numbers.map((n) => [n.name, start(n)])));
  const [why, setWhy] = useState("");
  const whyId = useId();
  return (
    <form
      className="mission-numbers"
      aria-label={name}
      onSubmit={(e) => {
        e.preventDefault();
        onSave(Object.fromEntries(numbers.map((n) => [n.name, sent(n, values[n.name])])), why.trim());
      }}
    >
      {numbers.map((n) => (
        <Field key={n.name} n={n} value={values[n.name]} onChange={(v) => setValues({ ...values, [n.name]: v })} />
      ))}
      <div className="mission-number">
        <label htmlFor={whyId}>Why (kept in the history)</label>
        <input id={whyId} className="fi-input" type="text" value={why} onChange={(e) => setWhy(e.target.value)} />
      </div>
      <div>
        <button type="submit" className="fi-btn" disabled={busy}>{saveLabel}</button>
      </div>
    </form>
  );
}

interface Props {
  numbers: MissionNumber[];
  /** Everything in the form, as the server takes it, and why. */
  onSave: (values: Record<string, NumberValue>, why: string) => void;
  busy: boolean;
  saveLabel: string;
  /** The form's accessible name. */
  name: string;
}

/** The profile settings a mission runs with, drawn from what the server says each is (kind, limits, one plain line
 * on what it does) and saved together, with why. Adding a setting to a mission is a line in the backend's registry
 * (missions/numbers.py); nothing here changes. The form starts over when the saved values change. */
export default function NumbersForm(props: Props) {
  return <Form key={JSON.stringify(props.numbers.map((n) => [n.name, n.value]))} {...props} />;
}
