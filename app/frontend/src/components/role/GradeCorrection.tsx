import { useState } from "react";
import type { RequirementRow } from "../../api/types/me";
import { useAction } from "../../lib/useAction";
import { SAID_TOAST_MS } from "../../lib/useDraftPage";
import ErrorBanner from "../primitives/ErrorBanner";
import { useToast } from "../toast/ToastProvider";

export const GRADES = { P: "Proven", T: "Transferable", U: "Unproven", N: "Not measurable" } as const;
/** Each class in the ink of its job: proven is green, transferable saffron (close, not there yet), unproven oxblood. */
export const GRADE_INK = { P: "green", T: "saffron", U: "oxblood", N: "graphite" } as const;

/** The owner's correction of one graded item: a class and a reason, sent through the one route (the score and the
 * expectation follow). Shared by the Your qualifications tab and the marks in the posting. */
/** Sends a correction through the one route: (item index, class, why). */
export type Correct = (index: number, cls: string, why: string) => Promise<{ said?: string }>;

export default function GradeCorrection({ correct, row, onDone, onCancel }: { correct: Correct; row: RequirementRow; onDone: () => void; onCancel: () => void }) {
  const { showToast } = useToast();
  const [cls, setCls] = useState<"P" | "T" | "U">(row.class === "N" ? "U" : row.class);
  const [why, setWhy] = useState("");
  const save = useAction(async () => {
    const corrected = await correct(row.index, cls, why);
    // The role's gates run again on the new class (docs/PRD.md 39.14): when that moved the role, the save says so.
    if (corrected.said) showToast(corrected.said, SAID_TOAST_MS);
    onDone();
  }, { fallback: "Couldn't change the class." });
  return (
    <>
      <form className="req-edit" onSubmit={(e) => { e.preventDefault(); save.run(); }}>
        <select className="fi-input" aria-label="Class" value={cls} onChange={(e) => setCls(e.target.value as "P" | "T" | "U")}>
          <option value="P">Proven</option>
          <option value="T">Transferable</option>
          <option value="U">Unproven</option>
        </select>
        <input type="text" className="fi-input" aria-label="Why" placeholder="Why" value={why} onChange={(e) => setWhy(e.target.value)} />
        <button type="submit" className="fi-btn" disabled={save.busy}>Save</button>
        <button type="button" className="fi-btn fi-btn-quiet" onClick={onCancel}>Cancel</button>
      </form>
      {save.error && <ErrorBanner as="p">{save.error}</ErrorBanner>}
    </>
  );
}
