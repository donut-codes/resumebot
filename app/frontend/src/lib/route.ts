// The whole navigation state lives in the URL hash, so a refresh keeps your place and Back goes back. This file is
// the one place that reads and writes it.
//
//   #/board                                        the front page, the board: every role in its column (an empty or
//                                                  unknown address is this too, so is an address that no longer exists)
//   #/board/<role id>                              the board with one role open beside it
//   #/board?role=<target role id>                  the board of only the roles found for that target role (also with
//                                                  a role open: #/board/<role id>?role=<target role id>)
//   #/board?role=<id>&stage=gated_out              the board open on that stage (also without a role: #/board?stage=applied)
//   #/employers  #/employers/<employer id>         the employers, and one open beside the list
//   #/missions  #/missions/<mission id>           the standing work, and one mission open beside the list: the Missions
//                                                  tab of Agent Settings (there is no Missions page of its own)
//   #/inbox  #/inbox/notifications  #/inbox/activity  #/inbox/decisions   Notifications (the route keeps its old name):
//                                                  what needs you, what to know, what the agents are doing and did,
//                                                  what you decided
//   #/profile  #/profile/<part>                    Search Settings (the route keeps its old name): where to look (the default),
//                                                  criteria (What to look for), gates (Hard pass), scoring
//                                                  (Prioritization), about
//   #/profile/criteria?role=<target role id>       What to look for with that target role's tab open
//   #/me  #/me/<part>                              My Resume: the master resume (the default; its facts forms ask for are its first part),
//                                                  resume-rules, letter-rules, document-rules (the writing rules, in three)
//   #/me?import=<parse job id>                     My Resume with Import your resume open on a reading that was started for the
//                                                  owner (by their Claude, through the owner door): its steps, then the review
//   #/agents  #/agents/<id>  #/agents/<id>/<tab>   Agent Settings: the agents, and one agent's tab
//   #/agents/how  #/agents/how/<process>  #/agents/how/<process>/<sub-process>   How they work
//   #/agents/how?sent=<run id>                     How they work, opened on what was sent for that search
//   #/agents/how?live=<instance id>                How they work, opened on a running process
//
// A screen that needs more of the address (a task's page) puts its own fields on `Route`, its own path in
// `parseRoute` and `serializeRoute`, and its own tests in route.test.ts.

import type { ProfilePart } from "../api/types/profile";
import { STAGES, type StageKey } from "./stages";

export type { ProfilePart };
export type Tab = "board" | "employers" | "missions" | "profile" | "me" | "inbox" | "agents";
/** Notifications' four parts, as in the segmented control: what needs you, the notifications, what the agents are
 * doing and did, past decisions. */
export type InboxSection = "needs" | "notifications" | "activity" | "decisions";
export type AgentTabKey = "activity" | "settings";

export interface Route {
  tab: Tab;
  /** The role open on the board (#/board/<id>), or null for the board itself. */
  roleId: string | null;
  /** The target role the board is narrowed to (#/board?role=<id>), or null for every role. */
  targetRole: string | null;
  /** The stage the board opens on (#/board?stage=gated_out), or null for the one it showed last. */
  boardStage: StageKey | null;
  /** The employer open beside the list (#/employers/<id>), or null. */
  employerId: string | null;
  /** The mission open beside the list (#/missions/<id>), or null. */
  missionId: string | null;
  /** The profile's part open (#/profile/gates). Always "where", the first, on any other tab. */
  profilePart: ProfileTab;
  /** The target role whose tab What to look for opens on (#/profile/criteria?role=<id>), or null for the first. Only on that part. */
  profileRole: string | null;
  /** The Me page open (#/me/letter-rules). Always "resume" on any other tab. */
  mePart: MePage;
  /** The resume reading Import your resume opens on (#/me?import=<parse job id>): one started for the owner, not from this
   * page. Only on the master resume. */
  importJob: string | null;
  /** The Inbox's part open (#/inbox/decisions). Always "needs" on any other tab. */
  inboxSection: InboxSection;
  agentId: string | null;
  agentTab: AgentTabKey | null;
  /** The Agents page's "How they work" tab (#/agents/how). */
  agentsHow: boolean;
  /** How they work, opened on what was sent for this search run (#/agents/how?sent=<run id>). */
  sentTaskId: string | null;
  /** How they work, opened on a running process: where that run is and the steps it passed (#/agents/how?live=<id>). */
  liveRunId: string | null;
  /** How they work: the process tab open, top-level process first, then the sub-process under it, and so on
   * (#/agents/how/jobsearch_round/jobsearch_screen_role). Empty is the page's first process. */
  howPath: string[];
}

const AGENT_TABS: AgentTabKey[] = ["activity", "settings"];
const STAGE_KEYS: StageKey[] = STAGES.map((s) => s.key);
const INBOX_SECTIONS: InboxSection[] = ["needs", "notifications", "activity", "decisions"];
/** The Profile's tabs, in order: Where to look (the sites the search reads), What to look for, Hard pass (`gates`),
 * Prioritization (`scoring`), Also sent to agents (`also`) and About. The route ids are the documents' names and stay as they were. The Settings document
 * is stored as before but has no tab: its numbers moved to the Missions tab (docs/PRD.md 30.7). */
export type ProfileTab = Exclude<ProfilePart, "settings"> | "where" | "also" | "about";
export const PROFILE_TABS: ProfileTab[] = ["where", "criteria", "gates", "scoring", "also", "about"];
/** My Resume's pages, in order: the master resume (with the facts forms ask for), Find Gaps, then the writing rules in three: Resume
 * Rules, Cover Letter Rules and Document Creation Rules. These are pages of the screen, not the stored documents
 * (`DocumentPart`): the three rules pages are all the one writing document. */
export type WritingPage = "resume-rules" | "letter-rules" | "document-rules";
export type MePage = "resume" | "find-gaps" | "story-library" | WritingPage;
export const ME_PAGES: MePage[] = ["resume", "find-gaps", "story-library", "resume-rules", "letter-rules", "document-rules"];

const TAB_PATH: Record<Tab, string> = {
  board: "board",
  employers: "employers",
  missions: "missions",
  profile: "profile",
  me: "me",
  inbox: "inbox",
  agents: "agents",
};

/** A route with nothing open on the given tab: that tab's front page. */
export function blankRoute(tab: Tab = "board"): Route {
  return { tab, roleId: null, targetRole: null, boardStage: null, employerId: null, missionId: null, profilePart: "where", profileRole: null, mePart: "resume", importJob: null, inboxSection: "needs", agentId: null, agentTab: null, agentsHow: false, sentTaskId: null, liveRunId: null, howPath: [] };
}

/** A tab's route with some of it filled in. */
export function routeFor(tab: Tab, patch: Partial<Route> = {}): Route {
  return { ...blankRoute(tab), ...patch, tab };
}

function decode(s: string): string | null {
  try {
    const v = decodeURIComponent(s);
    return v || null;
  } catch {
    return null;
  }
}

function member<T extends string>(list: readonly T[], v: string | undefined): T | null {
  return v && (list as readonly string[]).includes(v) ? (v as T) : null;
}

/** Reads a hash ("#/agents/how?live=x", with or without the "#") into a route. Anything unknown is the board. */
export function parseRoute(hash: string): Route {
  let h = hash.startsWith("#") ? hash.slice(1) : hash;
  let query = "";
  const q = h.indexOf("?");
  if (q >= 0) {
    query = h.slice(q + 1);
    h = h.slice(0, q);
  }
  const segs = h.split("/").filter((s) => s !== "");
  const params = new URLSearchParams(query);
  const [head, a, b] = segs;
  let r: Route;
  switch (head) {
    case "inbox":
      r = routeFor("inbox", { inboxSection: member(INBOX_SECTIONS, a) ?? "needs" });
      break;
    case "profile": {
      const profilePart = member(PROFILE_TABS, a) ?? "where";
      r = routeFor("profile", { profilePart, profileRole: profilePart === "criteria" ? decode(params.get("role") ?? "") : null });
      break;
    }
    case "me": {
      const mePart = member(ME_PAGES, a) ?? "resume";
      r = routeFor("me", { mePart, importJob: mePart === "resume" ? decode(params.get("import") ?? "") : null });
      break;
    }
    case "agents":
      r =
        a === "how"
          ? routeFor("agents", { agentsHow: true, howPath: segs.slice(2).map(decode).filter((s): s is string => !!s) })
          : routeFor("agents", { agentId: a ? decode(a) : null, agentTab: member(AGENT_TABS, b) });
      break;
    case "missions":
      r = routeFor("missions", { missionId: a ? decode(a) : null });
      break;
    case "employers":
      r = routeFor("employers", { employerId: a ? decode(a) : null });
      break;
    case "board":
      r = routeFor("board", {
        roleId: a ? decode(a) : null,
        targetRole: decode(params.get("role") ?? ""),
        boardStage: member(STAGE_KEYS, params.get("stage") ?? undefined),
      });
      break;
    default:
      // The board is the front page: an empty address and anything unknown, an address that no longer exists included, land on it.
      r = blankRoute("board");
  }
  const sent = params.get("sent");
  const live = params.get("live");
  if (sent && r.agentsHow) r.sentTaskId = sent;
  if (live && r.agentsHow) r.liveRunId = live;
  return r;
}

const enc = encodeURIComponent;

/** Writes a route as a hash, always with the leading "#/". Round-trips with parseRoute. */
export function serializeRoute(r: Route): string {
  let path = `#/${TAB_PATH[r.tab]}`;
  if (r.tab === "inbox" && r.inboxSection !== "needs") path += `/${r.inboxSection}`;
  if (r.tab === "profile" && r.profilePart !== "where") path += `/${r.profilePart}`;
  if (r.tab === "me" && r.mePart !== "resume") path += `/${r.mePart}`;
  if (r.tab === "board" && r.roleId) path += `/${encodeURIComponent(r.roleId)}`;
  if (r.tab === "employers" && r.employerId) path += `/${enc(r.employerId)}`;
  if (r.tab === "missions" && r.missionId) path += `/${enc(r.missionId)}`;
  if (r.tab === "agents") {
    if (r.agentsHow) path += "/how" + r.howPath.map((s) => `/${enc(s)}`).join("");
    else if (r.agentId) {
      path += `/${enc(r.agentId)}`;
      if (r.agentTab) path += `/${r.agentTab}`;
    }
  }
  const q: string[] = [];
  if (r.tab === "board" && r.targetRole) q.push(`role=${enc(r.targetRole)}`);
  if (r.tab === "board" && r.boardStage) q.push(`stage=${enc(r.boardStage)}`);
  if (r.tab === "profile" && r.profilePart === "criteria" && r.profileRole) q.push(`role=${enc(r.profileRole)}`);
  if (r.tab === "me" && r.mePart === "resume" && r.importJob) q.push(`import=${enc(r.importJob)}`);
  if (r.agentsHow && r.sentTaskId) q.push(`sent=${enc(r.sentTaskId)}`);
  if (r.agentsHow && r.liveRunId) q.push(`live=${enc(r.liveRunId)}`);
  return q.length ? `${path}?${q.join("&")}` : path;
}

/** The address of one role open on the board (#/board/<id>): where a "See the role" link goes. */
export function roleAddress(roleId: string): string {
  return serializeRoute(routeFor("board", { roleId }));
}

/** The address of the board: where every link to the Pipeline goes. */
export const BOARD_HREF = serializeRoute(blankRoute("board"));

/** Opens the board, from a link that sits where the shell's own navigation is not in reach (a trail's "See where it is"). */
export function openPipeline(): void {
  window.location.hash = BOARD_HREF;
}

/** Opens one role on the board ("See the role"). */
export function openRole(roleId: string): void {
  window.location.hash = roleAddress(roleId);
}
