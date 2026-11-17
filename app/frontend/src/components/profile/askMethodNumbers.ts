// The pay method's numbers as the page that edits them holds them: the coverage tiers read from and written back to the
// stored text, and the range each number may be in (the server's, said in what is shown). Nothing here works the ask out:
// the live example is the server's (comp.compute), never a copy of its math.
import type { CompExample, CompMethod } from "../../api/types/profile";
import { formatDollars } from "../../lib/format";
import { percentOf } from "../../lib/percent";

/** One coverage tier, as the stored fractions: from this much of the job shown (`least`), the ask starts this far up the
 * range (`ask`). */
export interface Tier {
  least: number;
  ask: number;
}

/** The tiers a method with none of its own has, as the server holds them (backend comp.TIERS, the default of
 * Settings.comp_tiers): shown when the stored text doesn't read, as the server uses them then. */
export const DEFAULT_TIERS = "0.90:0.70, 0.75:0.60, 0.60:0.45, 0.45:0.30, 0:0.15";

const NUMBER = /^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*$/;

/** Highest first: the order the server reads them in (backend comp.tiers_of). */
export const byLeast = (a: Tier, b: Tier) => b.least - a.least || b.ask - a.ask;

function parse(text: string): Tier[] | null {
  const tiers: Tier[] = [];
  for (const piece of text.split(",")) {
    if (piece.trim() === "") continue;
    const two = piece.split(":");
    if (two.length !== 2 || !NUMBER.test(two[0]) || !NUMBER.test(two[1])) return null;
    tiers.push({ least: Number(two[0]), ask: Number(two[1]) });
  }
  tiers.sort(byLeast);
  // The lowest tier reaches 0, so every coverage is in one; without that the server uses the default tiers.
  return tiers.length > 0 && tiers[tiers.length - 1].least <= 0 ? tiers : null;
}

/** The stored tiers ("0.90:0.70, 0.75:0.60, ..."), highest first, read as the server reads them (backend comp.tiers_of):
 * pairs of numbers, the lowest reaching 0; anything else is the default tiers. */
export function readTiers(text: string): Tier[] {
  return parse(text) ?? parse(DEFAULT_TIERS)!;
}

/** A fraction as the stored text writes it: two decimals at least ("0.90", "0.625"), and a plain 0. */
function written(n: number): string {
  const s = String(n);
  if (n === 0) return "0";
  if (/^\d+$/.test(s)) return `${s}.00`;
  return /^\d+\.\d$/.test(s) ? `${s}0` : s;
}

/** The tiers as the stored text: highest first, the lowest reaching 0, each "least:ask" and joined by ", ". The default
 * tiers read and written again are the same text. */
export function writeTiers(tiers: Tier[]): string {
  const sorted = [...tiers].sort(byLeast);
  const last = sorted.length - 1;
  return sorted.map((t, i) => `${i === last ? "0" : written(t.least)}:${written(t.ask)}`).join(", ");
}

/** The numbers that have a range. */
export type RangedKey =
  | "comp_transferable_weight" | "comp_stance_min_siblings" | "comp_stance_clamp" | "comp_likely_low" | "comp_likely_high" | "comp_round_to";

interface Rule {
  key: RangedKey;
  /** Its step in the list on the page. */
  step: number;
  /** Kept as a fraction and shown as a percent. */
  percent: boolean;
  min: number;
  max: number;
  /** Whole numbers only. */
  whole: boolean;
  /** May be left blank. */
  blank: boolean;
  /** What a value outside the range is told, after "Step N: ". */
  says: string;
}

/** The ranges the server holds the numbers to (backend Settings), said in what the page shows: a share in percent, the
 * employer's move in points, counts and dollars whole. */
const RULES: Rule[] = [
  { key: "comp_transferable_weight", step: 2, percent: true, min: 0, max: 100, whole: false, blank: false,
    says: "a Transferable requirement counts as 0 to 100% of a Proven one" },
  { key: "comp_stance_min_siblings", step: 4, percent: false, min: 1, max: 20, whole: true, blank: false,
    says: "the employer's pay needs 1 to 20 other posted ranges to be judged" },
  { key: "comp_stance_clamp", step: 4, percent: true, min: 0, max: 50, whole: false, blank: false,
    says: "the ask moves by 0 to 50 points at most" },
  { key: "comp_likely_low", step: 5, percent: true, min: 50, max: 120, whole: false, blank: false,
    says: "the likely offer's low figure is 50 to 120% of the middle of the range" },
  { key: "comp_likely_high", step: 5, percent: true, min: 50, max: 120, whole: false, blank: false,
    says: "the likely offer's top figure is 50 to 120% of the middle of the range" },
  { key: "comp_round_to", step: 6, percent: false, min: 1, max: 50000, whole: true, blank: true,
    says: "round to a whole number of dollars from 1 to 50,000, or leave it blank" },
];

/** The range of one number, in what is shown (for the box's own `min` and `max`), and whether it is a percent or may be blank. */
export const rangeOf = (key: RangedKey): Rule => RULES.find((r) => r.key === key)!;

/** What is wrong with the numbers, one sentence each, by the number's name: every one outside the range the server holds
 * it to. Judged on what is shown, so a share is in percent. Nothing for numbers that are fine. */
export function problemsOf(comp: CompMethod): Partial<Record<RangedKey, string>> {
  const out: Partial<Record<RangedKey, string>> = {};
  for (const r of RULES) {
    const value: number | null | undefined = comp[r.key];
    if (value === null || value === undefined) {
      if (!r.blank) out[r.key] = `Step ${r.step}: ${r.says}.`;
      continue;
    }
    const shown = r.percent ? percentOf(value) : value;
    if (!Number.isFinite(shown) || shown < r.min || shown > r.max || (r.whole && !Number.isInteger(shown))) {
      out[r.key] = `Step ${r.step}: ${r.says}.`;
    }
  }
  return out;
}

/** The worked example as one plain sentence, from what the server's own `compute` gave (GET /profile/comp-example): the
 * made-up range, the share of the job shown, the ask, and the step that decided it. Only the server's figures are put into
 * words here; none is worked out. `capped_at_likely_offer` is the server saying the likely-offer limit held the ask down;
 * without it the tiers placed the ask. */
export function exampleLine(w: CompExample): string {
  const decided = w.capped_at_likely_offer
    ? "The likely-offer limit (step 5) decided it."
    : "The share of the range the tiers give (step 3) decided it.";
  return (
    `Example: a ${formatDollars(w.band[0])} to ${formatDollars(w.band[1])} range where you can show ${percentOf(w.coverage)}% of the job ` +
    `gives an ask of ${formatDollars(w.ask)}. ${decided}`
  );
}
