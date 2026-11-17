// Typed fetch wrappers over the same-origin /api/v1 surface (the app serves the page from the same origin; the Vite dev proxy forwards
// /api to the backend locally). Errors are normalized into ApiError with a message a screen can show directly
// ("A search is already running.") instead of a raw fetch/JSON failure.
//
// The calls are grouped by screen, each under its own heading.

import type { AgentDetail, AgentKind, CostNight, AgentRunAllResult, AgentRunResult, AgentSummary, EditAgentIn, HarnessExample, HarnessFileReply, HarnessFileText, HarnessOverview, HarnessTaskSent, MailLogin, MailLoginTest, Mission, MissionDetail, MissionRun, MissionSchedule, PeakView, ProcessBpmn, ProcessEditCheck, ProcessHistory, ProcessRunDetail, ProcessVersionFile, TrailFilter, TrailPage } from "./types/agents";
import type { Employer, EmployerMergePreview, EmployerMerged, EmployerPage, EmployerQuery, EmployerSplit, EmployerSplitPreview } from "./types/employers";
import type { DecisionHistory, Inbox, Task, TaskList } from "./types/inbox";
import type { DocumentPart, Documents, GapDashboard, GapDraft, GapJobOption, GapProposal, GapRunView, Me, Resume, RuleCheck, StoryLibrary, StyleRule } from "./types/me";
import type { ApplyGo, Board, BoardCard, BoardColumn, FavoriteResult, Interview, InterviewInput, MoveResult, Packet, RoleAdded, RoleDetail, SearchPage } from "./types/pipeline";
import type { AgentReady, CheckJob, CheckScope, CompExample, CompMethod, FitsNone, ParsedRole, ParseJob, Profile, ProfilePart, ProfileSaved, ProfileVersion, TargetRole, WherePage } from "./types/profile";
import type { RoleTrend, Site, SiteList, SiteReadBy, SiteRolesPage } from "./types/where";

const BASE = "/api/v1";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** The words to show for a failed call: the server's own message when it sent one (an `ApiError`),
 *  otherwise `fallback`. The one place every screen's `err instanceof ApiError ? err.message : "..."` lives. */
export function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError("Can't reach the server. Check your connection and try again.", 0);
  }
  if (!resp.ok) throw new ApiError(await extractError(resp), resp.status);
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

async function extractError(resp: Response): Promise<string> {
  try {
    const body = await resp.json();
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail)) {
      return body.detail.map((d: { msg?: string }) => d.msg).filter(Boolean).join("; ") || resp.statusText;
    }
  } catch {
    // not JSON
  }
  if (resp.status === 404) return "Not found.";
  if (resp.status === 409) return "That can't happen right now.";
  if (resp.status === 422) return "Something about that wasn't valid.";
  return resp.statusText || `Request failed (${resp.status})`;
}

function get<T>(path: string): Promise<T> {
  return request<T>(path);
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
}

function put<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "PUT", body: body === undefined ? undefined : JSON.stringify(body) });
}

function patch<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "PATCH", body: body === undefined ? undefined : JSON.stringify(body) });
}

function del<T>(path: string): Promise<T> {
  return request<T>(path, { method: "DELETE" });
}

interface TrailQuery {
  filter?: TrailFilter;
  before?: string;
  limit?: number;
}

function trailQs(opts?: TrailQuery): Record<string, string | number | undefined> {
  return { filter: opts?.filter, before: opts?.before, limit: opts?.limit };
}

function qs(params?: Record<string, string | number | undefined>): string {
  if (!params) return "";
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== "");
  if (!entries.length) return "";
  const usp = new URLSearchParams();
  for (const [k, v] of entries) usp.set(k, String(v));
  return `?${usp.toString()}`;
}

export const api = {
  // --- Who is here: the one owner of this install (no sign-in) ------------------------------------------

  me: () => get<Me>("/me"),

  // --- The board: every role in its column, and moving one ----------------------------------------------------

  /** Every column with its first cards; with a column key, that column whole; with a target role's id, only the roles
   * found for that role (counts included). */
  board: (column?: string, role?: string, limit?: number, reason?: string, page?: number) =>
    get<Board>(`/board${qs({ column, role, limit, reason, page })}`),

  /** Add a job by pasting its link: a posting (Landing Zone, or the Added links list until its page is read), put through the steps a scouted posting takes. A job
   * already on the pipeline answers with where it stands and `already`, and nothing is made. 422, in words, when the text is not a public web address. */
  /** Press Go: the server checks the day's Ready to Launch roles, builds their packets and opens each role in the owner's
   * browser as its packet is done. It runs in the background; this answers at once with where it stands. */
  applyGo: () => post<ApplyGo>("/apply/go"),
  /** Where Go stands now, or how its last run ended. */
  applyGoProgress: () => get<ApplyGo>("/apply/go"),
  /** Move a closed role back to where it was before it closed (the Closed review's button). */
  moveBack: (id: string) => post<MoveResult>(`/roles/${id}/move-back`),
  addRole: (url: string) => post<RoleAdded>("/roles", { url }),

  /** Move a role to another column (a status), with an optional note and, for an Ended column, why it ended (the app
   * works it out when left out). 409, in words, when it can't be. */
  moveRole: (id: string, to: string, note?: string, endedReason?: string) =>
    post<MoveResult>(`/roles/${id}/move`, { to, note: note ?? null, ended_reason: endedReason ?? null }),

  /** Every starred role, whatever its stage, as one column of cards, the latest starred first. */
  favorites: () => get<BoardColumn>("/favorites"),

  /** Star or unstar a role: a starred role is fast-tracked and holds a Ready to Launch place. Answers with its card,
   * the counts and what was done. */
  setFavorite: (id: string, favorite: boolean) => put<FavoriteResult>(`/roles/${id}/favorite`, { favorite }),

  /** Ask the analyst for a researched role's summary alone (a role opened without one); `asked` is false when there is
   * nothing to ask. */
  askSummary: (id: string) => post<{ asked: boolean }>(`/roles/${id}/summary`, {}),

  /** One search across every role in any status and the postings that are no role yet; every word must match. */
  search: (q: string, page?: number, size?: number) => get<SearchPage>(`/search${qs({ q: q.trim(), page, size })}`),

  // --- Packets: ask for one (or for changes), read a role's versions ---------------------------------------------------

  /** Ask for the next version of a role's packet; `feedback` says what to change from the one before. */
  askPacket: (roleId: string, feedback = "") => post<Packet>(`/roles/${roleId}/packet`, { feedback }),
  savePacketFiles: (packetId: string) => post<Packet>(`/packets/${packetId}/save-files`, {}),

  // --- Interviews: records on a role in Active Discussion, and the Coach's prep ----------------------------------------------

  /** Add an interview to a role in Active Discussion (409, in words, for any other stage). A time ahead starts its prep. */
  addInterview: (roleId: string, body: InterviewInput) => post<Interview>(`/roles/${roleId}/interviews`, body),
  editInterview: (id: string, body: InterviewInput) => patch<Interview>(`/interviews/${id}`, body),
  removeInterview: (id: string) => del<{ ok: boolean }>(`/interviews/${id}`),
  /** Make the next version of an interview's report (409 while one is being made, or once the time has passed). */
  prepareInterview: (id: string) => post<Interview>(`/interviews/${id}/prepare`),

  // --- Missions ---------------------------------------------------------------------------------------------------

  missions: () => get<{ missions: Mission[]; peak: PeakView }>("/missions"),

  /** Change the profile settings missions run with (the daily apply number, readers a night, peak hours). Each is a field
   * of the profile's Settings, so this is its next version, kept in the history with why. 422, in words, when a value
   * doesn't fit. `version` is null when nothing changed. */
  saveMissionNumbers: (values: Record<string, number | boolean | string | null>, why = "") =>
    patch<{ saved: boolean; version: number | null; peak: PeakView }>("/missions/numbers", { values, why }),

  mission: (id: string) => get<MissionDetail>(`/missions/${id}`),

  /** Run now, whatever the schedule and even when paused. */
  runMission: (id: string) => post<MissionRun>(`/missions/${id}/run`),

  pauseMission: (id: string, until: string | null, why = "") => post<MissionDetail>(`/missions/${id}/pause`, { until, why }),

  resumeMission: (id: string, why = "") => post<MissionDetail>(`/missions/${id}/resume`, { why }),

  /** Change the schedule or the settings; kept in the mission's history with why. */
  changeMission: (id: string, change: { schedule?: MissionSchedule; settings?: Record<string, unknown>; why?: string }) =>
    patch<MissionDetail>(`/missions/${id}`, change),

  /** Correct a required item's class, with why (docs/PRD.md 26.2): the score and the expectation follow, and the role's
   * gates run again. `said` is the line its history gained when that moved the role or changed its stop (39.14). */
  correctRequirement: (roleId: string, index: number, cls: string, why: string) =>
    post<{ said?: string }>(`/roles/${roleId}/requirements`, { index, class: cls, why }),

  // --- Where to look: the sites the search reads (only the ones switched on) ------------------------------------

  sites: () => get<WherePage>("/sites"),

  /** Switch a job system on or off; while it is off the night's read skips the employers that post on it. Answers the
   * whole page, since those employers' sites say so. */
  setJobSystem: (key: string, on: boolean) => patch<WherePage>(`/sites/job-systems/${encodeURIComponent(key)}`, { on }),

  /** Add a site by its address to a list: to search (the default; it starts switched on and starts learning in the
   * night's mission) or to avoid. Left to resumebot to test, unless said otherwise: the answer comes after the
   * server has tried the site once (up to 15 seconds). 409, in words, when it is already on the other list. The kind
   * is the server's to work out, so none is sent. */
  addSite: (url: string, opts: { list?: SiteList; readBy?: SiteReadBy } = {}) =>
    post<Site>("/sites", { url, list: opts.list ?? "search", read_by: opts.readBy ?? "auto" }),

  /** The roles found at one site that reached Found, newest first, `limit` from `offset`. */
  siteRoles: (id: string, offset = 0, limit = 10) =>
    get<SiteRolesPage>(`/sites/${encodeURIComponent(id)}/roles${qs({ offset, limit })}`),

  /** The same for a job system: the roles found at it that reached Found. */
  jobSystemRoles: (key: string, offset = 0, limit = 10) =>
    get<SiteRolesPage>(`/sites/job-systems/${encodeURIComponent(key)}/roles${qs({ offset, limit })}`),

  /** Switch a site on or off. */
  setSite: (id: string, on: boolean) => patch<Site>(`/sites/${encodeURIComponent(id)}`, { on }),

  /** Say how a site is read: left to resumebot to test (the answer comes after the server has tried it once), the
   * app, or the owner's own browser. */
  setSiteReadBy: (id: string, readBy: SiteReadBy) => patch<Site>(`/sites/${encodeURIComponent(id)}`, { read_by: readBy }),

  /** Take a site off the list to avoid. `said` is the line it says of what it changed: the log checked again (docs/PRD.md 39.14). */
  removeSite: (id: string) => del<{ ok: boolean; said?: string }>(`/sites/${encodeURIComponent(id)}`),

  // --- Employers --------------------------------------------------------------------------------------------------

  /** A page of the employers in a view, narrowed to a name and an industry, with each view's count. */
  employers: (query: EmployerQuery = {}) =>
    get<EmployerPage>(`/employers${qs({ view: query.view, q: query.q, industry: query.industry, cursor: query.cursor ?? undefined, limit: query.limit })}`),

  /** Switch watching every employer with a role in play on or off. On, every employer with a role in play is watched
   * now; off, the ones already watched stay watched until their roles leave play. */
  setWatchInPlay: (on: boolean) => put<{ on: boolean }>("/employers/watch-in-play", { on }),

  employer: (id: string) => get<Employer>(`/employers/${id}`),

  /** Set facts by hand; each becomes yours, which research leaves alone. */
  editEmployer: (id: string, changes: Partial<Employer>) => patch<Employer>(`/employers/${id}`, changes),

  /** What merging this employer into another would move, counted: nothing changes. */
  employerMergePreview: (id: string, into: string) =>
    get<EmployerMergePreview>(`/employers/${id}/merge${qs({ into })}`),

  /** Merge this employer into another, which stays: every role, posting, site and name moves over. Answers the one left. */
  mergeEmployer: (id: string, into: string) => post<EmployerMerged>(`/employers/${id}/merge`, { into }),

  /** What moving these roles to the employer of that name (a new one when none has it) would move, counted. */
  employerSplitPreview: (id: string, roles: string[], name: string) =>
    get<EmployerSplitPreview>(`/employers/${id}/split${qs({ roles: roles.join(","), name })}`),

  /** Split roles out of this employer onto the one of that name, made when none has it. Answers the one they went to. */
  splitEmployer: (id: string, roles: string[], name: string) =>
    post<EmployerSplit>(`/employers/${id}/split`, { roles, name }),

  /** One role whole: its facts, its history with the reasoning behind each event, and its packets. */
  role: (roleId: string) => get<RoleDetail>(`/roles/${roleId}`),

  /** One role's row and nothing else (the top of `role`): what a column shows, the server's note included. */
  roleRow: (roleId: string) => get<BoardCard>(`/roles/${roleId}/row`),

  // --- The profile: what to look for, the gates, scoring and settings ------------------------------------------

  profile: () => get<Profile>("/profile"),

  /** The pay method worked on a made-up posting (a range and a share of the job shown) with these numbers, saved or not:
   * the server's own `compute`, nothing saved read, nothing written. A blank step (`comp_round_to` null) is left out, so
   * it is the proportional rounding. 422, in words, when a number doesn't fit. */
  compExample: (n: Omit<CompMethod, "comp_basis" | "comp_assumptions" | "comp_range_width">) =>
    get<CompExample>(`/profile/comp-example${qs({
      comp_tiers: n.comp_tiers, comp_transferable_weight: n.comp_transferable_weight, comp_stance_clamp: n.comp_stance_clamp,
      comp_stance_min_siblings: n.comp_stance_min_siblings, comp_likely_low: n.comp_likely_low, comp_likely_high: n.comp_likely_high,
      comp_round_to: n.comp_round_to ?? undefined,
    })}`),

  /** Save one part as its next version, with why (shown in its history). `baseVersion` is the version the page loaded: the
   * server refuses with 409 (in words) when a newer one is in force, so an old copy never writes over newer work. 422, in
   * words, when it doesn't fit. */
  saveProfile: (part: ProfilePart | DocumentPart, data: unknown, why: string, baseVersion: number) =>
    put<ProfileSaved>(`/profile/${part}`, {
      data,
      why,
      base_version: baseVersion,
    }),

  /** Say why the newest version of a part was saved, after it was (the Saved line's "Add a reason"). 422, in words, for
   * a version that is not the newest or that someone else saved. */
  profileWhy: (part: ProfilePart, version: number, why: string) =>
    put<Omit<ProfileVersion, "changed">>(`/profile/${part}/versions/${version}/why`, { why }),

  /** How many each line of Hard pass has stopped (roles Gated out by it and postings it stopped), by the line's id. */
  gatesStopped: () => get<{ stopped: Record<string, number> }>("/profile/gates/stopped"),

  /** Every version of one part, newest first, with who saved it, why and what changed. */
  profileHistory: (part: ProfilePart | DocumentPart) =>
    get<{ kind: string; versions: ProfileVersion[] }>(`/profile/${part}/history`),

  /** Whether an agent can take work now (not in peak hours, not busy), and when not, why in words. */
  agentReady: () => get<AgentReady>("/agent/ready"),

  /** Start reading a pasted job posting for one target role (leave the role out for one not saved yet). Answers with
   * the job to ask after. 409, in words, when no agent is free. */
  parseExample: (text: string, roleId?: string) =>
    post<{ id: string }>("/profile/parse", roleId ? { text, role_id: roleId } : { text }),

  /** How reading a posting (or a resume) is going: waiting, reading, done (with the fields it found) or failed (with why). */
  parseJob: <F = Partial<ParsedRole>>(id: string) => get<ParseJob<F>>(`/profile/parse/${encodeURIComponent(id)}`),

  /** Start dividing a pasted resume into the master resume's fields. Answers with the job to ask after (`parseJob`). 409,
   * in words, when no agent is free. */
  parseResume: (text: string) => post<{ id: string }>("/profile/parse/resume", { text }),

  /** Start dividing a resume file (.docx, .pdf or .txt): the file's own bytes are sent, and the server reads its text.
   * Answers with the job to ask after. 422, in words, when the file can't be read or has no text; 409 when no agent is free. */
  parseResumeFile: (file: File) =>
    request<{ id: string }>(`/profile/parse/resume/file?name=${encodeURIComponent(file.name)}`, {
      method: "POST",
      body: file,
      headers: { "Content-Type": "application/octet-stream" },
    }),

  /** How many roles reached Found for one target role on each of the last `days` days (1 to 90), oldest first. */
  roleTrend: (roleId: string, days = 30) =>
    get<RoleTrend>(`/profile/roles/${encodeURIComponent(roleId)}/trend${qs({ days })}`),

  /** What the role tabs miss: the roles the owner pursued with no target role and the postings found this week that fit
   * none, each a count and the newest `limit` (from the given offsets). */
  fitsNone: (opts: { limit?: number; pursuedOffset?: number; thisWeekOffset?: number } = {}) =>
    get<FitsNone>(
      `/profile/fits-none${qs({ limit: opts.limit, pursued_offset: opts.pursuedOffset || undefined, this_week_offset: opts.thisWeekOffset || undefined })}`,
    ),

  /** Call a role (or a posting from this week's list) fine as it is: it leaves what fits none. Answers with the new counts. */
  dismissFitsNone: (id: string) =>
    post<{ id: string; kind: string; dismissed: boolean; pursued: number; this_week: number }>(
      `/profile/fits-none/${encodeURIComponent(id)}/dismiss`,
    ),

  /** A target role drafted from a role or posting (its name, an example title, a description from its text). Not saved:
   * the page adds it as a new tab. */
  roleFromRole: (id: string) => post<TargetRole>(`/profile/roles/from-role/${encodeURIComponent(id)}`),

  /** Ask for a check of the role tabs (a dry run of the title check): of the pursued roles, of a sample of stopped
   * postings, or of one pasted posting (`text`). Answers with the job to ask after. 409, in words, when no agent is free. */
  checkRoles: (scope: CheckScope, text?: string) =>
    post<{ id: string }>("/profile/check", text === undefined ? { scope } : { scope, text }),

  /** How a check is going: waiting, working (with how far along), done (with its result) or failed (with why). */
  checkJob: (id: string) => get<CheckJob>(`/profile/check/${encodeURIComponent(id)}`),

  /** The resume, application info and writing rules, with how many versions each has. Saved with saveProfile. */
  documents: () => get<Documents>("/documents"),

  /** The checks that ship with the app and run on every draft, and the style rules that ship with it (read-only: which style rules are off is in the writing document). */
  ruleChecks: () => get<{ checks: RuleCheck[]; style: StyleRule[] }>("/documents/built-in-checks"),

  /** The story library built from the master resume (read-only). */
  storyLibrary: () => get<StoryLibrary>("/documents/story-library"),

  // --- Find Gaps (My Resume): what the postings for a role ask that the master resume does not yet show ----------------

  /** A row for each target role (the researched roles it rests on, the shares of the analyst's grades, its latest run), and
   * the run going now. */
  findGaps: () => get<GapDashboard>("/find-gaps"),

  /** The roles the picker lists (38.11), by company and title: read when the picker is first opened, not with the dashboard. */
  gapJobs: () => get<GapJobOption[]>("/find-gaps/jobs"),

  /** Start a run for a target role, for a title that is not one (an agent looks on the public web), or from a role in the
   * app (its own posting and similar ones). Answers with the run to ask after (`gapRun`). 409, in words, when no agent is
   * free or a run is going; 422, in words, when fewer than five employers have a stored posting for the role, or the role
   * holds no posting text. */
  startGaps: (target: { role_id: string } | { title: string } | { opportunity_id: string }) =>
    post<{ id: string }>("/find-gaps/runs", target),

  /** Where a run is (waiting, how many postings it has read), and, once done, its themes. 404 when it is gone. */
  gapRun: (id: string) => get<GapRunView>(`/find-gaps/runs/${encodeURIComponent(id)}`),

  /** Grade the same postings again against the master as it is now. Answers with the new run (`gapRun`). */
  checkGapsAgain: (id: string) => post<{ id: string }>(`/find-gaps/runs/${encodeURIComponent(id)}/check-again`),

  /** Draw a fresh sample of stored postings for the run's role. Answers with the new run (`gapRun`). */
  newGapSample: (id: string) => post<{ id: string }>(`/find-gaps/runs/${encodeURIComponent(id)}/new-sample`),

  /** "I haven't" (`not_done`) or "Not relevant" (`not_relevant`: hidden for the role, on later runs too). Touches nothing
   * outside Find Gaps. */
  answerGapTheme: (runId: string, themeId: string, answer: "not_done" | "not_relevant") =>
    put<{ id: string; answer: string }>(
      `/find-gaps/runs/${encodeURIComponent(runId)}/themes/${encodeURIComponent(themeId)}/answer`,
      { answer },
    ),

  /** Take an answer back: a theme marked a real gap is unmarked, a hidden theme is brought back. */
  takeBackGapAnswer: (answerId: string) => del<{ cleared: boolean }>(`/find-gaps/answers/${encodeURIComponent(answerId)}`),

  /** Ask for a line drafted from the owner's own words about a theme. Answers with the draft to ask after (`gapDraft`). */
  draftGapLine: (runId: string, themeId: string, words: string) =>
    post<{ id: string }>(
      `/find-gaps/runs/${encodeURIComponent(runId)}/themes/${encodeURIComponent(themeId)}/draft`,
      { words },
    ),

  /** How a drafted line is going: waiting, drafting, done (the line) or failed (why). */
  gapDraft: (id: string) => get<GapDraft>(`/find-gaps/drafts/${encodeURIComponent(id)}`),

  /** Ask the Coach for questions about the gaps of a finished run started from a job. The run's own view says how it goes
   * (`gapRun`). 409, in words, when no agent is free or a job is going; 422 when there is nothing left to ask. */
  askGapQuestions: (runId: string) => post<{ id: string }>(`/find-gaps/runs/${encodeURIComponent(runId)}/questions`),

  /** Ask the Coach to propose master lines from the owner's answers that no suggestion has been made from yet. */
  suggestGapLines: (runId: string) => post<{ id: string }>(`/find-gaps/runs/${encodeURIComponent(runId)}/proposals`),

  /** The owner's answer to one of the Coach's questions: their own words (`answer`), `skip`, or `not_true` (the theme is
   * never asked again for the role). Touches nothing outside Find Gaps. */
  answerGapCard: (cardId: string, action: "answer" | "skip" | "not_true", text = "") =>
    put<{ id: string; state: string }>(`/find-gaps/cards/${encodeURIComponent(cardId)}`, { action, text }),

  /** Accept a proposal: its line is saved into its job as the next version of the master (with the owner's own edit of the
   * words, when they sent one). Answers with that version and the master as it now is. */
  acceptGapProposal: (id: string, text?: string, baseVersion?: number) =>
    post<{ version: number; resume: Resume }>(`/find-gaps/proposals/${encodeURIComponent(id)}/accept`, {
      ...(text === undefined ? {} : { text }),
      ...(baseVersion === undefined ? {} : { base_version: baseVersion }),
    }),

  /** What the owner's own Claude suggested for the master resume that waits for the owner (docs/PRD.md 50.15), and the
   * master version in force. A suggestion is decided with `acceptGapProposal` (naming the version the page loaded) or
   * `rejectGapProposal`. */
  openGapProposals: () => get<{ version: number; proposals: GapProposal[] }>("/find-gaps/proposals/open"),

  /** Reject a proposal: it is kept as rejected, the master is left as it was and the same words are not proposed again. */
  rejectGapProposal: (id: string) => post<{ id: string; status: string }>(`/find-gaps/proposals/${encodeURIComponent(id)}/reject`),

  // --- The Inbox: what needs the owner, what the app has to say, what was decided --------------------------

  /** What needs the owner (at most three), what waits behind them, the notifications and what is on track. */
  inbox: () => get<Inbox>("/inbox"),

  /** Past decisions, newest first; pass the last answer's `next_before` for the page after it. */
  inboxHistory: (opts: { before?: string; limit?: number } = {}) =>
    get<DecisionHistory>(`/inbox/history${qs({ before: opts.before, limit: opts.limit })}`),

  /** Mark one notification read. */
  readNotification: (id: string) => post<{ changed: number }>(`/notifications/${id}/read`),

  readAllNotifications: () => post<{ changed: number }>("/notifications/read-all"),

  // --- The tasks ------------------------------------------------------------------------------------------

  /** The task list: for the owner, for the agents, and finished lately. */
  tasks: () => get<TaskList>("/tasks"),

  /** Done is done: the owner's word is taken. 409 (in words) when it was already finished. */
  taskDone: (id: string, note?: string) => post<Task>(`/tasks/${id}/done`, { note: note ?? null }),

  taskSkip: (id: string, reason?: string) => post<Task>(`/tasks/${id}/skip`, { reason: reason ?? null }),

  /** Answer a decision with one of its options' ids, and the owner's own words when the option asked for them. A
   * failed search's "Search again" starts a search. */
  taskAnswer: (id: string, option: string, note?: string) =>
    post<Task>(`/tasks/${id}/answer`, note ? { option, note } : { option }),

  /** Put a task off until a day ("2026-10-08"), or bring it back with null. */
  taskSnooze: (id: string, until: string | null) => post<Task>(`/tasks/${id}/snooze`, { until }),

  // --- Agents ----------------------------------------------------------------------------------------------

  listAgents: () => get<AgentSummary[]>("/agents"),

  getAgent: (connectionId: string) => get<AgentDetail>(`/agents/${connectionId}`),

  listAgentKinds: () => get<AgentKind[]>("/agent-kinds"),

  /** The last seven nights' Claude cost, newest first. */
  listCostNights: () => get<CostNight[]>("/cost-nights"),

  /** `what` is run, pause or resume; for one kind, or every kind with "all". */
  agentKindAction: (kind: string | "all", what: "pause" | "resume") =>
    post<AgentKind[]>(kind === "all" ? `/agent-kinds/${what}-all` : `/agent-kinds/${encodeURIComponent(kind)}/${what}`),

  /** Run now for one kind: `held` says why nothing was queued. */
  runAgentKind: (kind: string) => post<AgentRunResult>(`/agent-kinds/${encodeURIComponent(kind)}/run`),

  /** How many sessions may work at once, and how many lanes there are. */
  agentSessions: () => get<AgentSessions>("/agent-kinds/sessions"),

  setAgentSessions: (change: { count?: number; budget?: number | null }) =>
    put<AgentSessions>("/agent-kinds/sessions", change),

  runAllAgentKinds: () => post<AgentRunAllResult>("/agent-kinds/run-all"),

  // --- The mail login (Agent Settings) --------------------------------------------------------------------------

  mailLogin: () => get<MailLogin>("/mail-login"),

  /** Keep the login. The password goes in and is never returned; `host` and `port` only for a provider the app does not know. */
  saveMailLogin: (body: { username: string; password: string; host?: string; port?: number }) => put<MailLogin>("/mail-login", body),

  /** Log in read-only and list the folders; a refused login is an answer (`ok` false), not an error. */
  testMailLogin: () => post<MailLoginTest>("/mail-login/test"),

  removeMailLogin: () => del<MailLogin>("/mail-login"),

  /** Pause or resume an agent: a paused agent is handed no work. */
  editAgent: (connectionId: string, body: EditAgentIn) => patch<AgentDetail>(`/agents/${connectionId}`, body),

  // --- The trail: what happened, newest first ---------------------------------------------------------------

  agentTrail: (connectionId: string, opts?: TrailQuery) =>
    get<TrailPage>(`/agents/${connectionId}/trail${qs(trailQs(opts))}`),

  // --- How they work ---------------------------------------------------------------------------------------

  /** Every process, with the files an agent is sent at each step. */
  harness: () => get<HarnessOverview>("/harness"),

  /** The most recent real task of a step's kind, as it would be sent. */
  harnessExample: (stepId: string) =>
    get<{ step_id: string; example: HarnessExample | null }>(`/harness/steps/${encodeURIComponent(stepId)}/example`),

  /** What was sent for one search run: its step and its own payload. */
  harnessTask: (taskId: string) => get<HarnessTaskSent>(`/harness/tasks/${taskId}`),

  /** A file as in force and its history (number, when, who, why), newest first. */
  harnessFile: (kind: string, name: string) =>
    get<HarnessFileReply>(`/harness/files/${kind}/${encodeURIComponent(name)}`),

  /** One version of a file, with its text. */
  harnessFileVersion: (kind: string, name: string, version: number) =>
    get<HarnessFileText>(`/harness/files/${kind}/${encodeURIComponent(name)}/versions/${version}`),

  /** Save an edit of a file as its next version, with the owner's one-line why. */
  saveHarnessFile: (kind: string, name: string, body: { text: string; why: string }) =>
    put<HarnessFileReply>(`/harness/files/${kind}/${encodeURIComponent(name)}`, body),

  /** Bring an earlier version back: it is saved as the next version. */
  restoreHarnessFile: (kind: string, name: string, version: number) =>
    post<HarnessFileReply>(`/harness/files/${kind}/${encodeURIComponent(name)}/restore`, { version }),

  /** One run: its current step, the steps it passed and what was sent at each agent step it reached. */
  processRun: (runId: string) => get<ProcessRunDetail>(`/harness/runs/${runId}`),

  // --- Changing how it works (docs/PRD.md path 16): no approval step, a save is the change ----------------

  /** The BPMN file of a process as it runs now, with its version: what the editor starts from. */
  processBpmn: (key: string) => get<ProcessBpmn>(`/harness/processes/${key}/bpmn`),

  /** Asks the engine whether an edited file is one it can run. Nothing is kept. */
  validateProcessEdit: (key: string, xml: string) =>
    post<ProcessEditCheck>(`/harness/processes/${key}/validate`, { xml }),

  /** Saves the edit as the process's next version. `basedOn` is the version the edit was made on; the server answers
   *  409 when the process has moved on since. */
  saveProcessEdit: (key: string, xml: string, why: string, basedOn?: number) =>
    post<ProcessHistory>(`/harness/processes/${key}/edits`, { xml, why, based_on: basedOn }),

  /** The versions of a process, newest first. */
  processVersions: (key: string) => get<ProcessHistory>(`/harness/processes/${key}/versions`),

  /** One earlier version's file, to look at. */
  processVersionFile: (key: string, number: number) =>
    get<ProcessVersionFile>(`/harness/processes/${key}/versions/${number}`),

  /** Restore an earlier version: saved as a new version. */
  restoreProcess: (key: string, to: number, why?: string) =>
    post<ProcessHistory>(`/harness/processes/${key}/rollback`, { to, why }),
};

/** How many sessions may work at once, and the nightly dollar budget (at API prices) with the line the last night left. */
export interface AgentSessions {
  count: number;
  max: number;
  /** null: no limit */
  budget?: number | null;
  budget_line?: string | null;
}
