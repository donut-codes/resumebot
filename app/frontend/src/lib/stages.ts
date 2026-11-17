// The statuses a role moves through: the one place the screens learn a status's
// key and name. Screens read a status's label (`stageFor`) and the board's stage keys (`STAGES`, lib/route.ts).
//
// The keys are the backend's (app/jobsearch/stages.py): a role's status, and the board's columns. Three statuses that end
// a role (rejected, gone quiet, withdrew) are shown as one stage, Ended (`OF_STATUS`).

export type StageKey =
  | "added_links"
  | "followup_needed"
  | "landing_zone"
  | "cleansed_data"
  | "first_impression"
  | "second_impression"
  | "ready_to_launch"
  | "applied"
  | "active_discussion"
  | "closed"
  | "ended"
  | "gated_out";

export interface Stage {
  key: StageKey;
  label: string;
}

export const STAGES: Stage[] = [
  { key: "added_links", label: "Added links" },
  { key: "followup_needed", label: "Followup needed" },
  { key: "landing_zone", label: "Landing Zone" },
  { key: "cleansed_data", label: "Cleansed Data" },
  { key: "first_impression", label: "First Impression" },
  { key: "second_impression", label: "Second Impression" },
  { key: "ready_to_launch", label: "Ready to Launch" },
  { key: "applied", label: "Applied" },
  { key: "active_discussion", label: "Active Discussion" },
  { key: "closed", label: "Closed" },
  { key: "gated_out", label: "Gated out" },
  { key: "ended", label: "Ended" },
];

/** The statuses that end a role are one stage, Ended; every other status is its own stage. */
const OF_STATUS: Record<string, StageKey> = { rejected: "ended", gone_quiet: "ended", withdrew: "ended" };

/** The stage a status or a board column key belongs to, or null for a word this page doesn't know. */
export function stageOf(word: string | null | undefined): StageKey | null {
  if (!word) return null;
  if (word in OF_STATUS) return OF_STATUS[word];
  return STAGES.some((s) => s.key === word) ? (word as StageKey) : null;
}

/** The stage a status or column key belongs to, whole (its key for the stage square, its label for the name), or null
 * for a word this page doesn't know. */
export function stageFor(word: string | null | undefined): Stage | null {
  const key = stageOf(word);
  return key ? (STAGES.find((s) => s.key === key) ?? null) : null;
}
