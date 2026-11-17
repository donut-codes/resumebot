import { useId } from "react";
import { plural } from "../../lib/text";
import { useModalDialog } from "../../lib/useModalDialog";
import type { Fate, ImportPlan } from "./importMerge";

interface Props {
  plan: ImportPlan;
  /** The owner confirmed: put the additions into the page, as unsaved changes. */
  onConfirm: () => void;
  onClose: () => void;
}

const FATE: Record<Fate, string> = { new: "New", similar: "Similar", duplicate: "Already there" };

/** The review before an import goes into the master resume: per part and per role, what will be added, what sits beside
 * a line it resembles, and what is skipped as already there. Nothing is removed or reworded, and nothing reaches the page
 * until the owner presses Add; even then the page still has to be saved. */
export default function ImportReview({ plan, onConfirm, onClose }: Props) {
  const headingId = useId();
  const dialog = useModalDialog(onClose);
  const { counts } = plan;
  const adding = counts.new + counts.similar;
  return (
    <dialog {...dialog} className="fi-dialog" aria-labelledby={headingId}>
      <h2 id={headingId} className="fi-dialog-title">Add from your resume</h2>
      <p className="fi-dialog-hint">
        {adding === 0
          ? "Everything this resume holds is already in your master resume. Nothing will be added."
          : `${plural(counts.new, "new line")}, ${counts.similar} similar to a line you have (placed beside it), ${counts.duplicate} already there (skipped). Nothing you wrote is removed or reworded.`}
      </p>
      <div className="fi-me-review-groups">
        {plan.groups.map((g) => (
          <section key={g.key} className="fi-me-review-group" aria-label={g.title}>
            <h3 className="fi-me-review-title">{g.title}</h3>
            {g.where && <p className="fi-me-quiet">{g.where}</p>}
            {g.lines.length > 0 && (
              <ul className="fi-me-review-lines">
                {g.lines.map((l, i) => (
                  <li key={i} className="fi-me-review-line" data-fate={l.fate}>
                    <span className="fi-badge fi-me-tag" data-fate={l.fate}>{FATE[l.fate]}</span>
                    <span className="fi-me-review-text">
                      {l.label && <span className="fi-me-review-label">{l.label}: </span>}
                      {l.text}
                      {l.fate === "similar" && l.like && <span className="fi-me-review-like">Beside: {l.like}</span>}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {g.notes.map((n) => (
              <p key={n} className="fi-me-review-note">{n}</p>
            ))}
          </section>
        ))}
      </div>
      <p className="fi-dialog-hint">Adding puts these into the page as unsaved changes: look them over, then Save.</p>
      <div className="fi-dialog-actions">
        <button type="button" className="fi-btn fi-btn-go" disabled={adding === 0} onClick={onConfirm}>
          Add to my resume
        </button>
        <button type="button" className="fi-btn" onClick={onClose}>
          Cancel
        </button>
      </div>
    </dialog>
  );
}
