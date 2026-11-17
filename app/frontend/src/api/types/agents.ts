import type { UUID } from "./shared";

// --- Agents (GET .../agents, .../agents/{id}) -----------------------------------------------------------

export type AgentStatus = "working" | "idle" | "offline";

export interface AgentCurrent {
  task_title: string;
  /** The word "Job search". */
  project_title: string;
  started_at: string;
  // Set when the task is held and nothing has been heard from its holder for too long: the time of the last
  // sign of life. The Agents page says "Stuck since ..." instead of that it is working.
  stuck_since?: string | null;
  /** Which of the agent's roles the work is for ("scout"): one runner can hold several and takes them in turn. */
  role?: string | null;
}

export type AgentAvailability = "Free" | "On a task" | "Offline";

/** Free, or busy with N of M slots; when its newest task started. With several runners the slots are the runners that are
 * there and busy the ones with a session at work. */
export interface AgentCapacity {
  free: boolean;
  busy: number;
  slots: number | null;
  words: string;
  since: string | null;
}

/** One runner of the agent (a container of the scout's image: an install with lanes runs several at once) and what it is
 * at. `not_seen` is a runner that has said nothing for too long to count as there. */
export interface AgentRunner {
  /** How it names itself: "search", "analysis-1", "owner", "writer". */
  name: string;
  /** The name in words: "Analysis 1". */
  label: string;
  state: "working" | "idle" | "not_seen";
  /** The kind of session it has while it works ("analyst", "reader", "titles"...), else null. */
  kind: string | null;
  /** What it is doing, in words ("Researching Lead at Contoso", "Reading postings", "Idle"); null when not seen. */
  task: string | null;
  /** When it entered this state (a session's start; since it went idle); when it was last heard, if not seen. */
  since: string;
  seen_at: string;
}

export interface AgentSummary {
  connection_id: UUID;
  name: string;
  description: string | null;
  status: AgentStatus;
  // Owner Pause/Resume: it is handed no work and never woken while true.
  paused: boolean;
  // "live" while a session is checking in, "not_responding" when the newest session went silent, null otherwise.
  session_state: "live" | "not_responding" | null;
  checked_in_at: string | null;
  // The last time it called the app at all; null if it never has.
  last_seen_at: string | null;
  declared_at: string | null;
  current: AgentCurrent | null;
  /** The role keys it holds (data: "scout" today, "writer" or "reviewer" later). */
  roles: string[];
  /** The roles in words: "Scout". */
  roles_words: string;
  availability: AgentAvailability;
  /** The model in words, from what it declared; null until it says. */
  model_words: string | null;
  capacity: AgentCapacity;
  /** Every runner, when there are several (an install with lanes); empty for one, which is the agent as it always was. */
  runners: AgentRunner[];
}

export interface AgentActivityItem {
  at: string;
  text: string;
  detail: string | null;
}

export interface AgentMayDo {
  /** What it may do, in words. An agent with one kind of work tells it here; one with several tells here only what none
   * of them ever does ("Never applies, fills a form or sends anything."), once. */
  summary: string;
  /** An agent with several kinds of work: each kind once, under its short label (its role's title). Empty for one kind. */
  rules: { kind: string; title: string; rule: string }[];
}

export interface AgentDetail extends AgentSummary {
  activity: AgentActivityItem[];
  may_do: AgentMayDo;
}

export interface EditAgentIn {
  paused: boolean;
}

// --- The trail (what happened, newest first) -----------------------------------------------------------------

/** Matched the plan, didn't, or neither. */
export type TrailMark = "on" | "off" | "neutral";
export type TrailFilter = "all" | "needs_look" | "highlights";
/** How much a line matters: what you need to see, a result, or a routine step (quiet). */
export type TrailTier = "needs_you" | "result" | "routine";

interface TrailStep {
  at: string;
  text: string;
}

/** The one "Details" an entry may open: the agent's own words, its evidence, the steps it ran. */
export interface TrailDetails {
  words: string | null;
  evidence: string | null;
  steps: TrailStep[];
}

export interface TrailEntry {
  id: string;
  at: string;
  /** A person, an agent by its name, or "resumebot" for the app itself. */
  who: string;
  /** "app" is the app itself; the page says "resumebot". */
  actor: "person" | "agent" | "app";
  /** What kind of event it was, for tests and grouping; never shown. */
  event: string;
  tier?: TrailTier;
  /** One plain sentence, at most 140 characters. */
  sentence: string;
  mark: TrailMark;
  details: TrailDetails | null;
  link: { kind: "search_run" | "task" | "opportunity"; id: UUID } | null;
  /** The search run an agent was handed on this line: "What was sent" opens its instructions (How they work). */
  sent_task_id?: UUID | null;
  /** Only on a collapsed line ("8 items matched the plan"): how many, and the list it expands to. */
  count?: number;
  entries?: TrailEntry[];
}

export interface TrailPage {
  items: TrailEntry[];
  next_before: string | null;
}

// --- Mail login (Agent Settings) -------------------------------------------------------------------------------------

/** A mail provider's help: how to find it by an address, where its inbox is, and the steps for an app password. */
export interface MailProvider {
  name: string;
  /** The domains of its addresses; none for "Another provider". */
  match: string[];
  host: string;
  port: number;
  steps: string[];
}

/** The saved mail login as the screen sees it. The password is never in it: it is written and not read back. */
export interface MailLogin {
  connected: boolean;
  username: string | null;
  host: string | null;
  port: number | null;
  provider: string | null;
  providers: MailProvider[];
}

export interface MailLoginTest {
  ok: boolean;
  message: string;
  folders: string[];
}

// --- How they work (the Agents page's tab) -------------------------------------------------------------------------

/** One kind of session's cost on a night, at API prices, with what it produced (the unit its cost is divided by). */
export interface CostKind {
  sessions: number;
  turns: number;
  usd: number;
  unit?: string;
  per_unit_usd?: number | null;
}

/** One night's Claude cost at API prices (the nightly benchmark, `cost_report.py`). */
export interface CostNight {
  night: string;
  total: { usd: number; sessions: number };
  kinds: Record<string, CostKind>;
  roles: { analysed: number; ready: number; usd_per_ready: number | null; gated_usd: number; gated_roles: number };
}

/** One kind of agent, as the pool reports it: what it is doing, whether it is paused, how much waits for it. */
export interface AgentKind {
  kind: string;
  label: string;
  /** What the kind is for, in a sentence. */
  purpose: string;
  /** One line per busy session: "Researching Lead at Contoso". */
  doing: { session: string; task: string }[];
  paused: boolean;
  waiting: number;
  /** The writer's packets being written now (`waiting` is the roles still without one); only the writer has it. */
  writing?: number;
  /** The browser kinds' one health line (done in the last hour, the oldest waiting, a stall); null for the others. */
  health?: string | null;
  /** The instruction files it is sent. */
  instructions: { kind_of_file: string; name: string }[];
}

/** What Run now answers for one kind: the jobs it queued, and why none were when a limit or a pause holds the kind. */
export interface AgentRunResult {
  kind: string;
  queued: number;
  held: string | null;
  /** Set when nothing was queued but work waits, blocked: what it is stuck on. */
  blocked?: string;
}

/** What Run all answers: the jobs queued per kind, and the one reason that held any. */
export interface AgentRunAllResult {
  queued: Record<string, number>;
  held: string | null;
}

/** One file as the agent gets it: the owner's newest saved version, else the file shipped with the app (version 1). */
export interface HarnessFile {
  kind: "instructions" | "templates";
  name: string;
  title: string;
  text: string;
  chars: number;
  placeholders: string[];
  /** The version in force: 1 until the owner saves an edit. */
  version: number;
  /** The owner has saved a version of their own. */
  edited: boolean;
  /** The version in force was saved by a release (an auto-update), not by the owner. */
  from_app?: boolean;
  /** Plain words when an agent is working with the file right now (it can't be saved then), else null. */
  busy: string | null;
}

/** One version of a file in its history: `by` is "You" or "Shipped with the app"; `at` is null for the shipped one. */
export interface HarnessFileVersion {
  version: number;
  at: string | null;
  by: string;
  why: string;
  chars: number;
  current: boolean;
  shipped: boolean;
}

export interface HarnessFileHistory {
  kind: "instructions" | "templates";
  name: string;
  current: number;
  /** Newest first. */
  versions: HarnessFileVersion[];
}

/** What a read or a change of a file answers: the file as now in force, and its history. */
export interface HarnessFileReply {
  file: Omit<HarnessFile, "title">;
  history: HarnessFileHistory;
}

/** One earlier version of a file, with its text. */
export interface HarnessFileText {
  version: number;
  text: string;
  by: string;
  why: string;
  at: string | null;
}

export interface HarnessField {
  key: string;
  means: string;
  group: string;
}

/** What an agent is sent at one step. */
export interface HarnessSent {
  role_files: HarnessFile[];
  instruction: HarnessFile | null;
  template: HarnessFile | null;
  fields: HarnessField[];
  budget_chars: number;
  has_example: boolean;
}

/** One way out of a decision: where it goes, its rule in FEEL and in plain words, and what no rule can change. */
export interface HarnessWay {
  flow: string;
  to: string;
  to_title: string;
  name: string;
  condition: string | null;
  meaning: string;
  default: boolean;
  firm_meaning: string | null;
}

/** One step of a process the page draws from its BPMN file (written from the process specifications). `who` is
 * the lane, the actor in the specification's words; `actor`, `trigger`, `rules`, `outputs`, `owner_sees` and
 * `source` are the step's documentation under those headings; `harness_id` is the step of the task path an
 * agent's instructions and examples belong to. A start event carries what fires it in `trigger_kind` (message |
 * timer), `trigger_detail` and `trigger_subtype`; a question carries its conditions in `criteria`. */
export interface HarnessStep {
  /** A decision's ways out, each with its condition and plain meaning. */
  ways?: HarnessWay[];
  id: string;
  harness_id: string | null;
  title: string;
  type: "start" | "task" | "decision" | "subprocess" | "end";
  who: string;
  who_about: string;
  role: string | null;
  actor: string;
  trigger: string;
  /** For a start event: what fires it, and where it enters the process. */
  fired_by: string;
  enters_at: string;
  preconditions: string;
  inputs: string;
  /** The rules, numbered one to a line. */
  rules: string;
  outputs: string;
  exceptions: string;
  owner_sees: string;
  source: string;
  criteria: string | null;
  trigger_kind: string | null;
  trigger_detail: string | null;
  trigger_subtype: string | null;
  /** A call activity (type "subprocess"): the id of the sub-process it calls, which has its own tab. */
  calls: string | null;
  next: { to: string; label: string }[];
  sent: HarnessSent | null;
}

/** One field of a process's data model, which a decision's condition may read. */
export interface DataModelField {
  path: string;
  type: string;
  enum: string[];
  description: string;
}

/** One process the page draws. The top-level ones (`top_level`) are the main tabs; `children` are the ids of the
 * sub-processes it calls, in the order it first calls them: they sit under it as child tabs. A sub-process called
 * from several processes is one entry, listed under each. `slug` is its address (#/agents/how/<slug>). */
export interface HarnessProcess {
  /** The version of the process file in use (1 is the file as it shipped). */
  version?: number;
  /** What a condition can see: every field of the process's data model. */
  data_model?: DataModelField[];
  id: string;
  slug: string;
  name: string;
  about: string;
  top_level: boolean;
  children: string[];
  steps: HarnessStep[];
}

export interface HarnessOverview {
  budget_chars: number;
  roles: { role: string; who: string; about: string }[];
  processes: HarnessProcess[];
}

/** A real task as it was sent, with the size of each part. `source` is "sent" (the copy kept of what the agent
 * was sent: `label` is "as sent to <agent>", `sent_at` says when, `changed_files` the instruction, template and
 * role files that are different now) or "rebuilt" (an older task with no copy kept: `label` says it is rebuilt
 * as it would be sent now). */
export interface HarnessExample {
  task_id: string;
  title: string;
  payload: Record<string, unknown>;
  chars: number;
  budget_chars: number;
  parts: { key: string; chars: number }[];
  source: "sent" | "rebuilt";
  label: string;
  agent: string | null;
  sent_at: string | null;
  changed_files: { kind: string; name: string; now: "changed" | "gone"; version?: number }[];
  /** The files it was sent with and the version of each (`version` is missing on a copy kept before versions). */
  files?: { kind: string; name: string; version?: number | null }[];
}

/** One run of a process, as the live view lists it: where it is, in plain words. `alongside` is a shadow run,
 * which only checks what the app did and acts on nothing. */
export interface ProcessRun {
  id: string;
  /** The page's process (its tab): a process file's key, or a sub-process's. */
  process: string;
  name: string;
  slug: string;
  alongside: boolean;
  status: "running" | "waiting" | "completed" | "failed" | "cancelled";
  status_words: string;
  step_title: string | null;
  started_at: string;
  ended_at: string | null;
  /** The version of its process the run started on; null when that text is not among the versions kept. */
  version?: number | null;
}

/** A copy of what an agent was sent at a step the run reached; opens through the run's own page. */
export interface ProcessRunSent {
  task_id: string;
  agent: string | null;
  title: string;
  sent_at: string;
}

/** One run opened: the step it is at, the steps it passed, and what was sent at each agent step it reached. */
export interface ProcessRunDetail extends ProcessRun {
  /** The version in use now, so a run on an older one can say so. */
  version_now?: number | null;
  /** The step it is at (a finished run: the end it reached); null when that step isn't in the process now. */
  current: string | null;
  ended: boolean;
  passed: { step: string; at: string }[];
  waiting_for: { step: string; words: string }[];
  /** `{step id: [what was sent]}`, newest first. */
  sent: Record<string, ProcessRunSent[]>;
  /** Its sub-runs (a step that calls another process). */
  parts: ProcessRun[];
  /** The other runs of the same search. */
  others: ProcessRun[];
}

// --- changing how it works (docs/PRD.md path 16) ---------------------------------------------------------

/** A process file as it runs now: the text the editor starts from. */
export interface ProcessBpmn {
  process: string;
  number: number;
  xml: string;
  /** Plain words when the newest version no longer works with this release and an earlier one runs instead. */
  note: string | null;
}

/** Whether an edited file can run, in the engine's own words. */
export interface ProcessEditCheck {
  ok: boolean;
  problems: string[];
  changes: string[];
  note?: string;
}

/** One saved version of a process file. 1 is the file as it first shipped. */
export interface FileVersion {
  number: number;
  /** shipped (a file a release brought) | edit | rollback (a restore) */
  source: "shipped" | "edit" | "rollback" | "restore";
  at: string | null;
  by: string | null;
  why: string;
  current: boolean;
  restored: number | null;
  can_restore: boolean;
  /** What an edit of a process changed, in words. */
  summary?: string[];
  /** False for a version that no longer works with this release (with the engine's `problem`). */
  loads?: boolean;
  problem?: string | null;
}

export interface ProcessHistory {
  process: string;
  running: { number: number; note: string | null };
  versions: FileVersion[];
}

export interface ProcessVersionFile {
  process: string;
  number: number;
  xml: string;
}

export interface HarnessTaskSent {
  /** The process this step belongs to (a process file's key). */
  process: string;
  step: HarnessStep | null;
  example: HarnessExample | null;
}

// --- missions (GET/POST/PATCH /api/v1/missions) ---------------------------------------------------------------------

export type MissionSchedule =
  | { kind: "every"; hours: number }
  | { kind: "daily"; at: string }
  | { kind: "after"; mission: string }
  | { kind: "on_demand" }
  /** Started by the owner's own computer (Read in your browser), not by the app's clock. */
  | { kind: "outside" };

/** Claude's peak hours, which the owner can ask the missions to avoid (docs/PRD.md 25.8). */
export interface PeakView {
  avoid: boolean;
  /** The window in the owner's own time: "8:00 AM to 2:00 PM". */
  window: string;
  start: string;
  end: string;
  zone: string;
  /** Inside the window right now, with the box ticked. */
  now: boolean;
  resumes_words: string | null;
  /** The settings that set the window (only on the Missions list): drawn by the same form as a mission's. */
  numbers?: MissionNumber[];
}

/** One profile setting a mission runs with (backend missions/numbers.py): what to draw and its value now. */
export interface MissionNumber {
  /** The field of the profile's Settings, and the key saved. */
  name: string;
  label: string;
  /** One plain line saying what it does. */
  help: string;
  kind: "whole" | "fraction" | "flag" | "time" | "zone";
  low: number | null;
  high: number | null;
  /** Shown after the value ("%"). */
  unit: string;
  /** Can be left blank (none set). */
  optional: boolean;
  /** A count limit: it takes -1 for no limit as well as a number in its range (docs/PRD.md 25.12). */
  unlimited: boolean;
  value: number | boolean | string | null;
}

export interface MissionRun {
  id: string;
  started_by: string;
  started_at: string;
  ended_at: string | null;
  outcome: "running" | "done" | "failed" | "skipped";
  summary: string;
  search_run: string | null;
  /** The process instances it started, each opening the live run view. */
  instances: string[];
  /** How far the night got against its done condition ("12 of 20"), and what stopped it. */
  progress: { done: number; target: number; text?: string } | null;
  stopped: string | null;
}

/** The missions every install has, by key (backend missions/record.py `KINDS`). What the screen says of each (who does
 * it, whether Claude holds it, what finishes its night) is sent with it, so none of that is written here. A new mission
 * is its kind file on the server, its key here and, if it has settings of its own, their labels in MissionsScreen. */
export type MissionKey =
  | "read_boards" | "read_mail" | "research_employers" | "read_browser" | "scout_boards" | "analyse_finds" | "sweep_stuck" | "open_apply_tabs";

export interface Mission {
  id: string;
  key: MissionKey;
  name: string;
  purpose: string;
  /** The process it follows (a process key), or null for work the app does itself. */
  playbook: string | null;
  schedule: MissionSchedule;
  schedule_words: string;
  /** The done conditions it runs with, as the server holds them: the screen draws a field for each it is sent. */
  settings: Record<string, unknown>;
  /** Claude does its work: peak hours hold it and Run now is off inside them. A mission that is the app's own work says no. */
  needs_claude: boolean;
  /** The robot whose work it is, where one does it. */
  agent: "scout" | "analyst" | null;
  /** The app never starts it: a session on the owner's computer does, and its time is set there. */
  runs_elsewhere: boolean;
  /** What finishes its night, in one line; none for a mission that has no night. */
  done_when: string | null;
  state: "on" | "paused";
  paused_until: string | null;
  checked_at: string | null;
  next_run_at: string | null;
  last_run: MissionRun | null;
  /** The profile settings it runs with, each with its value now (none for the sweep). */
  numbers: MissionNumber[];
}

export interface MissionDetail extends Mission {
  runs: MissionRun[];
  changes: { at: string; by: string; what: string; why: string }[];
}
