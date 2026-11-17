import type { RequirementsGrade } from "../../api/types/profile";
import { InlineNumber } from "./EditParts";
import { GRADE_WORDS } from "./profileSpec";

/** "Let the app grade the requirements", in Qualifications on All's tab (the prioritization groups spec, rule 13):
 * a checkbox, unticked until the owner ticks it, and beside it the five numbers each rated requirement earns, as boxes inside
 * its sentences the way the estimated ask's steps have them. The analyst rates each required and each preferred requirement of
 * a posting Proven, Transferable or Unproven; with the box ticked the app gives each the points set here and never asks
 * Claude. The numbers are whole, of either sign (green for a plus, oxblood for a minus, as the ask rows' points are), and are
 * kept ticked or not. It is a setting, not a row: it has no Remove. Nothing here saves: the page has one Save. */
export default function GradeRequirements({ grade, onChange }: { grade: RequirementsGrade; onChange: (next: RequirementsGrade) => void }) {
  const number = (key: Exclude<keyof RequirementsGrade, "on">, label: string) => (
    <span className="fi-ss-numbox">
      <InlineNumber signed value={grade[key]} label={label} onChange={(next) => next !== null && onChange({ ...grade, [key]: Math.trunc(next) })} />
    </span>
  );
  return (
    <div className="fi-ss-grade">
      <label className="fi-ss-tick">
        <input type="checkbox" checked={grade.on} onChange={(e) => onChange({ ...grade, on: e.target.checked })} />
        <span>{GRADE_WORDS.check}</span>
      </label>
      <p className="fi-ss-grade-line">
        Required: each proven {number("required_proven", "Points for each proven required item")}, each transferable{" "}
        {number("required_transferable", "Points for each transferable required item")}, each unproven{" "}
        {number("required_unproven", "Points for each unproven required item")}.
      </p>
      <p className="fi-ss-grade-line">
        Preferred: each proven or transferable {number("preferred_proven_or_transferable", "Points for each proven or transferable preferred item")}, each
        unproven {number("preferred_unproven", "Points for each unproven preferred item")}.
      </p>
    </div>
  );
}
