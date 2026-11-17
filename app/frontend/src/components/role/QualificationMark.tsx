import { useId, useState } from "react";
import type { RequirementRow } from "../../api/types/me";
import GradeCorrection, { GRADES, GRADE_INK, type Correct } from "./GradeCorrection";

/** The mark of each class: a text sign, so it reads without colour. */
export const MARK_SIGN = { P: "✓", T: "~", U: "✗", N: "–" } as const;

/** One posting line with the mark of its graded item at its start, in place of its bullet. The mark opens, in place, the
 * analyst's reason and the master lines behind it; a required item's class can be changed there with a reason (the
 * route corrects required items only). */
export function MarkedLine({ text, row, correct, editable }: {
  text: string;
  row: RequirementRow;
  correct: Correct;
  editable: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const id = useId();
  const measurable = row.class !== "N"; // N: a resume can't show it; its reason is the box's tooltip (docs/PRD.md 26.12)
  editable = editable && measurable;
  const detail = open && (
    <div className="qmark-detail" id={id} role="group" aria-label={`${GRADES[row.class]}: why`}>
      <p className="fi-note"><strong>{GRADES[row.class]}</strong>{row.by === "owner" && " (set by you)"}</p>
      {row.why && <p className="fi-note">{row.why}</p>}
      {row.from.map((f) => <p key={f.id} className="fi-note req-evidence-line">&ldquo;{f.text}&rdquo;</p>)}
      {row.from.length === 0 && row.class !== "U" && measurable && <p className="fi-note">Not linked to a resume line yet.</p>}
      {row.owner_why && <p className="fi-note">Your reason: {row.owner_why}</p>}
      {editable && (editing ? (
        <GradeCorrection correct={correct} row={row} onCancel={() => setEditing(false)}
          onDone={() => { setEditing(false); setOpen(false); }} />
      ) : (
        <button type="button" className="fi-link" onClick={() => setEditing(true)}>Change the grade</button>
      ))}
    </div>
  );
  const mark = (
    <button type="button" className="qmark" data-ink={GRADE_INK[row.class]} aria-expanded={open} aria-controls={open ? id : undefined}
      title={measurable ? undefined : row.why || undefined}
      onClick={() => { setOpen((v) => !v); setEditing(false); }}>
      <span aria-hidden="true">{MARK_SIGN[row.class]}</span>
      <span className="fi-vh">{`${GRADES[row.class]}: show why`}</span>
    </button>
  );
  return <><div className="qmark-line">{mark} {text}</div>{detail}</>;
}

/** The marker of a requirement line that was not graded: a neutral grey box in the mark's own slot, not clickable. */
export function NotCompared() {
  return (
    <span className="qmark qmark-none" title="Not compared: this line was not graded">
      <span className="fi-vh">Not compared</span>
    </span>
  );
}
