import { useMemo } from "react";
import type { RequirementRow } from "../../api/types/me";
import { isHeadingLine, placeDuties, placeMarks, postingBlocks, requirementPartLines, type PostingLine } from "../../lib/qualificationMarks";
import type { Correct } from "../role/GradeCorrection";
import { MarkedLine, NotCompared } from "../role/QualificationMark";

/** The role's graded items, for the marks in the posting (docs/PRD.md 20.8): its qualifications and its duties. */
export interface Graded {
  correct: Correct;
  requirements: RequirementRow[];
  preferred: RequirementRow[];
  /** The posting's duties, graded the same way; read-only, as preferred is. */
  responsibilities?: RequirementRow[];
}

/** A posting's text as it was written: the same blocks and lines in the same order, except that a line matching a graded
 * item (a qualification or a duty) shows that item's mark in place of its bullet. Nothing is added or moved; a graded
 * item that matches no line is not shown here (docs/PRD.md 20.8). Without `graded` it is the plain text. `className`
 * adds to the wrapper so the Pipeline pane can give it its own look. */
export function MarkedPosting({ text, graded = null, className = "" }: { text: string; graded?: Graded | null; className?: string }) {
  const blocks = useMemo(() => postingBlocks(text), [text]);
  const requirements = graded?.requirements;
  const preferred = graded?.preferred;
  const responsibilities = graded?.responsibilities;
  const hasGrades = graded !== null;
  const flat = useMemo(() => blocks.flat(), [blocks]);
  const qualificationRows = useMemo(
    () => (hasGrades ? placeMarks(flat, requirements ?? [], preferred ?? []) : new Map<number, RequirementRow>()),
    [hasGrades, flat, requirements, preferred],
  );
  // The duties take the lines the qualifications left. A duty's line is no requirement part (below): the lines of the
  // duties that were not graded stay as they are.
  const lineRows = useMemo(
    () => new Map([...qualificationRows, ...(hasGrades ? placeDuties(flat, responsibilities ?? [], qualificationRows) : [])]),
    [hasGrades, flat, responsibilities, qualificationRows],
  );
  const inParts = useMemo(() => requirementPartLines(flat, qualificationRows), [flat, qualificationRows]);
  const starts = useMemo(() => blocks.map((_, b) => blocks.slice(0, b).reduce((n, l) => n + l.length, 0)), [blocks]);
  // A line in a requirement part that matched no graded item and is not a heading carries the neutral marker. A part is a
  // section the page recognises, or the list of bullets a strongly matched line sits in under any other heading.
  const uncompared = (l: PostingLine, at: number) => !lineRows.has(at) && inParts.has(at) && !isHeadingLine(l);
  return (
    <div className={`role-description ${className}`.trim()}>
      {blocks.map((lines, b) => {
        const first = starts[b];
        if (!graded || !lines.some((l, k) => lineRows.has(first + k) || uncompared(l, first + k))) return <p key={b}>{lines.map((l) => l.raw).join("\n")}</p>;
        return (
          <div key={b} className="posting-block">
            {lines.map((l, k) => {
              const row = lineRows.get(first + k);
              return row
                ? <MarkedLine key={k} text={l.shown} row={row} correct={graded.correct} editable={requirements?.includes(row) ?? false} />
                : uncompared(l, first + k)
                  ? <div key={k} className="qmark-line"><NotCompared /> {l.shown}</div>
                  : <div key={k} className="posting-line">{l.raw}</div>;
            })}
          </div>
        );
      })}
    </div>
  );
}
