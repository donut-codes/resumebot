// What Notifications shows of the store's tasks, notifications and past decisions, as rows: pure, so the screen only
// draws. A task's `kind` is free words; nothing here branches on it beyond saying it in sentence case.

import type { DecisionRecord, Notification, Task, TaskLink, TaskOption } from "../../api/types/inbox";
import { byNewest, dateInWords, dateWithWeekday, daysBetween, dueInWords, wasDueInWords } from "../../lib/dates";
import { blankRoute, BOARD_HREF, roleAddress, serializeRoute, type InboxSection } from "../../lib/route";
import { humanize } from "../../lib/text";
import { asksToday } from "../../state/inboxStore";

export const PARTS: { key: InboxSection; label: string }[] = [
  { key: "needs", label: "Needs you" },
  { key: "notifications", label: "Notifications" },
  { key: "activity", label: "What agents did" },
  { key: "decisions", label: "Past decisions" },
];

export type ItemKind = "task" | "note" | "agent" | "past";

export interface Item {
  id: string;
  kind: ItemKind;
  title: string;
  meta: string[];
  /** When it happened, for the row's end ("2h"). */
  at: string;
  /** Read already, or put off: its title is quiet. */
  quiet: boolean;
  task?: Task;
  note?: Notification;
  record?: DecisionRecord;
}

/** How long ago, in the fewest letters: "now", "25m", "2h", "3d", then the date. */
export function shortAge(iso: string, now: Date = new Date()): string {
  const seconds = Math.max(0, (now.getTime() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.round(hours / 24);
  if (days <= 14) return `${days}d`;
  return dateInWords(iso);
}

/** A due date in plain words, only when there is one: "Due tomorrow, Oct 8", "Was due Oct 2". */
export function dueWords(due: string | null, today: string | null): string | null {
  if (!due || !today) return null;
  return daysBetween(today, due) < 0 ? wasDueInWords(due, today) : `Due ${dueInWords(due, today)}`;
}

/** "Put off until Fri, Oct 25" for a task put off past today. */
export function putOffWords(task: Task, today: string | null): string | null {
  if (!task.snoozed_until || !today || daysBetween(today, task.snoozed_until) <= 0) return null;
  return `Put off until ${dateWithWeekday(task.snoozed_until)}`;
}

/** The options, the suggested one first. */
export function ordered(options: TaskOption[], recommended: string | null): TaskOption[] {
  return [...options.filter((o) => o.id === recommended), ...options.filter((o) => o.id !== recommended)];
}

/** Where a link goes and what it says: a role opens that role, a search or several roles open the Pipeline, a site
 * opens Search Settings; any other kind opens nothing. */
export function linkTo(link: TaskLink | null, taskKind?: string): { href: string; words: string } | null {
  if (!link) return null;
  if (link.kind === "site") return { href: serializeRoute(blankRoute("profile")), words: "See Where to look" };
  if (link.kind === "search_run") return { href: BOARD_HREF, words: taskKind === "review_roles" ? "See the roles" : "See the search" };
  if (link.kind === "opportunity") {
    return taskKind === "review_roles" ? { href: BOARD_HREF, words: "See the roles" } : { href: roleAddress(link.id), words: "See the role" };
  }
  return null;
}

/** What became of a finished task, in a few words. */
export function outcomeWords(task: Task): string {
  const chose = task.options?.find((o) => o.id === task.answer)?.label;
  if (task.state === "skipped") return "Set aside";
  if (chose) return `Chose "${chose}"`;
  return task.for.kind === "agent" ? `Finished by ${task.for.name}` : "Done";
}

function taskItem(task: Task, today: string | null): Item {
  const putOff = putOffWords(task, today);
  return {
    id: task.id,
    kind: "task",
    title: task.title,
    meta: [humanize(task.kind), putOff ?? dueWords(task.due, today)].filter((m): m is string => !!m),
    at: task.created_at,
    quiet: !!putOff,
    task,
  };
}

/** Needs you: the owner's open tasks in the server's order, the ones put off after the rest. */
export function needsItems(mine: Task[], today: string | null): Item[] {
  const asking = mine.filter((t) => asksToday(t, today));
  const later = mine.filter((t) => !asksToday(t, today));
  return [...asking, ...later].map((t) => taskItem(t, today));
}

/** The notifications, newest first. */
export function noteItems(notes: Notification[]): Item[] {
  return [...notes]
    .sort((a, b) => byNewest(a.created_at, b.created_at))
    .map((n) => ({
      id: n.id,
      kind: "note" as const,
      title: n.title,
      meta: [humanize(n.kind), n.level === "attention" ? "Worth a look" : null].filter((m): m is string => !!m),
      at: n.created_at,
      quiet: !!n.read_at,
      note: n,
    }));
}

/** What the agents have open, then what they finished, newest first. */
export function agentItems(theirs: Task[], done: Task[]): Item[] {
  const open = [...theirs].sort((a, b) => byNewest(a.created_at, b.created_at));
  const finished = done.filter((t) => t.for.kind === "agent").sort((a, b) => byNewest(a.done_at, b.done_at));
  return [
    ...open.map((t) => ({ id: t.id, kind: "agent" as const, title: t.title, meta: [`For ${t.for.name}`, "Open"], at: t.created_at, quiet: false, task: t })),
    ...finished.map((t) => ({ id: t.id, kind: "agent" as const, title: t.title, meta: [outcomeWords(t)], at: t.done_at ?? t.created_at, quiet: true, task: t })),
  ];
}

/** Past decisions, newest first, as the server keeps them. */
export function pastItems(records: DecisionRecord[]): Item[] {
  return records.map((r, i) => ({
    id: `${r.at}-${i}`,
    kind: "past" as const,
    title: r.title,
    meta: [r.decided_by === "You" ? r.decision : `${r.decision} (${r.decided_by})`, dateInWords(r.at)],
    at: r.at,
    quiet: false,
    record: r,
  }));
}

/** The rows whose title or meta hold every word of the find field. */
export function matching(items: Item[], q: string): Item[] {
  const words = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return items;
  return items.filter((i) => {
    const text = `${i.title} ${i.meta.join(" ")}`.toLowerCase();
    return words.every((w) => text.includes(w));
  });
}
