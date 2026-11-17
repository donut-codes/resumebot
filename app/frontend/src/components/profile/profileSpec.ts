// The vocabulary and word tables of Search Settings: the parts of a posting, the employer facts and comparisons a rule can
// use, where a gate sits, and the words of Prioritization's rows and band. The shapes are the backend's
// (backend/app/jobsearch/profile/, the profile spec).

import type { Gate, GateLocation, GateSet, RequirementsGrade, SectionId } from "../../api/types/profile";

export const EMPLOYER_FACTS: [string, string][] = [
  ["employees", "Employees"],
  ["years_since_founded", "Years since founded"],
  ["ownership", "Ownership (public, private, pe, vc, nonprofit, government)"],
  ["ai_native", "AI-native (true or false)"],
  ["industry", "Industry and sectors"],
  ["local", "Local (true or false)"],
  ["work_style", "Work style (remote, hybrid, onsite, mixed)"],
  ["years_since_layoff", "Years since a layoff"],
];

/** An employer fact's name without the hint in brackets, so a select shows it whole ("Ownership", not a line longer than the box). */
export const factName = (label: string): string => label.replace(/\s*\([^)]*\)\s*$/, "");

/** What a value for an employer fact can be, from the bracket on its label ("public, private, pe..."); empty when it can be anything. */
export function factValues(fact: string | null | undefined): string {
  const label = EMPLOYER_FACTS.find(([v]) => v === fact)?.[1] ?? "";
  return label.match(/\(([^)]*)\)\s*$/)?.[1] ?? "";
}

export const FACT_OPS: [string, string][] = [
  [">=", "at least"], ["<=", "at most"], [">", "more than"], ["<", "fewer than"], ["=", "is"], ["!=", "is not"],
  ["contains", "includes"], ["excludes", "doesn't include"],
];


/** The outline What to look for, Hard pass and Prioritization are all written by: the parts of a posting, in the order a posting
 * reads (backend profile_base.SECTIONS). An ask or a gate belongs to one of them. The ids are what is stored and stay as they
 * are, whatever the page calls a part. */
export const SECTIONS: [SectionId, string][] = [
  ["company", "Company"],
  ["posting", "Job posting"],
  ["description", "Description"],
  ["responsibilities", "Responsibilities"],
  ["qualifications", "Qualifications"],
  ["preferred", "Preferred"],
  ["compensation", "Benefits / Compensation"],
];

/** What an ask decided from the analyst's requirement classes means, in the plain words its line says (the key is the ask's
 * `from_classes`). */
export const CLASS_WORDS: Record<string, string> = {
  U: "Counts each Unproven item",
  T: "Counts each Transferable item",
  P: "Counts each Proven item",
  all_P: "Applies when every item is Proven",
};

/** The analyst's classes by name (the key is a rule's `from_classes`): the words a rule decided from them says. */
export const CLASS_NAMES: Record<string, string> = { U: "Unproven", T: "Transferable", P: "Proven", PT: "Proven or Transferable" };

/** Where a gate sits when none was stored (backend profile.GATE_HOME), from what it is and never from its words: a
 * sentence goes to Description, where the owner places it; an employer rule is about the company and a posting age about
 * the posting. */
export const GATE_HOME: Record<Gate["kind"], SectionId> = { rule: "description", employer_fact: "company", posting_age: "posting", job_fact: "posting" };

/** The part a gate is in. */
export const gateSection = (g: Gate): SectionId => g.section ?? GATE_HOME[g.kind] ?? "description";

/** The gates with every one in a part: a gate saved without one (an older profile) is put in its kind's home, the rest
 * are as they came. */
export function placeGates(gates: GateSet): GateSet {
  return { ...gates, other: gates.other.map((g) => ({ ...g, section: gateSection(g) })) };
}

/** The words of the two kinds of row that hold several statements, on Prioritization (the prioritization groups spec): a "first match" row, whose first true statement decides the points, and an "any of" row, which earns its
 * points once when any one statement is true. "first match", "any of", "statement" and "row" are the whole vocabulary; the
 * screens never say "group", "member", "kind", "pick one", "ranked list" or "all of", and a statement is never a "level" or
 * a "part" (a part is one of the seven sections of a posting). */
export const SET_WORDS = {
  best: {
    title: "First match (top wins)",
    hint: "The first one that is true counts. Put the best at the top.",
    /** The id a new statement or row starts from. */
    row: "statement",
    sentence: "The statement, one sentence about a posting",
    more: "Add a statement",
    /** What a screen reader hears for the button, before the row's name. */
    moreFor: "Add a statement to the row",
    remove: "Remove this row",
    unnamed: "a new statement",
    /** The row's name while it has no sentence yet. */
    blank: "a new row",
    worthMore: "A statement lower down is worth more than the one above it. The first true one from the top still counts.",
  },
  any: {
    title: "Any of these",
    hint: "Earns its points once, when any one of these is true.",
    row: "statement",
    sentence: "The statement, one sentence about a posting",
    more: "Add a statement",
    moreFor: "Add a statement to the row",
    remove: "Remove this row",
    unnamed: "a new statement",
    blank: "a new row",
  },
} as const;

/** Who decides an ask or a statement, in the one quiet line under it. */
export const DECIDER_WORDS = {
  claude: "Answered by: Claude, from the job posting",
  record: "Answered by: the company record",
  toApp: "Answer from the company record instead",
  /** In a first match row that has some statements the app decides: the same for every one, so the row is all the app's. */
  wholeRow: "Answer the whole row from the company record",
} as const;

/** What an ask starts with when the app is let to decide it from the employer's record: a fact and a comparison to choose
 * from, and a number left blank, which the app cannot compare with and so decides nothing until it is typed. */
export const EMPLOYER_START = { fact: "employees", op: ">=", value: "" } as const;

/** The numbers "Let the app grade the requirements" starts at, until the owner types others (the prioritization groups spec, rule 13): each required item graded Proven earns 2, Transferable 0 and Unproven -2; each preferred item graded
 * Proven or Transferable earns 1 and Unproven 0. They are the page's own starting numbers, one constant: the arithmetic
 * reads only the numbers that are saved. It starts off. */
export const GRADE_START: RequirementsGrade = {
  on: false,
  required_proven: 2,
  required_transferable: 0,
  required_unproven: -2,
  preferred_proven_or_transferable: 1,
  preferred_unproven: 0,
};

/** How much "can I do it" counts against "does it fit what I want" in the order of the read roles when the server sends no
 * number (`Scoring.match_weight`): half, as the server's own default. */
export const MATCH_WEIGHT_START = 0.5;

/** The words of the grading setting, in Qualifications on All's tab. */
export const GRADE_WORDS = {
  check: "Let the app grade the requirements",
  hint: "The analyst rates each requirement in a posting as Proven, Transferable or Unproven. With this on, each one earns the points set here.",
} as const;

/** The band at the head of Prioritization while a suggestion is left for the owner to use or put off. */
export const BAND_WORDS = {
  head: "Some of your asks look like choices between each other.",
  lead:
    "Two asks that cannot both be true of one posting, counted one by one, let a role earn both and keep every role under 100%. " +
    "Putting them in one row fixes that. Nothing changes until you save.",
  use: "Use this row",
  later: "Not now",
  /** The second block, only while the requirements are graded: asks that count what grading counts again. */
  overlapHead: "Grading counts the same things as some of your asks.",
  overlapLead: "Keeping both counts a requirement twice. Nothing changes until you save.",
  drop: "Remove these asks",
} as const;

/** The sections of Hard pass that have a switch, in the page's order: the seven of the outline. */
export const GATE_SECTION_ORDER: SectionId[] = SECTIONS.map(([id]) => id);

/** The ids of the sections that are off, once each, in the page's order (the shape that is stored). */
export function inPageOrder<T extends SectionId>(off: readonly T[]): T[] {
  return GATE_SECTION_ORDER.filter((id): id is T => (off as readonly SectionId[]).includes(id));
}

const listWords = (items: string[], limit = 6): string => items.slice(0, limit).join(", ") + (items.length > limit ? ` and ${items.length - limit} more` : "");

const orWords = (items: string[], limit = 6): string => {
  const shown = items.slice(0, limit);
  const more = items.length - shown.length;
  if (more > 0) return `${shown.join(", ")} and ${more} more`;
  return shown.length === 1 ? shown[0] : shown.length ? `${shown.slice(0, -1).join(", ")} or ${shown[shown.length - 1]}` : "";
};

/** The work location as one sentence, from the lists as they are typed (the backend's `location_says`, so what the page
 * says is what a Gated posting is told): the ways of working that are accepted, and where each must be. */
export function locationSays(loc: GateLocation): string {
  const ways: string[] = [];
  if (loc.remote.on) ways.push("remote" + (loc.remote.locations.length ? ` (in ${listWords(loc.remote.locations)})` : ""));
  for (const [label, way] of [["hybrid", loc.hybrid], ["in person", loc.in_person]] as const) {
    if (way.on) ways.push(way.locations.length ? `${label} (in ${listWords(way.locations)})` : `${label} (anywhere)`);
  }
  return ways.length ? `Work must be ${orWords(ways)}.` : "No way of working is allowed yet.";
}

/** The one plain line under each part of What to look for (docs/PRD.md 30.5 and 31.6): what the part does, in the words
 * of what the code does with it. Company, the example titles and Description stop a posting (the title check compares a
 * posting's title and employer with them; a posting that clearly doesn't fit is stopped); Responsibilities scores it
 * (a reader's answer to the built-in ask, worth the points set on Prioritization) while that ask is there, and only
 * informs the reader once the owner has removed it (`responsibilitiesUnscored`); Qualifications, Preferred and
 * Compensation only inform the reader, who reads a posting against them, and change no stop and no score by themselves. */
export const CRITERIA_HELP = {
  company: "Stops a posting whose employer clearly isn't this kind. What kind of employer you want: industry, size, how it's run.",
  titles: "Stops a posting whose title clearly isn't like these. Separated by semicolons. Each posting's title is compared with these by meaning, along with Company and Description, so a few close examples are enough.",
  description: "Stops a posting that clearly isn't this work, and a reader reads each posting against it. The work, in your own words.",
  responsibilities: "Scores a posting: a reader asks whether its work matches this, and the points are set on Prioritization. What you want to be doing day to day.",
  // The same part once the owner has removed the built-in ask on Prioritization: nothing asks about it, so it only informs.
  responsibilitiesUnscored: "Only informs the reader. What you want to be doing day to day. It stops nothing and scores nothing by itself.",
  qualifications: "Only informs the reader. What a posting's required qualifications should ask of you. It stops nothing and scores nothing by itself.",
  preferred: "Only informs the reader. The nice-to-haves you'd like to see. It stops nothing and scores nothing by itself.",
  compensation:
    "Only informs the reader. What you'd want in pay and benefits. The lowest pay you'd accept stops a posting on Hard pass; how pay affects fit is set on Prioritization.",
} as const;
