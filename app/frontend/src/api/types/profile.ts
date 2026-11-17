import type { JobSystem, Site, TermsEntry } from "./where";

// --- the profile (GET /api/v1/profile, /profile/{part}/history; the profile spec) -------------------------

export type ProfilePart = "criteria" | "gates" | "scoring" | "settings";

/** Something from the owner's old profile that no field holds yet, kept word for word. */
export interface ProfileNote {
  id: string;
  text: string;
  source: string;
}

/** One role the owner targets: its name, a few example titles, and the parts of a posting written for it (in the owner's
 * words, as long as they like). A posting is judged against one role at a time. */
export interface TargetRole {
  /** A slug of the name when the role was added, unique among the roles; it never changes when the role is renamed. */
  id: string;
  name: string;
  /** Examples of what this role is called: not words a posting must carry. */
  titles: string[];
  description: string;
  responsibilities: string;
  /** What the required list should ask of the owner. */
  qualifications: string;
  /** The nice-to-haves the owner would like to see. */
  preferred: string;
  /** Benefits / Compensation: what the owner wants in pay and benefits. */
  compensation: string;
}

/** What to look for (docs/PRD.md 30.5): the company, then each role the owner targets. The document is saved whole, so
 * anything else stored in it (keys this page doesn't show) goes back as it came. */
export interface Criteria {
  /** Company: what kind of employer the owner wants. */
  company: string;
  target_roles: TargetRole[];
  /** How the owner presents themselves: read by the writer of resumes and letters and by the ranking (shown under Also sent to agents). */
  positioning?: string;
  /** What an older profile held that no field does (shown under Also sent to agents). */
  notes?: ProfileNote[];
  [stored: string]: unknown;
}

/** GET /api/v1/agent/ready: whether an agent is there to take work the owner asks for, and when not, why in words (peak
 * hours, no agent has checked in, it is paused). An agent busy with other work is there: the work is queued behind it, and
 * says so in the step of its own job. */
export interface AgentReady {
  ready: boolean;
  why: string | null;
}

/** What reading a pasted posting fills in for a role (GET /api/v1/profile/parse/{id}, once done). */
export interface ParsedRole {
  name: string;
  titles: string[];
  description: string;
  responsibilities: string;
  qualifications: string;
  preferred: string;
  compensation: string;
}

/** Reading a pasted posting (or an imported resume): waiting for an agent, reading it, then done (with the fields) or
 * failed (with why). A posting's fields are a role's; a resume's are `ParsedResume`. */
export interface ParseJob<F = Partial<ParsedRole>> {
  state: "waiting" | "reading" | "done" | "failed";
  /** What it is doing now, in words. */
  step: string;
  fields?: F;
  error?: string;
}

/** What reading an imported resume gives (GET /api/v1/profile/parse/{id}, once done): the resume's own words divided
 * into the master resume's parts, and the contact details, which go to the facts at the top of the master resume page. A part the resume does not
 * have is empty. */
export interface ParsedResume {
  summary: string;
  achievements: string[];
  competencies: { area: string; items: string[] }[];
  roles: {
    employer: string;
    title: string;
    location: string;
    start: string;
    end: string;
    intro: string;
    descriptor: string;
    bullets: string[];
  }[];
  education: { degree: string; school: string }[];
  philosophy: string;
  awards: string[];
  projects: string[];
  contact: ParsedContact;
}

/** The contact details a resume gives: the facts forms ask for, the first part of the master resume page. */
export interface ParsedContact {
  name: string;
  location: string;
  address: string;
  phone: string;
  email: string;
  links: string[];
}

/** One way of working (remote, hybrid or in person): accepted or not, and where it must be (docs/PRD.md 30.4). */
export interface GateWay {
  on: boolean;
  /** Names a posting's location line may use, separated by semicolons on the page: for remote, where remote work must be
   * open ("United States"); for hybrid and in person, the towns or areas. Empty: anywhere. */
  locations: string[];
}

/** Where the work happens: the app checks it. Each way of working is accepted or not, with the locations that count for it.
 * With no way accepted there is no work location gate. */
export interface GateLocation {
  remote: GateWay;
  hybrid: GateWay;
  in_person: GateWay;
}

export type PayAgainst = "minimum" | "maximum" | "ask";

/** The lowest pay: with no amount there is no pay gate. */
export interface GateCompensation {
  amount: number | null;
  against: PayAgainst;
  wide_band_ratio: number;
}

/** A gate in a part of a posting: a requirement the reader answers pass, fail or unknown, or a kind the app checks itself. It
 * has no switch of its own: its section has one (`GateSet.sections_off`). */
export interface Gate {
  id: string;
  kind: "rule" | "employer_fact" | "posting_age" | "job_fact";
  /** The part of a posting it belongs to. A gate saved before gates had parts has none: it loads into its kind's home
   * (a sentence in Description, an employer rule in Company, a posting age in Job posting), never guessed from its words. */
  section?: SectionId;
  text: string;
  days?: number | null;
  fact?: string | null;
  op?: string | null;
  value?: string | number | boolean | null;
  /** Title words only: a comma list of phrases inside which a listed word is no hit ("assistant vice president" lets that
   * title past "vice president"). Left out or empty: no exceptions. */
  except_words?: string | null;
  /** A rule the app decides itself from the analyst's classes of the role's required items, as an ask's `from_classes` is:
   * the class that counts, and `share`, the percent of the required items in that class at or above which the rule fails.
   * Both or neither. The page does not set them (nor an ask's); it keeps them as they came. */
  from_classes?: "U" | "T" | "P" | "PT" | null;
  share?: number | null;
  /** The target role (its id) this gate is for, on that role's tab of Hard pass; empty or left out: every role (the All
   * tab). The page only writes [] or one id; the server splits a gate that names several. */
  roles?: string[];
}

/** What one role's tab of Hard pass holds besides its gate lines: its own switches, work location and lowest pay. They add
 * to the ones on All, never replace them. */
export interface RoleGates {
  sections_off: SectionId[];
  location: GateLocation;
  compensation: GateCompensation;
}

/** The gates, grouped (docs/PRD.md 30.4). Employers are not here: each employer's gate is on its own record. The top
 * level is the All tab; `by_role` holds what a role's own tab adds. There is no gate on a role's fit. */
export interface GateSet {
  /** The sections switched off, in the page's order; none: every section is on. An off section stops nothing. */
  sections_off: SectionId[];
  location: GateLocation;
  compensation: GateCompensation;
  other: Gate[];
  /** At most two open applications per employer: past two, the other roles there are held out of the deck (34.2). On by default. */
  employer_limit: boolean;
  /** Each target role's own tab, by the role's id: only a role with something set has an entry. The server always sends
   * it ({} when there is nothing); a missing one is read as {}. */
  by_role?: Record<string, RoleGates>;
  /** Names that were gated here before employers had their own switch; they move onto employer records. */
  employer_names: string[];
  /** What an older profile held that no field does (a gate that was switched off, for one): kept word for word. */
  notes: ProfileNote[];
}

/** The parts of a posting asks are grouped by (docs/PRD.md 30.6), in the order a posting reads. */
export type SectionId =
  | "company"
  | "posting"
  | "description"
  | "responsibilities"
  | "qualifications"
  | "preferred"
  | "compensation";

/** One sentence about a posting, with the points it adds (or takes off); the reader answers it for each posting. */
export interface Ask {
  id: string;
  section: SectionId;
  says: string;
  /** The owner's Verify line: exactly how to check the ask against a posting, so the same posting gets the same answer every
   * time. Claude applies it exactly (met only when the posting shows what it names, not met when it shows otherwise, unknown
   * when it does not say). The owner's text, with no limit on its length; empty or left out: the ask is read by its sentence. */
  verify?: string;
  points: number;
  checked_by: "reader" | "app";
  per_item?: boolean;
  limit?: number | null;
  /** What the app decides it from, when it can: an employer fact, a posting's age, a pay tier or the requirement classes. */
  fact?: string | null;
  op?: string | null;
  value?: string | number | boolean | null;
  /** A posting's age in days: older than that (the default), or, with `above` false, posted within the last of them
   * (under days x 24 hours old, so 1 is under 24 hours; at least 1). */
  days?: number | null;
  amount?: number | null;
  /** Which way `amount` or `days` goes. A pay tier: over the amount (the default) or under it. A posting's age: older
   * than the days (the default) or within them. */
  above?: boolean | null;
  from_classes?: "U" | "T" | "P" | "all_P" | null;
  /** The target role (its id) this ask is for, on that role's tab of Prioritization; empty or left out: every role (the
   * All tab). The page only writes [] or one id; the server splits an ask that names several. */
  roles?: string[];
  /** The built-in ask that follows each role's Responsibilities (31.6): the owner can turn it off or remove it like any ask. */
  derived?: "responsibilities" | null;
}

/** What one role's tab of Prioritization holds besides its asks: its own switches. */
export interface RoleSwitches {
  sections_off: SectionId[];
}

/** The two kinds of row that hold several statements (the prioritization groups spec). `best` is the page's "First
 * match (top wins)": its members are statements, each with its own points, and the first one that is true counts. `any` is
 * "Any of these": its members are statements with no points of their own, and the row's points are earned once when any one
 * of them is true. (An "all of" row was dropped by the owner: every row already counts together.) The screens never say
 * "group" or "kind". The names are the model's (`GroupKind` in profile/scoring.py). */
export type GroupKind = "best" | "any";

/** A row of several statements, in one part of a posting and on one tab (`roles` as an ask's). Its members are ask-shaped: a
 * statement of a `best` row carries its own `points`, one of an `any` row carries none (the row's `points` are on the row).
 * They take the row's part and tab. */
export interface Group {
  id: string;
  kind: GroupKind;
  section: SectionId;
  /** The target role (its id) whose tab it is on; empty or left out: every role (the All tab). */
  roles?: string[];
  /** The row's points for `any`; 0 (or left out) for `best`. */
  points?: number;
  /** Two or more, in order: best first for a `best` row. */
  members: Ask[];
}

/** What an agent left for the owner to use or dismiss: asks the owner already has (by id, in the order proposed).
 * `best` and `any` offer to put them in one row ("First match", "Any of"); `remove` offers to take out asks the graded
 * requirements count again. It changes nothing until the owner uses it and saves; the app never writes
 * one itself. */
export interface Suggestion {
  id: string;
  kind: GroupKind | "remove";
  asks: string[];
  /** The "any of" row's points. */
  points: number;
  /** One plain sentence for the card. */
  why: string;
}

/** "Let the app grade the requirements" (Qualifications on All): the app grades each requirement of a posting from the
 * analyst's ratings (Proven, Transferable, Unproven) and scores it by these weights, one number for each: plus adds to the
 * fit and minus takes off, whole numbers. Off until the owner ticks it. */
export interface RequirementsGrade {
  on: boolean;
  /** For each required item, by its rating. */
  required_proven: number;
  required_transferable: number;
  required_unproven: number;
  /** For each preferred item: proven and transferable count the same. */
  preferred_proven_or_transferable: number;
  preferred_unproven: number;
}

export interface Scoring {
  /** The sections switched off, in the page's order; none: every section is on. An ask in an off section counts for nothing.
   * This is the All tab's. */
  sections_off: SectionId[];
  asks: Ask[];
  /** The "first match" and "any of" rows of asks. The server always sends it ([] when there are none); a missing one is read
   * as []. */
  groups?: Group[];
  /** What was left for the owner to use or dismiss. The server always sends it ([] when there are none). */
  suggestions?: Suggestion[];
  /** Whether the app grades the requirements, and by what weights. Null until the owner first ticks the box (the server
   * holds no default numbers); a missing or null one is read as off with the page's starting numbers. */
  grade_requirements?: RequirementsGrade | null;
  /** Each target role's own switches, by the role's id: only a role with a part switched off has an entry. The server always
   * sends it ({} when there is nothing); a missing one is read as {}. */
  by_role?: Record<string, RoleSwitches>;
  notes: ProfileNote[];
  /** How much "can I do it" counts against "does it fit what I want" when the read roles are put in order, 0 to 1: the share
   * of a read role's queue score that is its match (the Proven share of its graded required items), the rest the share of
   * its other asks it earned. The server always sends it (0.5 until the owner sets it); a missing one is read as 0.5. */
  match_weight?: number;
}

/** How we work out what to ask for (docs/PRD.md 26.6): kept in Settings, edited with the pay gate. */
export interface CompMethod {
  comp_basis: "base" | "total";
  comp_assumptions: string;
  comp_tiers: string;
  comp_transferable_weight: number;
  comp_stance_clamp: number;
  comp_stance_min_siblings: number;
  comp_likely_low: number;
  comp_likely_high: number;
  comp_round_to: number | null;
  comp_range_width: number | null;
}

/** The method worked on the made-up posting the settings page uses (GET /profile/comp-example): the range, the share of the
 * job shown (0 to 1), the ask, where the tiers alone put it (`by_method`), the likely offer, and whether the likely-offer
 * limit held the ask down. All from the server's own `compute`. */
export interface CompExample {
  band: [number, number];
  coverage: number;
  ask: number;
  by_method: number;
  likely_offer: number;
  capped_at_likely_offer: boolean;
}

export interface ProfileSettings extends CompMethod {
  rules: { id: string; area: string; says: string }[];
  notes: ProfileNote[];
}

export interface Profile {
  criteria: Criteria;
  gates: GateSet;
  scoring: Scoring;
  settings: ProfileSettings;
  /** How many versions each part has (0: never saved). */
  versions: Record<ProfilePart, number>;
  /** Each gate and ask as the one sentence the page shows: a gate group by its name, a line in Other and an ask by its id. */
  says: {
    gates: { location: string; compensation: string; other: Record<string, string> };
    asks: Record<string, string>;
  };
}

/** What a save answers: whether it made a version, its number, the line it says of what it did, and, for a part of the
 * profile, the whole profile as it now stands (so the page need not ask again). */
export interface ProfileSaved {
  saved: boolean;
  version: number | null;
  said?: string | null;
  profile?: Profile;
}

export interface ProfileVersion {
  version: number;
  saved_by: string;
  why: string;
  saved_at: string | null;
  /** What changed from the version before, in short phrases. */
  changed: string[];
}

// --- fits none of my roles, and Check my roles (docs/PRD.md 31.2, 31.3) --------------------------------------------

/** One role the owner pursued, or one posting found this week, that fits none of their target roles. `id` is a role's
 * or a posting's, as `kind` says; `stage` is the role's status, or `to_read` for a posting waiting for a reader. */
export interface FitsNoneItem {
  id: string;
  kind: "role" | "posting";
  title: string;
  company: string;
  stage: string;
  stage_label: string;
  found_at: string;
}

/** How many there are in all, and a page of them, newest first. */
export interface FitsNoneList {
  count: number;
  items: FitsNoneItem[];
}

/** GET /api/v1/profile/fits-none: what the role tabs miss. */
export interface FitsNone {
  pursued: FitsNoneList;
  this_week: FitsNoneList;
}

/** What a check looks at: the roles the owner pursued, a sample of recent postings the title check stopped, or one
 * posting they paste. */
export type CheckScope = "pursued" | "gated" | "posting";

/** One role or posting in a check's result, with the tab it was matched to (or the nearest one for a stop) and the line
 * why. `was` is the target role a role was found for. */
export interface CheckRow {
  id?: string;
  title: string;
  company: string;
  stage?: string;
  was?: string;
  tab?: string;
  tab_name?: string;
  why: string;
}

export interface CheckBucket {
  count: number;
  items: CheckRow[];
}

/** How many a tab would pass, and how many would be stopped under it as the nearest tab. */
export interface CheckTabCount {
  id: string;
  name: string;
  passed: number;
  stopped: number;
}

/** A check of the pursued roles or of stopped postings: `checked` of `of` were judged; each is passed (a tab claims it),
 * stopped (the title check would stop it) or fits none (it would only pass on a doubt). */
export interface CheckListResult {
  scope: "pursued" | "gated";
  checked: number;
  of: number;
  tabs: CheckTabCount[];
  passed: CheckBucket;
  stopped: CheckBucket;
  fits_none: CheckBucket;
}

/** A check of one pasted posting: the tab it matches, or none, and why. */
export interface CheckPostingResult {
  scope: "posting";
  matched: boolean;
  outcome: "passed" | "stopped" | "fits_none";
  tab: { id: string; name: string } | null;
  why: string;
}

/** GET /api/v1/profile/check/{id}: waiting for an agent, working (with how far along), done (with its result) or failed. */
export interface CheckJob {
  id: string;
  scope: CheckScope;
  state: "waiting" | "working" | "done" | "failed";
  step: string;
  progress?: { done: number; total: number };
  result?: CheckListResult | CheckPostingResult;
  error?: string;
}

/** What Where to look shows: the sites, and the job systems they post on. */
export interface WherePage {
  sites: Site[];
  job_systems: JobSystem[];
  /** Whether the owner switched on watching the job board of every employer with a role in play (docs/PRD.md 29.9);
   * GET /sites carries it. */
  watch_in_play?: boolean;
  /** The known sites whose terms forbid automated tools (docs/PRD.md 55.10); GET /sites carries it. */
  terms?: TermsEntry[];
  /** Whether the owner turned on reading a job board's public search by hand (docs/PRD.md 48.6); GET /sites carries
   * it, and no screen shows or changes it. */
  read_robots_exempt?: boolean;
}
