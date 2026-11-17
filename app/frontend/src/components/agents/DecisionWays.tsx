import { useState } from "react";
import type { DataModelField, HarnessStep } from "../../api/types/agents";
import { Inline } from "./StepSpec";

/** A decision's ways out, read-only: where each goes, its rule in plain words and in FEEL, and what no rule can
 * change. A ruled list, one way to a row. */
export function DecisionWays({ step, stepTitle }: { step: HarnessStep; stepTitle: (id: string) => string }) {
  const ways = step.ways ?? [];
  if (ways.length === 0) return null;
  return (
    <div className="how-ways" onClick={(e) => e.stopPropagation()}>
      <h4 className="how-part-title">Which way it goes</h4>
      <ul className="how-ways-list">
        {ways.map((w) => (
          <li key={w.flow}>
            <strong>{w.name || stepTitle(w.to)}</strong>
            <span className="fi-note"> &rarr; {w.to_title}</span>
            {w.meaning && (
              <p className="how-way-meaning">
                <Inline text={w.meaning} />
              </p>
            )}
            {w.default ? (
              <p className="fi-note">When no other way applies.</p>
            ) : (
              w.condition && (
                <p className="fi-note">
                  Rule: <code>{w.condition}</code>
                </p>
              )
            )}
            {w.firm_meaning && <p className="fi-note">Always: {w.firm_meaning}</p>}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** The engine's own facts, which a rule can see but nobody asks about. */
const ENGINE_FACTS = ["result", "error", "error.code", "error.message"];

/** The facts of a process's data model that are worth showing. */
export function visibleFacts(fields: DataModelField[]): DataModelField[] {
  return fields.filter((f) => !ENGINE_FACTS.includes(f.path));
}

/** What a condition can see: every field of the process's data model, with its type and what it is. */
export function DataModelBlock({ fields }: { fields: DataModelField[] }) {
  const [open, setOpen] = useState(false);
  const shown = visibleFacts(fields);
  if (shown.length === 0) return null;
  return (
    <div className="how-model">
      <div className="how-model-row">
        <button type="button" className="fi-btn how-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
          {open ? "Hide" : "Show"} what a rule can look at
        </button>
        <span className="fi-note"> &middot; {shown.length} facts</span>
      </div>
      {open && (
        <table className="how-fields">
          <thead>
            <tr>
              <th>Fact</th>
              <th>Kind</th>
              <th>What it is</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((f) => (
              <tr key={f.path}>
                <td>
                  <code>{f.path}</code>
                </td>
                <td>{f.enum.length ? f.enum.join(" / ") : f.type}</td>
                <td>{f.description}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
