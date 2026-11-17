import { useId, type ReactNode } from "react";
import GrowingText from "../primitives/GrowingText";
import SemicolonInput from "./SemicolonInput";

interface FieldProps {
  label: string;
  /** The one plain line under the label, tied to the field with `aria-describedby`. */
  hint?: string;
  /** The label is read but not drawn (a line in a list, whose heading is the list's). */
  hideLabel?: boolean;
  /** The label is a column's name: drawn once above the list on a wide screen (ItemList `heads`), here on a narrow one. */
  column?: boolean;
  className?: string;
  children: (props: { id: string; describedBy: string | undefined }) => ReactNode;
}

/** A field with its label above it and its hint under that: 14px 600 `soft`, then 14px `muted`. */
export function Field({ label, hint, hideLabel = false, column = false, className = "", children }: FieldProps) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const labelClass = hideLabel ? "fi-vh" : column ? "fi-me-label fi-me-col" : "fi-me-label";
  return (
    <div className={`fi-me-field ${className}`.trim()}>
      <label htmlFor={id} className={labelClass}>
        {label}
      </label>
      {hint && (
        <p id={hintId} className="fi-me-field-hint">
          {hint}
        </p>
      )}
      {children({ id, describedBy: hintId })}
    </div>
  );
}

interface TextProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
  hint?: string;
  hideLabel?: boolean;
  column?: boolean;
  /** Shown in the box while it is empty (the label of a note, which is not drawn). */
  placeholder?: string;
  /** How many lines it starts at; it grows with its text. */
  rows?: number;
  /** One line of words: Enter adds no break (a name, a phone number). */
  oneLine?: boolean;
  className?: string;
}

/** Words in a box that grows with them, so what was written is read whole instead of behind a scrollbar. */
export function Text({ label, value, onChange, hint, hideLabel, column, placeholder, rows = 1, oneLine = false, className }: TextProps) {
  return (
    <Field label={label} hint={hint} hideLabel={hideLabel} column={column} className={className}>
      {({ id, describedBy }) => (
        <GrowingText id={id} className="fi-input" aria-describedby={describedBy} placeholder={placeholder} minRows={rows} oneLine={oneLine} value={value} onText={onChange} />
      )}
    </Field>
  );
}

interface TermsProps {
  label: string;
  items: string[];
  onChange: (items: string[]) => void;
  hint?: string;
  hideLabel?: boolean;
  column?: boolean;
  className?: string;
}

/** Terms on one line, separated by semicolons (a term may hold a comma). */
export function Terms({ label, items, onChange, hint, hideLabel, column, className }: TermsProps) {
  return (
    <Field label={label} hint={hint} hideLabel={hideLabel} column={column} className={className}>
      {({ id, describedBy }) => <SemicolonInput id={id} items={items} onChange={onChange} describedBy={describedBy} />}
    </Field>
  );
}

/** A note that is not for printing: its label is read, and the box says it while it is empty, so a list of notes does
 * not repeat the label over every box. */
export function Note({ value, onChange, label = "Note, never printed" }: { value: string; onChange: (value: string) => void; label?: string }) {
  return <Text label={label} hideLabel placeholder={label} className="fi-me-note" value={value} onChange={onChange} />;
}

/** A yes or no beside its words. As a column of a list (`column`), the words are the column's header on a wide screen. */
export function Check({ label, checked, onChange, column = false }: { label: string; checked: boolean; onChange: (checked: boolean) => void; column?: boolean }) {
  return (
    <label className="fi-me-check">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className={column ? "fi-me-col" : undefined}>{label}</span>
    </label>
  );
}
