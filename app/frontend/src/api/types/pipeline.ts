import type { PeakView } from "./agents";
import type { Employer, LiveEmployer } from "./employers";
import type { Notification, Task } from "./inbox";
import type { RequirementRow } from "./me";
import type { SectionId } from "./profile";

/** POST /api/v1/roles: the job the pasted link is. `already` says the owner had it (nothing was made): `role` is the role
 * that holds it, in `stage`, or, for a posting that is no role yet, `role` is null and `posting` is its id. A new job is a
 * posting in Landing Zone (`stage` landing_zone), in the Added links list while nothing has named it (`added_links`), or in Gated out when a gate stopped it. `said` is the answer in words. */
export interface RoleAdded {
  role: string | null;
  posting: string | null;
  already: boolean;
  said: string;
  stage: string;
  stage_label: string;
  title: string;
  company: string;
}

// --- the board (GET /api/v1/board, POST /api/v1/roles/{id}/move) ------------------------------------------------

/** A role's row: what the Pipeline list shows for one role, whole and finished. It is the server's `role_row.Row`: a
 * column's card, a move's and a star's answer, a live change read again, and the top of the role's own page
 * (GET /api/v1/roles/{id}) are all this one shape, and every word on it, `next` included, is the server's: the screen
 * draws it and rewrites nothing. A key added here is added to `role_row.Row` and to `role-row.json` in the same change
 * (`role-row.test.ts` checks the keys against that file, and the backend checks its own row against it). */
export interface BoardCard {
  id: string;
  title: string;
  company: string;
  /** A status, or `added_links` for a posting that waits there. */
  status: string;
  status_label: string;
  /** The fit as a percent of the highest the owner's scoring allows (docs/PRD.md 23.4). */
  fit_pct: number | null;
  /** The role's one number, the one the list is sorted by, 0 to 100 (docs/PRD.md 24.11, 24.15): its Haiku rank as written
   * until Sonnet has read it, Sonnet's score after, with the first hours' bonus of a new posting and less the stale penalty
   * of an old one (a new role scored 70 shows 90); null for a role with no score, which shows `fit_pct` if it has one. */
  score?: number | null;
  /** What is wrong with the role or waits on the owner, in the server's words ("The packet failed", "Posting not
   * confirmed when checked today", "They said no"); null for the normal run of a stage and for a posting that is no role
   * yet (docs/PRD.md 24.18). */
  next: string | null;
  /** When Go opened the role's tab (an instant), or null: it stays in Ready to Launch until the mail confirms or the
   * owner marks it Applied. Set means Launch in Browser opened it. */
  opened_at?: string | null;
  /** The standard answers for a row: how the work happens (Remote, Hybrid · City, In person · City, Not stated), as the
   * role's own words say it; the pay in one format; the age in days and whether from posting or finding. */
  work: { mode: "remote" | "hybrid" | "onsite" | "unknown"; place: string | null; words: string };
  pay: string;
  /** The predicted ask (method 4, never lifted to the owner's lowest pay), once the role is researched; else null. */
  ask?: number | null;
  age: { days: number | null; words: string; from: "posted" | "found" | null };
  /** Why a gate stopped it. */
  reason: string | null;
  /** Why an Ended role ended (they_said_no, i_passed, posting_gone, no_reply or unknown), and in words; null while in
   * play. The move box offers nine of them, none required; the quiet rules end a role with posting_gone or no_reply. */
  ended_reason: string | null;
  ended_words: string | null;
  /** The link the role is read and opened from: the employer's own posting once the app has found it, else the job
   * board's. */
  url: string | null;
  apply_url: string | null;
  /** Where the role came from: your tracker, a search, a link you pasted in, or a posting that is no role yet. */
  origin: "tracker" | "scout" | "owner" | "posting";
  /** The employer's record, for its page. */
  employer_id: string | null;
  /** The role's newest packet version, or null when none was asked for; `progress` is where it is on its way. */
  packet: { id: string; version: number; status: PacketStatus; progress?: PacketProgress } | null;
  /** The owner's star (PUT /api/v1/roles/{id}/favorite): a favorite is fast-tracked and holds a Ready to Launch place. */
  favorite?: boolean;
  favorited_at?: string | null;
  /** In its first hours, whatever its stage. */
  new?: boolean;
  /** The points of `score` that are the first hours' bonus: the New tag says them, New (+20). 0 once the hours are over,
   * for a rank of 0, for a role with no score and when the owner turned the bonus off. */
  first_hours_bonus?: number;
}

export type PacketStatus = "writing" | "ready" | "approved" | "held" | "replaced" | "failed";

/** Where a packet is on its way, for a building bar: `done` of `of` steps (`fraction`, 0 to 1). */
export interface PacketProgress {
  step: "asked" | "checking" | "fixing" | "done";
  label: string;
  done: number;
  of: number;
  fraction: number;
}

/** How many roles each lane holds (Added links included), how many each of the two groups the page draws apart holds
 * (`work` and `ranked`, the sums of their lanes, docs/PRD.md 24.17), each Closed reason, and the favorites. */
export interface LaneCounts {
  lanes: Record<string, number>;
  groups: Record<string, number>;
  closed: Record<string, number>;
  closed_total: number;
  favorites: number;
}

/** A move's answer (POST /api/v1/roles/{id}/move and /move-back): the role's card as its new column shows it, and
 * the counts after the move. */
export type MoveResult = BoardCard & { counts: LaneCounts };

/** A star's answer (PUT /api/v1/roles/{id}/favorite). */
export interface FavoriteResult {
  favorite: boolean;
  changed: boolean;
  /** It moved to Ready to Launch. */
  moved: boolean;
  /** What the research ask said, when one was made. */
  research: { role: string; state: string | null; already: boolean; again: boolean; said: string } | null;
  /** The packet asked, when one was. */
  packet: string | null;
  role: BoardCard;
  counts: LaneCounts;
}

/** One row of a search (GET /api/v1/search): a role in any status, or a posting that is no role yet. */
export interface SearchRow {
  kind: "role" | "posting";
  id: string;
  title: string;
  company: string;
  where: string | null;
  /** A role's status, or a posting's place: added_links, stopped or not_a_role_yet. */
  status: string;
  label: string;
  /** The board lane it is in (closed for every closed status), or null for a posting outside Added links. */
  lane: string | null;
  closed_reason: string | null;
  closed_label: string | null;
  /** A gate's, an ended role's or the filter's reason, in words. */
  reason: string | null;
  favorite: boolean;
  fit_pct: number | null;
  found_at: string | null;
  url: string | null;
}

export interface SearchPage {
  q: string;
  page: number;
  size: number;
  pages: number;
  total: number;
  roles: number;
  postings: number;
  /** How many match in each status or place, only those with any. */
  stages: Record<string, number>;
  closed: number;
  items: SearchRow[];
}

/** The live stream's messages (GET /api/v1/live, server-sent events), by event name. */
export interface LiveRole {
  id: string;
  title: string | null;
  company: string | null;
  from: string | null;
  to: string | null;
  lane_to: string | null;
  label: string | null;
  closed_reason: string | null;
  reason: string | null;
  favorite: boolean;
  changed: ("status" | "favorite" | "new")[];
}

/** A task as the inbox shows it, or one that was removed. */
export type LiveTask = Task | { id: string; deleted: true };

/** A notification as the inbox shows it, with whether it had been read before this change (null for a new one); or one
 * that was removed. */
export type LiveNotification = (Notification & { was_read: boolean | null }) | { id: string; deleted: true };

export interface LivePacket {
  id: string;
  role_id: string;
  version: number;
  status: PacketStatus;
  from: PacketStatus | null;
  progress: PacketProgress;
}

/** The screens that are told their own data changed (`changed`): each reads one endpoint of its own again. */
export type LiveArea = "jobs" | "gaps" | "agents" | "missions" | "harness" | "prep" | "go" | "ready";

/** A `changed` message: which screen's data, and the rows (or what names them to the screen). No data is in it. */
export interface LiveChanged {
  area: LiveArea;
  ids: string[];
}

export interface LiveEvents {
  /** The counts come when the stream asked for roles or counts. */
  hello: { counts?: LaneCounts };
  role: LiveRole;
  counts: LaneCounts;
  packet: LivePacket;
  /** A role's summary was written: a page with the role open reads it again. */
  summary: { id: string };
  /** A task was raised or changed (as the inbox shows it), or removed (`deleted`). */
  task: LiveTask;
  /** A notification was raised or read, or removed (`deleted`). */
  notification: LiveNotification;
  /** An employer was made or changed (the fields its list row shows), or removed (`deleted`). */
  employer: LiveEmployer;
  /** A version of a profile part was saved (here, on another page, by an agent or a restore), or its reason was said. */
  profile_version: LiveProfileVersion;
  /** A site on Where to look was added or changed (the plain columns the server held), or removed (`deleted`). */
  site: LiveSite;
  /** A screen's data changed: the area, and the rows that changed. Read the screen's own endpoint again. */
  changed: LiveChanged;
  resync: Record<string, never>;
}

/** A `profile_version` message: which part, its new version, who saved it and why. */
export interface LiveProfileVersion {
  kind: "criteria" | "gates" | "scoring" | "settings";
  version: number;
  saved_by: string;
  why: string;
  saved_at: string | null;
}

/** A `site` message: its id and the columns the server held, or that it was removed. The rest of its row (its status, its
 * roles) is read again with the sites. */
export type LiveSite = { id: string; deleted: true } | { id: string; url?: string; label?: string | null; on?: boolean; list?: string; read_by?: string };

/** What the Go button did: the roles it opened a tab for, the ones it skipped and why (no apply link, already applied,
 * posting closed), and any note (mail not set up). */
/** Where Go stands (GET and POST /api/v1/apply/go): it checks the day's Ready to Launch roles, builds their packets and
 * opens each role's tab as its packet is done. `total` roles are to open, `built` of them have a finished packet. */
export interface ApplyGo {
  phase: "idle" | "asked" | "checking" | "opening" | "building" | "done" | "stopped";
  total: number;
  built: number;
  checked?: number;
  opened: { id: string; title: string; company: string; url: string }[];
  skipped: { id: string; title: string; company: string; why: string }[];
  notes: string[];
  /** The role whose packet is being built now, and what it waits for. */
  current?: { id: string; title: string; company: string; step: string } | null;
  /** Every role being built now, side by side (up to the owner's limit on active sessions). */
  building?: { id: string; title: string; company: string; step: string }[];
  done: boolean;
}

export interface BoardColumn {
  key: string;
  label: string;
  count: number;
  cards: BoardCard[];
  /** Set when the column was asked for by page: the page it holds and how many there are. */
  page?: number;
  pages?: number;
}

export interface Board {
  columns: BoardColumn[];
  /** The reasons offered when a role is ended by hand (docs/PRD.md 31.7). */
  ended_reasons?: EndedReason[];
}

/** One reason a role may end: "I passed on it", "They said no", "The posting went". */
export interface EndedReason {
  reason: string;
  label: string;
}

// --- packets (POST /api/v1/roles/{id}/packet; a role's packets also come with the role) -----------------------------

export interface PacketCheck {
  check: string;
  ok: boolean;
  blocking: boolean;
  detail: string;
}

/** A line of a tailored document and the master lines it was cut from. */
export interface PacketLine {
  text?: string;
  from?: string[];
}

export interface PacketRole {
  role: string;
  employer?: string;
  title?: string;
  location?: string;
  start?: string;
  end?: string;
  intro?: string;
  bullets?: PacketLine[];
}

/** The tailored resume (backend packets.py): every part is there only when the writer wrote it. */
export interface PacketResume {
  name?: string;
  contact?: string[];
  headline?: string;
  summary?: string;
  competencies?: { area: string; items: string[] }[];
  achievements?: PacketLine[];
  roles?: PacketRole[];
  education?: { degree: string; school?: string }[];
}

/** Why the packet says what it says. */
export interface PacketReasoning {
  problem?: string;
  headline?: string;
  chosen?: { from: string; why: string }[];
  left_out?: { from: string; why: string }[];
  letter?: string;
  for_owner?: string[];
}

export interface Packet {
  id: string;
  role_id: string;
  version: number;
  status: PacketStatus;
  feedback: string;
  resume: PacketResume;
  letter: { paragraphs?: string[]; none?: string; from?: string[] };
  reasoning: PacketReasoning;
  checks: PacketCheck[];
  created_at: string | null;
  progress?: PacketProgress;
  /** Why the files are not in the packet folder, while the last save failed (a ready packet only). */
  files_problem?: string | null;
}

// --- one role whole (GET /api/v1/roles/{id}) ----------------------------------------------------------------------

export interface RoleEvent {
  at: string | null;
  kind: string;
  who: string;
  sentence: string;
  detail: string | null;
}

/** A researched role's summary (the analyst's three lines). */
export interface RoleSummary {
  company: string;
  role: string;
  why: string;
}

export interface RoleDetail {
  role: BoardCard & {
    notes: string | null;
    found: string | null;
    posted: string | null;
    board_found_on: string | null;
    /** The employer's own posting of the role, when the app has found it; null when the role has only its job board
     * link. `board_url` is the job board link it was found at (null for a role found on the employer's own site). */
    employer_url?: string | null;
    board_url?: string | null;
    ats: string | null;
    level: string | null;
    sector: string | null;
    /** The posting's text as it was read, kept after the posting comes down. */
    description: string | null;
    description_at: string | null;
    /** Whose copy the text is, when it is not the posting's own source's: "board" while the employer's own posting
     * could not be read (docs/PRD.md 48.14 f); the employer's text replaces it when it comes. */
    description_from?: "board" | null;
  };
  employer: Employer | null;
  scoring: RoleScoring;
  gates: GateResult[];
  /** Three short lines from the analyst: what the company does, what the role is, and why it is good for the owner.
   * Null until the role has one. */
  summary?: RoleSummary | null;
  /** The role is researched, has no summary and none is asked yet: opening it asks for one. */
  summary_askable?: boolean;
  /** The posting's required items against the master (docs/PRD.md 26.1). */
  requirements?: RequirementRow[];
  /** The posting's preferred items, classed the same way; none for a role researched before they were classed. */
  preferred?: RequirementRow[];
  /** The posting's duties, one item for each, classed the same way (P, T or U) and with no line numbers; none for a role
   * researched before they were classed. Read-only. */
  responsibilities?: RequirementRow[];
  peak?: PeakView;
  timeline: RoleEvent[];
  packets: Packet[];
  /** The role's interviews, by time (docs/PRD.md 15.2). */
  interviews?: Interview[];
}

// --- Interviews and the Coach's prep report (docs/PRD.md 15.2) ---------------------------------------------------------

export type RoundKind = "recruiter_screen" | "hiring_manager" | "panel" | "executive" | "other";

export interface InterviewPerson {
  id: string;
  name: string;
  title: string;
}

/** What the owner sends to add or change an interview. `when` is an ISO instant with an offset. */
export interface InterviewInput {
  when?: string;
  round_kind?: RoundKind;
  label?: string;
  people?: { name: string; title: string }[];
  notes?: string;
}

export type PrepState = "waiting" | "writing" | "review_waiting" | "reviewing" | "ready" | "failed" | "lapsed";

/** Where a fact came from, and the day it was checked. */
export interface PrepSource {
  url: string;
  title: string;
  checked_on: string;
}
/** A resolved citation: a master line or a library story. */
export interface PrepCite {
  id: string;
  text: string;
}
/** A line in the owner's voice, with the master lines or stories behind it. */
export interface PrepClaim {
  text: string;
  from?: string[];
  cites?: PrepCite[];
}

export interface PrepAnswer {
  kind: "story" | "judgement";
  title?: string;
  text: string;
  result?: string;
  from?: string[];
  cites?: PrepCite[];
}

export interface PrepInterviewer {
  name: string;
  found: boolean;
  background?: string;
  screens_for?: string;
  connection?: PrepClaim | null;
  dont: string;
  questions?: string[];
}

export interface PrepQuestion {
  question: string;
  asked_by?: string | null;
  method?: string;
  answer?: PrepAnswer | null;
  gap?: string | null;
}

export interface PrepAsk {
  question: string;
  round: "recruiter_screen" | "hiring_manager" | "panel" | "executive";
  priority: number;
  diagnoses: string;
  good_answer: string;
  bad_answer: string;
  follow_up: string;
}

export interface PrepMarket {
  low: number;
  high: number;
  basis: "base" | "total" | "hourly";
  source?: PrepSource;
}

/** The report as the page gets it: what the Coach wrote (`app/templates/jobsearch.coach.report.template.md`) with the
 * Quick Sheet and the pay answer made by code, and each citation resolved. */
export interface PrepReport {
  quick_sheet: {
    when_at: string;
    round_label: string;
    people: { name: string; title: string }[];
    core_need: string;
    opener: string;
    pay: { expectation: string | null; market: { low: number; high: number; basis: string } | null };
    first_choice: { question: string; round: string }[];
    story_map: { question: string; story: string }[];
  };
  open_gaps: { text: string; action: string }[];
  core_need: { text: string; from_title_only?: boolean };
  good_looks_like: { text: string; source?: PrepSource | null }[];
  stage_read: { screens_for: string; interviewers: PrepInterviewer[] };
  staples: {
    opener: PrepClaim[];
    why_here: { text: string; source?: PrepSource | null; from?: string[]; cites?: PrepCite[] }[];
    why_leaving: { text?: string; from?: string[]; cites?: PrepCite[]; ask_owner?: string };
    pay: { expectation: string | null; assumptions: string | null; market: PrepMarket | null };
  };
  questions: PrepQuestion[];
  problem?: { kind: "approach" | "first_engagement"; approach: string; discovery_questions: string[] } | null;
  ask: PrepAsk[];
  reference: { text: string; source: PrepSource }[];
}

export interface PrepReview {
  summary: string;
  findings: { where: string; problem: string; fix: string }[];
}

export interface Prep {
  id: string;
  version: number;
  state: PrepState;
  /** The status line, in plain words ("Waiting for the Coach", "Researching", "Checking", "Ready", or what went wrong). */
  step: string;
  error: string | null;
  /** What changed in the interview since the report was made, in words. */
  changed: string[];
  report: PrepReport | null;
  review: PrepReview | null;
}

export interface Interview {
  id: string;
  role_id: string;
  when_at: string;
  round_kind: RoundKind;
  round_label: string;
  label: string;
  people: InterviewPerson[];
  notes: string;
  past: boolean;
  can_prepare_again: boolean;
  versions: { id: string; version: number; state: PrepState }[];
  prep: Prep | null;
}

/** How an ask was answered for one posting: the reader's word (or the app's, for an ask it decides itself). */
type Answer = "met" | "partly" | "not" | "unknown";

/** One of the owner's asks, for one role: its answer, the points it earned and why (docs/PRD.md 30.6). */
export interface ScoredAsk {
  id: string;
  section: SectionId;
  says: string;
  /** The points the ask is worth when met, as the role was scored. */
  points: number;
  /** Null for an ask added after this role was scored. */
  answer: Answer | null;
  /** The points it earned: all of them when met, none otherwise (no partial credit; a stored "partly" earns none); null
   * when not scored. */
  earned: number | null;
  evidence: string;
  /** Answered; can't be told; or added since the role was scored. */
  state: "checked" | "unknown" | "unscored";
}

/** How a role scored: each of the owner's asks, answered. */
export interface RoleScoring {
  asks: ScoredAsk[];
}

export interface GateResult {
  id: string;
  kind: string;
  says: string;
  result: "pass" | "fail" | "unknown" | "waits";
  fact: string;
}
