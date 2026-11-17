// The measurable dials (docs/PRD.md path 46) as Search Settings shows them: one row for each, with its choice (Must, Prefer,
// Don't care: Avoid is gone, Prioritization only adds points, 30.10) and its band or pick list. A dial is not a new kind of
// stored thing: a Must is a line of Hard pass (a gate on the dial's fact) and a Prefer a line of Prioritization (an ask the app
// decides, with points), and
// this file reads those lines as a dial and writes a dial back as them. The table and the sentences are the server's
// (`app/jobsearch/measures/dials.py`); the app checks each against a stored fact, never a reader.

import type { Ask, Gate, GateSet, Profile, Scoring } from "../../api/types/profile";

export type Choice = "must" | "prefer" | "dont_care";
export const CHOICE_WORDS: Record<Choice, string> = { must: "Must", prefer: "Prefer", dont_care: "Don't care" };

export interface Option {
  value: string;
  label: string;
}

export interface DialSpec {
  id: string;
  side: "employer" | "job";
  label: string;
  fact: string;
  /** `flag` has nothing to set: choosing Must is the whole setting (Employer named: the posting names its employer). */
  kind: "pick" | "number" | "words" | "flag";
  choices: Exclude<Choice, "dont_care">[];
  /** A number's unit, in a sentence ("miles"). */
  unit?: string;
  /** The words before a number's figure when they are not "within" ("At least", "At most"). */
  lead?: string;
  /** What a sentence calls the fact. */
  noun: string;
  /** A pick list's values. An industry value is the word an employer's industry holds. */
  options?: Option[];
  /** A words dial's box caption (the sentence above its words box). */
  caption?: string;
  /** Whether a words dial has an "Except in these phrases" box: Title words only. */
  takesExcept?: boolean;
}

const ALL: DialSpec["choices"] = ["must", "prefer"];

export const DIALS: DialSpec[] = [
  {
    id: "size", side: "employer", label: "Size", fact: "size_band", kind: "pick", choices: ALL, noun: "the employer's size",
    options: [
      { value: "micro", label: "Under 10 employees" }, { value: "small", label: "10 to 99" },
      { value: "mid", label: "100 to 999" }, { value: "large", label: "1,000 or more" },
    ],
  },
  {
    id: "ownership", side: "employer", label: "Ownership", fact: "ownership", kind: "pick", choices: ALL, noun: "the employer's ownership",
    options: [
      { value: "public", label: "Public" }, { value: "private", label: "Private" }, { value: "pe", label: "Private equity" },
      { value: "vc", label: "Venture-backed" }, { value: "nonprofit", label: "Nonprofit" }, { value: "government", label: "Government" },
    ],
  },
  {
    id: "industry", side: "employer", label: "Industry", fact: "industry", kind: "pick", choices: ALL, noun: "the employer's industry",
    options: [
      { value: "health", label: "Healthcare" }, { value: "software", label: "Software and technology" },
      { value: "financ", label: "Finance and banking" }, { value: "insurance", label: "Insurance" },
      { value: "educat", label: "Education" }, { value: "government", label: "Government" },
      { value: "manufactur", label: "Manufacturing" }, { value: "retail", label: "Retail" }, { value: "energy", label: "Energy" },
      { value: "media", label: "Media" }, { value: "nonprofit", label: "Nonprofit" }, { value: "consult", label: "Consulting" },
      { value: "telecom", label: "Telecommunications" }, { value: "transport", label: "Transportation and logistics" },
    ],
  },
  { id: "distance", side: "job", label: "Distance from home", fact: "distance_miles", kind: "number", choices: ALL, unit: "miles", noun: "the distance from home" },
  { id: "experience", side: "job", label: "Years of experience asked", fact: "experience_years", kind: "number", choices: ALL, unit: "years", noun: "the years of experience asked" },
  {
    id: "degree", side: "job", label: "Degree required", fact: "degree_required", kind: "pick", choices: ALL, noun: "the degree required",
    options: [
      { value: "degree", label: "A degree, level not stated" }, { value: "associate", label: "Associate" },
      { value: "bachelor", label: "Bachelor's" }, { value: "master", label: "Master's" }, { value: "doctorate", label: "Doctorate" },
    ],
  },
  { id: "freshness", side: "job", label: "Freshness", fact: "posted_days", kind: "number", choices: ALL, unit: "days", noun: "the days since it was posted" },
  // The owner's two hard passes that were dropped with every reader-judged line (docs/PRD.md 46.4, 2026-11-03): travel is a
  // ceiling in percent, and a posting that states none passes; a posting must name its employer.
  { id: "travel", side: "job", label: "Travel", fact: "travel_percent", kind: "number", choices: ["must"], unit: "%", lead: "At most", noun: "the travel asked" },
  { id: "pay", side: "job", label: "Pay", fact: "pay_top", kind: "number", choices: ["prefer"], unit: "a year", noun: "the top of the posted pay" },
  {
    id: "mode", side: "job", label: "Work mode", fact: "work_mode", kind: "pick", choices: ["prefer"], noun: "the way of working",
    options: [{ value: "remote", label: "Remote" }, { value: "hybrid", label: "Hybrid" }, { value: "onsite", label: "Onsite" }],
  },
  {
    id: "clearance", side: "job", label: "Clearance", fact: "clearance", kind: "pick", choices: ["prefer"], noun: "the security clearance",
    options: [
      { value: "none", label: "No clearance" }, { value: "required", label: "A clearance required" },
      { value: "preferred", label: "A clearance preferred" },
    ],
  },
  // The owner's own words and phrases, a Must only (docs/PRD.md 46.4, 46.5, 2026-10-28): there is no list of ours.
  { id: "title", side: "job", label: "Title words", fact: "title", kind: "words", choices: ["must"], noun: "the title",
    caption: "A title with any of these is screened out. Separate words and phrases with commas.", takesExcept: true },
  { id: "employer-named", side: "job", label: "Employer named", fact: "employer_named", kind: "flag", choices: ["must"], noun: "the employer" },
  // The owner's "Add roles which lead business development as a gate" (docs/PRD.md 46.4, 2026-11-05): yes or no, read from the
  // posting by a short read of the sentences that speak of selling. A Must on No stops the roles that lead it; nothing is preset.
  {
    id: "business-development", side: "job", label: "Leads business development", fact: "leads_bd", kind: "pick", choices: ALL,
    noun: "whether the role leads business development", options: [{ value: "yes", label: "Yes" }, { value: "no", label: "No" }],
  },
  // The role's main work (docs/PRD.md 46.4, 2026-11-12): the owner's words, a Must only. A short read of the posting names the
  // word the main work is, with a quote; a posting that does not say passes. No exceptions box.
  {
    id: "main-work", side: "job", label: "Main work", fact: "main_work", kind: "words", choices: ["must"], noun: "the role's main work",
    caption: "A role whose main work is any of these is screened out. Separate words and phrases with commas. A posting that does not say passes.",
  },
];

/** The comparison a dial's line holds: a pick list is "in"; a list of the owner's words is "excludes" (a title with any is
 * stopped); a flag is "=" (its value is "yes"); a number reads toward what the user wants ("within 50 miles"). */
export function opFor(dial: DialSpec): string {
  if (dial.kind === "pick") return "in";
  if (dial.kind === "words") return "excludes";
  if (dial.kind === "flag") return "=";
  return dial.id === "pay" ? ">=" : "<=";
}

const wordsOf = (value: unknown) => String(value ?? "").split(",").map((v) => v.trim()).filter(Boolean);

const SENTENCE_OP: Record<string, string> = { "<=": "within", "<": "under", ">=": "at least", ">": "more than" };

/** A dial's line in a sentence: the condition the app checks. */
export function dialSays(dial: DialSpec, value: string | number, except = ""): string {
  const noun = dial.noun[0].toUpperCase() + dial.noun.slice(1);
  if (dial.kind === "flag") return "The posting names its employer.";
  if (dial.kind === "words") {
    const words = wordsOf(value).map((v) => v.toLowerCase());
    const joined = words.length > 1 ? `${words.slice(0, -1).join(", ")} or ${words[words.length - 1]}` : words[0] ?? "nothing listed";
    const skipped = wordsOf(except).map((v) => v.toLowerCase());
    const unless = skipped.length > 1 ? `${skipped.slice(0, -1).join(", ")} or ${skipped[skipped.length - 1]}` : skipped[0];
    return `${noun} has none of: ${joined}${unless ? `, except in: ${unless}` : ""}.`;
  }
  if (dial.kind === "pick") {
    const labels = String(value).split(",").map((v) => dial.options?.find((o) => o.value === v)?.label.toLowerCase() ?? v);
    const joined = labels.length > 1 ? `${labels.slice(0, -1).join(", ")} or ${labels[labels.length - 1]}` : labels[0];
    return `${noun} is ${joined}.`;
  }
  const n = Number(value);
  const figure = n.toLocaleString("en-US");
  const shown = dial.id === "pay" ? `$${figure} a year` : dial.unit === "%" ? `${figure}%` : `${figure} ${dial.unit ?? ""}`.trim();
  return `${noun} is ${dial.lead?.toLowerCase() ?? SENTENCE_OP[opFor(dial)]} ${shown}.`;
}

/** A dial as the stored lines hold it. `gate` and `ask` are the lines it came from. */
export interface DialState {
  dial: DialSpec;
  choice: Choice;
  value: string | number;
  points: number;
  /** Title words' exceptions: phrases a listed word is no hit inside. Empty for every other dial. */
  except: string;
  gate?: Gate;
  ask?: Ask;
}

/** Whether a stored line is a dial this page can show: on the dial's fact, with the comparison the page writes, and, for a pick
 * list, only values the list has. Anything else (an employer rule written before the dials, a value the list lacks) stays in its
 * own list, as it was. */
function reads(dial: DialSpec, line: { fact?: string | null; op?: string | null; value?: unknown }): boolean {
  if (line.fact !== dial.fact || line.op !== opFor(dial)) return false;
  if (dial.kind === "number") return line.value !== "" && line.value !== null && Number.isFinite(Number(line.value));
  if (dial.kind === "words") return wordsOf(line.value).length > 0;
  if (dial.kind === "flag") return String(line.value) === "yes";
  const known = new Set(dial.options?.map((o) => o.value));
  const values = wordsOf(line.value);
  return values.length > 0 && values.every((v) => known.has(v));
}

const isMust = (dial: DialSpec, g: Gate) =>
  g.kind === (dial.side === "job" ? "job_fact" : "employer_fact") && !(g.roles ?? []).length && reads(dial, g);
const isAsk = (dial: DialSpec, a: Ask, scope: Scoring) =>
  !(a.roles ?? []).length && !(scope.groups ?? []).some((g) => g.members.some((m) => m.id === a.id)) &&
  a.points > 0 && reads(dial, a);

/** Each dial as the profile holds it: the first line of Hard pass or Prioritization on its fact that the row can show. A dial
 * with no such line is Don't care. */
export function readDials(profile: Profile): DialState[] {
  return DIALS.map((dial) => {
    const gate = profile.gates.other.find((g) => isMust(dial, g) && dial.choices.includes("must"));
    const ask = profile.scoring.asks.find((a) => isAsk(dial, a, profile.scoring));
    if (gate) return { dial, choice: "must", value: dial.kind === "number" ? Number(gate.value) : String(gate.value), points: 0, except: gate.except_words ?? "", gate };
    if (ask) {
      return {
        dial, choice: "prefer", value: dial.kind === "number" ? Number(ask.value) : String(ask.value),
        points: ask.points, except: "", ask,
      };
    }
    return { dial, choice: "dont_care", value: dial.kind === "number" ? "" : "", points: 0, except: "" };
  });
}

/** The ids of the lines the rows show, so the lists do not show them twice. */
export function claimed(profile: Profile): Set<string> {
  const ids = new Set<string>();
  for (const s of readDials(profile)) {
    if (s.gate) ids.add(s.gate.id);
    if (s.ask) ids.add(s.ask.id);
  }
  return ids;
}

/** Whether a dial set this way says enough to be stored: a value, and points for a Prefer. Until it does it is only
 * what the user has typed. */
export const complete = (dial: DialSpec, choice: Choice, value: string | number, points: number) =>
  choice === "dont_care" ||
  ((dial.kind === "number" ? value !== "" && Number.isFinite(Number(value)) && Number(value) >= 0 : wordsOf(value).length > 0) &&
    (choice === "must" || (Number.isInteger(points) && points > 0)));

/** The two documents with this dial set: its old lines taken out, and its new one put in (a Must in Hard pass, a Prefer in
 * Prioritization). A document the change does not touch is returned as it was. When the choice and the value are
 * what the old ask already held, its sentence stays (the owner's words); a new condition is said in the dial's own words. */
export function applyDial(
  profile: Profile, state: DialState, choice: Choice, value: string | number, points: number, except = state.except,
): { gates: GateSet; scoring: Scoring } {
  const { dial } = state;
  let gates = profile.gates;
  let scoring = profile.scoring;
  const sameCondition = state.choice === choice && String(state.value) === String(value);
  if (state.gate) gates = { ...gates, other: gates.other.filter((g) => g.id !== state.gate?.id) };
  if (state.ask) scoring = { ...scoring, asks: scoring.asks.filter((a) => a.id !== state.ask?.id) };
  if (choice === "must") {
    const section = dial.side === "job" ? "posting" : "company";
    const line: Gate = {
      id: state.gate?.id ?? `dial-${dial.id}-must`, kind: dial.side === "job" ? "job_fact" : "employer_fact", section, text: "",
      fact: dial.fact, op: opFor(dial), value, roles: [],
    };
    if (dial.takesExcept && wordsOf(except).length > 0) line.except_words = wordsOf(except).join(", ");
    gates = { ...gates, other: [...gates.other, line] };
  } else if (choice !== "dont_care") {
    const signed = Math.abs(points);
    const line: Ask = {
      id: state.ask?.id ?? `dial-${dial.id}`, section: dial.side === "job" ? "posting" : "company",
      says: sameCondition && state.ask ? state.ask.says : dialSays(dial, value), points: signed, checked_by: "app",
      fact: dial.fact, op: opFor(dial), value, roles: [],
    };
    scoring = { ...scoring, asks: [...scoring.asks, line] };
  }
  return { gates, scoring };
}
