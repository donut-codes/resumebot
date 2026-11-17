// The words on the Agents page. Roles are data (the backend sends `roles_words`, "Scout" today), so a new role
// (a Writer, a Reviewer) needs no change here.

import type { AgentSummary } from "../../api/types/agents";
import { dateInWords, localDay, ownerToday, secondsAgoInWords, timeAgoInWords } from "../../lib/dates";
import { formatTime } from "../../lib/format";

/** The roles in words, as the server says them ("Scout"), else "Agent" for one that holds none. */
export function rolesWords(agent: Pick<AgentSummary, "roles_words">): string {
  return agent.roles_words || "Agent";
}

/** The three robots, in the order the work goes (the scout finds, the analyst researches, the writer drafts). */
type RibbonRole = "scout" | "analyst" | "writer";
const RIBBON_ORDER: RibbonRole[] = ["scout", "analyst", "writer"];

/** The robot whose ribbon an agent wears: the role its work is for right now, else the first role it holds. An agent
 * with none of the three has no ribbon. */
export function ribbonRole(agent: Pick<AgentSummary, "roles" | "current" | "paused" | "status">): RibbonRole | undefined {
  const held = RIBBON_ORDER.filter((r) => agent.roles.includes(r));
  if (held.length === 0) return undefined;
  const now = agent.current?.role;
  const working = !agent.paused && agent.status === "working";
  const lit = working ? held.find((r) => r === now) : undefined;
  return lit ?? held[0];
}

/** The ink a state is told in: working green, paused saffron, needs a look oxblood. No ink is a plain asleep. */
type StandingInk = "green" | "saffron" | "oxblood";

/** The one word that says where an agent stands, and the ink it wears. A paused agent is waiting; one that has gone
 * quiet or never checked in needs a look; one on a task is working; the rest are asleep. */
export function standing(agent: AgentSummary): { word: string; ink?: StandingInk } {
  if (agent.paused) return { word: "Paused", ink: "saffron" };
  if (agent.session_state === "not_responding") return { word: "Not responding", ink: "oxblood" };
  if (agent.status === "offline") return { word: agent.availability, ink: "oxblood" };
  if (agent.status === "working") return { word: agent.availability, ink: "green" };
  return { word: agent.availability };
}

/** The band over what an agent is doing now: green when it works, saffron when paused, oxblood when it is stuck or has
 * gone quiet, graphite (no job) when it simply waits. */
export function nowInk(agent: AgentSummary): StandingInk | "graphite" {
  if (agent.current?.stuck_since) return "oxblood";
  return standing(agent).ink ?? "graphite";
}

/** When it last called the app, for a roster column: "6 minutes ago", or "Never". */
export function lastSeenWords(agent: AgentSummary): string {
  return agent.last_seen_at ? timeAgoInWords(agent.last_seen_at) : "Never";
}

/** How many of its slots are busy, for a roster column: "1 of 2". */
export function slotsWords(agent: AgentSummary): string {
  const c = agent.capacity;
  return c.slots == null ? String(c.busy) : `${c.busy} of ${c.slots}`;
}

/** The time of day it last checked in, in words: "9:40 AM" today, else the day. */
function sinceWords(iso: string): string {
  if (localDay(iso) === ownerToday()) {
    return formatTime(new Date(iso));
  }
  return dateInWords(iso);
}

/** The line for a task held with no sign of life: "Stuck since 4:26 PM" (today), else the day. */
export function stuckWords(stuckSince: string): string {
  return `Stuck since ${sinceWords(stuckSince)}`;
}

/** The one status line under an agent's name. Only "hasn't checked in" is a problem, so only it is marked. */
export function statusLine(agent: AgentSummary): { text: string; problem: boolean } {
  if (agent.session_state === "not_responding" && !agent.paused) return { text: "Not responding", problem: true };
  if (agent.status === "offline" || !agent.last_seen_at) {
    return {
      text: agent.last_seen_at ? `Hasn't checked in since ${sinceWords(agent.last_seen_at)}` : "Hasn't checked in yet",
      problem: true,
    };
  }
  if (agent.session_state === "live" && agent.checked_in_at) {
    return {
      text: `${agent.status === "working" ? "Working · checked in" : "Checked in"} ${secondsAgoInWords(agent.checked_in_at)}`,
      problem: false,
    };
  }
  return { text: `Checked in ${timeAgoInWords(agent.last_seen_at)}`, problem: false };
}
