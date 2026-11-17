// Find Gaps (My Resume, docs/PRD.md path 38) as plain functions: the words of a grade, the run's line, and the new
// master resume that Add to resume saves. The components hold the state; this holds the rules.

import type { GapCard, GapGrade, GapProposal, GapRunView, Resume } from "../../api/types/me";
import { shortDateInWords } from "../../lib/dates";
import { newId } from "../../lib/ids";
import { plural } from "../../lib/text";

/** The analyst's classes in the order the bar and its legend show them, with the words the analyst uses. */
export const GRADES: { grade: GapGrade; words: string }[] = [
  { grade: "P", words: "Proven" },
  { grade: "T", words: "Transferable" },
  { grade: "U", words: "Unproven" },
];

/** What kind of ask a theme is, in words. */
export const KIND_WORDS = { required: "Required", preferred: "Preferred", responsibility: "Responsibility" } as const;

/** The why that goes in the resume's history when a line is added from here (docs/PRD.md 38.5). */
export const ADDED_WHY = "Added from Find Gaps";

/** The words of a grade bar for a screen reader: "60% Proven, 25% Transferable, 15% Unproven". */
export function barWords(percent: Record<GapGrade, number>): string {
  return GRADES.map((g) => `${percent[g.grade]}% ${g.words}`).join(", ");
}

/** The employers a run's postings come from, each once. */
export function employersIn(postings: { employer: string }[]): number {
  return new Set(postings.map((p) => p.employer.trim().toLowerCase())).size;
}

/** "10 postings from 10 employers · checked Oct 10 · instructions version 2": what a finished run read, and when. */
export function runLine(run: GapRunView): string {
  const read = `${plural(run.postings.length, "posting")} from ${plural(employersIn(run.postings), "employer")}`;
  const when = run.checked_at ? `checked ${shortDateInWords(run.checked_at)}` : null;
  const used = run.instructions_version ? `instructions version ${run.instructions_version}` : null;
  return [read, when, used].filter(Boolean).join(" · ");
}

/** The job a line can be added to, as the master resume names it: "Senior Manager, Northwind Labs". */
export function jobName(role: { title: string; employer: string }): string {
  return [role.title.trim(), role.employer.trim()].filter(Boolean).join(", ") || "A job with no name";
}

/** Every id the resume holds, so a new line's id is not one that any line already has (an id is how a line is cited). */
function idsIn(resume: Resume): { id: string }[] {
  return [
    ...resume.summary_variants,
    ...resume.achievements,
    ...resume.projects,
    ...resume.awards,
    ...resume.competencies,
    ...resume.education,
    ...resume.roles.flatMap((r) => [r, ...r.bullets, ...r.evidence]),
  ];
}

/** The master resume with one entry added to a job: Evidence (never printed; titled with the theme) or a Bullet (printed).
 * Nothing else in it changes. Null when the job is not in the resume. */
export function addToResume(resume: Resume, jobId: string, as: "evidence" | "bullet", theme: string, text: string): Resume | null {
  if (!resume.roles.some((r) => r.id === jobId)) return null;
  const line = text.trim();
  return {
    ...resume,
    roles: resume.roles.map((r) => {
      if (r.id !== jobId) return r;
      if (as === "evidence") {
        return { ...r, evidence: [...r.evidence, { id: newId({ name: theme }, idsIn(resume)), title: theme.trim(), text: line }] };
      }
      return { ...r, bullets: [...r.bullets, { id: newId({ name: line }, idsIn(resume)), text: line, note: "", group: "" }] };
    }),
  };
}

/** The fewest characters an answer to one of the Coach's questions may have (the server's own limit, docs/PRD.md 38.14). */
export const MIN_ANSWER = 10;

/** The Coach's cards grouped by the theme they ask about, in the run's order. */
export function cardsByTheme(cards: GapCard[]): { theme: string; grade: GapCard["grade"]; cards: GapCard[] }[] {
  const groups: { theme: string; grade: GapCard["grade"]; cards: GapCard[] }[] = [];
  for (const card of cards) {
    const last = groups[groups.length - 1];
    if (last && last.theme === card.theme) last.cards.push(card);
    else groups.push({ theme: card.theme, grade: card.grade, cards: [card] });
  }
  return groups;
}

/** Where a proposed line goes, in the master's words: "A new line under Senior Manager, Northwind Labs" or "Evidence for
 * "Led the group" (Senior Manager, Northwind Labs)". */
export function placeWords(place: GapProposal["place"]): string {
  if (place.kind === "evidence" && place.line) return `Evidence for “${place.line}” (${place.job})`;
  if (place.kind === "evidence") return `Evidence under ${place.job}`;
  return `A new line under ${place.job}`;
}
