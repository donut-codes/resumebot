import { useId, type ReactNode } from "react";
import GrowingText from "../primitives/GrowingText";
import SemicolonInput from "../primitives/SemicolonInput";
import { FACTS, type Facts } from "./meParts";

/** One row of the facts list: its name (and the one plain line after it) at the left, its value at the right. */
function Fact({ label, hint, children }: { label: string; hint?: string; children: (id: string, describedBy: string | undefined) => ReactNode }) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  return (
    <div className="fi-me-fact">
      <div className="fi-me-fact-name">
        <label htmlFor={id}>{label}</label>
        {hint && (
          <p id={hintId} className="fi-me-field-hint">
            {hint}
          </p>
        )}
      </div>
      <div className="fi-me-fact-value">{children(id, hintId)}</div>
    </div>
  );
}

/** How many of the facts are filled in, for the part's count. */
export const filledFacts = (a: Facts) => FACTS.filter((f) => (f.key === "links" ? a.links.length > 0 : (a[f.key] ?? "").trim() !== "")).length;

/** The facts forms ask for, as a two-column list divided by hairlines: each fact is edited where it stands. */
export default function FactsList({ facts, onChange }: { facts: Facts; onChange: (next: Facts) => void }) {
  return (
    <div className="fi-me-facts">
      {FACTS.map((f) => (
        <Fact key={f.key} label={f.label} hint={f.hint}>
          {(id, describedBy) =>
            f.key === "links" ? (
              <SemicolonInput id={id} items={facts.links} describedBy={describedBy} onChange={(links) => onChange({ ...facts, links })} />
            ) : (
              <GrowingText id={id} className="fi-input" aria-describedby={describedBy} oneLine value={facts[f.key] ?? ""} onText={(v) => onChange({ ...facts, [f.key]: v })} />
            )
          }
        </Fact>
      ))}
    </div>
  );
}
