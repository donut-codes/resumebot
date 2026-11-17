import type { ProfileNote } from "./profile";

// --- who is here (GET /api/v1/me) ---------------------------------------------------------------------

/** The app the owner is using. There is one owner and no sign-in, so this says only which release it is. */
export interface Me {
  version: string;
  /** The owner's IANA time zone ("Pacific/Auckland"): every time on the page is shown on this clock. */
  timezone?: string;
}

// --- the owner's own documents (GET /api/v1/documents; saved through PUT /api/v1/profile/{kind}) ------------------

export type DocumentPart = "resume" | "about" | "writing";

/** A resume line with the note that governs how it may be used. */
export interface ResumeLine {
  id: string;
  text: string;
  note: string;
}

export interface ResumeBullet extends ResumeLine {
  group: string;
}

export interface ResumeRole {
  id: string;
  employer: string;
  location: string;
  title: string;
  start: string;
  end: string;
  intro: string;
  descriptor: string;
  optional: boolean;
  note: string;
  /** The tools used in this job. */
  tools: string[];
  bullets: ResumeBullet[];
  /** Detail from this job for tailoring, never printed. */
  evidence: ResumeEvidence[];
}

export interface ResumeEvidence {
  id: string;
  title: string;
  text: string;
}

export interface Resume {
  summary: string;
  summary_variants: ResumeLine[];
  achievements: ResumeLine[];
  philosophy: string;
  competencies: { id: string; area: string; items: string[] }[];
  roles: ResumeRole[];
  education: { id: string; degree: string; school: string; note: string }[];
  projects: ResumeLine[];
  awards: ResumeLine[];
  notes: ProfileNote[];
}

export interface About {
  name: string;
  location: string;
  metro_location: string;
  address: string;
  phone: string;
  email: string;
  links: string[];
  work_authorization: string;
  sponsorship: string;
  current_company: string;
  notes: ProfileNote[];
}

/** The writing rules, in six sections: one-point rules for your voice, ATS screening, the resume (Global, then one list for each of its sections, then rules for a target role; Global holds the rules for
 * documents Settings once held) and cover letters (the same shape: Global, a list for each part of the letter, rules for a target role), then settings for QA of documentation (`review_steps`,
 * `drift_patterns`, `review_flags`, `banned`, `humanizing`, `style_off` and `credentials`). `page_margin_inches` is the Word export's margin on all four
 * sides (0.25 to 1.5), or null for the export's own. */
export interface Writing {
  voice_rules: { id: string; says: string }[];
  ats_rules: { id: string; says: string }[];
  /** Rules for interviews: the owner's own interview habits, one point to a rule; only interview prep is handed them (docs/PRD.md 15.2.14). */
  interview_rules: { id: string; says: string }[];
  resume_global: { id: string; says: string }[];
  resume_header: { id: string; says: string }[];
  resume_summary: { id: string; says: string }[];
  resume_competencies: { id: string; says: string }[];
  resume_achievements: { id: string; says: string }[];
  resume_experience: { id: string; says: string }[];
  resume_education: { id: string; says: string }[];
  /** Rules for tailoring a resume for one target role, by the role's id. A role deleted in Search Settings keeps its rules. */
  role_rules: Record<string, { id: string; says: string }[]>;
  page_margin_inches: number | null;
  letter_global: { id: string; says: string }[];
  letter_header: { id: string; says: string }[];
  letter_greeting: { id: string; says: string }[];
  letter_opening: { id: string; says: string }[];
  letter_body: { id: string; says: string }[];
  letter_closing: { id: string; says: string }[];
  letter_signoff: { id: string; says: string }[];
  /** Rules for writing a cover letter for one target role, by the role's id: a map of its own, apart from the resume's. */
  letter_role_rules: Record<string, { id: string; says: string }[]>;
  banned: { id: string; phrase: string; why: string }[];
  /** "Never write" entries that were regular expressions, from before phrases were plain words: still checked as they were, shown read-only until placed. */
  legacy_patterns: { id: string; pattern: string; regex: boolean; why: string }[];
  review_steps: { id: string; says: string }[];
  drift_patterns: { id: string; name: string; looks_like: string; fix: string }[];
  review_flags: { id: string; says: string }[];
  humanizing: { id: string; says: string }[];
  /** The ids of the style rules (`StyleRule.id`) the owner has switched off. Empty is every rule on, which is how a writing document
   * saved before the style rules loads. An id this page does not know (a rule from another version of the app) is kept as it is. */
  style_off: string[];
  /** The owner's own certification and licence names, checked beside the app's list: a packet that names one the master does not hold is refused. */
  credentials: { id: string; name: string }[];
  notes: ProfileNote[];
}

/** One of the checks that ship with the app, run on every draft: its plain-words name, the QA rule it enforces, and the packet check it backs. */
export interface RuleCheck {
  id: string;
  name: string;
  rule: string;
  backs: string;
}

/** One of the style rules that ship with the app, each on until the owner switches it off (`Writing.style_off`): its id, its plain-words name, and the sentence that says what it asks. */
export interface StyleRule {
  id: string;
  name: string;
  rule: string;
}

export interface Documents {
  resume: Resume;
  about: About;
  writing: Writing;
  versions: Record<DocumentPart, number>;
}

// --- Story library (GET /api/v1/documents/story-library): read-only ---------------------------------------------------

export interface StoryLine {
  id: string;
  text: string;
  from: string[];
  held: boolean;
  why_held?: string;
}
export interface StoryRole {
  role: string;
  title: string;
  employer: string;
  scope: StoryLine | null;
  lines: StoryLine[];
}
export interface StoryBlock {
  id: string;
  for: string;
  text: string;
  from: string[];
  held: boolean;
  why_held?: string;
}
export interface Story {
  id: string;
  title: string;
  index: number;
  blocks: StoryBlock[];
}
export interface StoryLibrary {
  version: number | null;
  built_at: string | null;
  current: boolean;
  building: boolean;
  roles: StoryRole[];
  achievements: StoryLine[];
  stories: Story[];
}

// --- Find Gaps (GET /api/v1/find-gaps and below; docs/PRD.md path 38) -------------------------------------------------

/** The analyst's own classes: Proven, Transferable, Unproven. There is no second scale. */
export type GapGrade = "P" | "T" | "U";

export interface GapRole {
  /** The target role's id. */
  id: string;
  name: string;
  /** How many researched roles in the Pipeline are matched to it. */
  researched: number;
  /** Their required items, counted by the analyst's class. */
  counts: Record<GapGrade, number>;
  /** Whole percents of those, adding to 100; null when nothing was classed. */
  percent: Record<GapGrade, number> | null;
  /** The role's latest finished run, or null. */
  last_run: { id: string; at: string | null } | null;
}

/** Where a run is, as the dashboard and the polling read it. */
export interface GapBrief {
  id: string;
  /** The target role it reads for; null for a title that is not one. */
  role_id: string | null;
  title: string;
  kind: "role" | "new";
  state: "waiting" | "working" | "done" | "failed";
  /** In words: "Waiting for an agent", "Queued: the agent is busy researching a role... This starts as soon as it has
   * finished." (while a session of the agent is at work), "3 of 10 read", "Done". */
  step: string;
  read: number;
  total: number;
  error?: string;
}

/** A role in the app the owner can start a run from: one that holds the text of its posting. */
export interface GapJobOption {
  id: string;
  company: string;
  title: string;
}

/** A finished run started from a job, to open again. */
export interface GapJobRun {
  id: string;
  company: string;
  title: string;
  at: string | null;
}

export interface GapDashboard {
  roles: GapRole[];
  /** The run going now (any role, or a title typed, or a job), or null. */
  active: GapBrief | null;
  /** The last run that failed, when none is going. */
  failed: GapBrief | null;
  /** The finished runs started from a job, newest first. */
  job_runs: GapJobRun[];
}

export interface GapPosting {
  ref: string;
  employer: string;
  title: string;
  url: string | null;
}

export interface GapTheme {
  id: string;
  name: string;
  kind: "required" | "preferred" | "responsibility";
  /** How many of the sampled postings ask it, of `of`. */
  asked: number;
  of: number;
  grade: GapGrade;
  /** The grade in the analyst's own word. */
  words: string;
  why: string;
  /** The master lines behind a Proven or Transferable grade (none for an Unproven one). */
  lines: { id: string; text: string }[];
  /** The refs of the postings that asked it (`GapRunView.postings`). */
  postings: string[];
  /** What the owner said of it: "not_done" is "I haven't". */
  answer: "not_done" | null;
  answer_id: string | null;
}

export interface GapRunView extends GapBrief {
  /** "Only 7 employers have a posting for this role; this reads 7." */
  note: string | null;
  checked_at: string | null;
  /** The version of the instruction file the run used. */
  instructions_version: number | null;
  /** Whether Check again and New sample apply (a target role's run). */
  can_repeat: boolean;
  postings: GapPosting[];
  themes: GapTheme[];
  /** The themes set aside as not relevant for the role, to bring back. */
  hidden: { id: string; name: string; kind: string }[];
  /** The master resume has changed since the run was graded. */
  stale: boolean;
  /** The role a run started from a job is about; null for the other runs. */
  job: { id: string; company: string; title: string } | null;
  /** The Coach's cards and proposals (a finished run started from a job); null for the other runs. */
  clarify: GapClarify | null;
}

/** One question the Coach asked about a theme of a run started from a job (docs/PRD.md 38.13, 38.14). */
export interface GapCard {
  id: string;
  theme: string;
  grade: "T" | "U";
  question: string;
  /** "not_true": the owner said they have not done it; the theme is never asked again for the role. */
  state: "open" | "answered" | "skipped" | "not_true";
  /** The owner's own words, whole. */
  answer: string;
  /** False once a line has been suggested from the answer. */
  can_change: boolean;
}

/** A master line the Coach proposed from an answer (38.15, 38.16; docs/PRD.md 8.3's minimal shape). */
export interface GapProposal {
  id: string;
  theme: string;
  /** The words: the Coach's, or the owner's own once they were saved. */
  text: string;
  why: string;
  status: "proposed" | "accepted" | "rejected";
  /** Who suggested it: the Coach (`coach.gaps`) or the owner's own Claude (`owner.claude`), whose quote is what Claude says
   * the owner told it. */
  source?: string;
  /** Where it goes: a new line under a job, or evidence on one of its lines. */
  place: { kind: "new_line" | "evidence"; job: string; line: string | null };
  question: string;
  /** The owner's answer it came from, whole. */
  quote: string;
}

/** The Coach's latest job on a run unless it is done: waiting, working or failed (why). */
export interface GapCoachBrief {
  id: string;
  mode: "ask" | "propose";
  state: "waiting" | "working" | "done" | "failed";
  step: string;
  error?: string;
}

export interface GapClarify {
  cards: GapCard[];
  proposals: GapProposal[];
  coach: GapCoachBrief | null;
  /** Work through the gaps has questions to ask. */
  can_ask: boolean;
  /** Suggest lines has answers to suggest from. */
  can_propose: boolean;
}

/** A line drafted from the owner's own words: waiting, working, then done (the line) or failed (why). */
export interface GapDraft {
  id: string;
  state: "waiting" | "working" | "done" | "failed";
  step: string;
  draft?: string;
  error?: string;
}

/** One required item of a posting, classed against the master resume (docs/PRD.md 26.1). */
export interface RequirementRow {
  index: number;
  item: string;
  /** N: the line can't be measured from a resume (docs/PRD.md 26.12): in no count, shown as a neutral box. */
  class: "P" | "T" | "U" | "N";
  /** The number of the posting line this item answers (cut by code); null for research kept before lines were numbered. */
  line?: number | null;
  why: string;
  by: "analyst" | "owner";
  owner_why: string;
  from: { id: string; text: string }[];
  /** The heading the posting lists a duty under; "" for a duty with none and for research kept before duties had one. */
  group?: string;
}
