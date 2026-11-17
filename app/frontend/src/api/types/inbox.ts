import type { UUID } from "./shared";

// --- the Inbox and the tasks (GET /api/v1/inbox, /inbox/history, /tasks, POST /tasks/{id}/...) ------------------

/** A task's link: a search or a role, both of which open the Pipeline. Other kinds may appear; they open nothing. */
export interface TaskLink {
  kind: "opportunity" | "search_run" | (string & {});
  id: UUID;
}

/** One way to answer a decision. */
export interface TaskOption {
  id: string;
  label: string;
  description?: string | null;
  /** When there is one, the owner's words are wanted with this choice, and this says what to write. */
  note?: string | null;
}

/** Something to do. Its `kind` is free words ("review_roles", "search_failed", a new one tomorrow): the screens
 * never branch on it, they show the title and the detail. A task with `options` is a decision. */
export interface Task {
  id: UUID;
  title: string;
  kind: string;
  state: "open" | "done" | "skipped";
  detail: string | null;
  /** A plain date, "2026-10-09". */
  due: string | null;
  for: { kind: "owner" | "agent"; name: string };
  options: TaskOption[] | null;
  recommended: string | null;
  answer: string | null;
  note: string | null;
  /** What a decision rests on, side by side (an employer merge: both names with their roles, the careers site they
   * share); empty when the code that raised it gave none. */
  facts?: TaskFact[];
  link: TaskLink | null;
  snoozed_until: string | null;
  created_at: string;
  done_at: string | null;
}

/** One fact a decision rests on, in plain words. */
export interface TaskFact {
  label: string;
  value: string;
  sub?: string;
}

/** Something the app has to tell the owner. */
export interface Notification {
  id: UUID;
  kind: string;
  title: string;
  body: string | null;
  level: "info" | "attention";
  link: TaskLink | null;
  created_at: string;
  read_at: string | null;
}

export interface Inbox {
  /** The server's today, "2026-10-07". */
  today: string;
  /** How many of `items` need the owner: the Inbox tab's badge. */
  count: number;
  /** At most three. */
  items: Task[];
  notifications: Notification[];
  unread_count: number;
}

/** One past decision, newest first. */
export interface DecisionRecord {
  at: string;
  title: string;
  decision: string;
  decided_by: string;
  why: string | null;
  link: TaskLink | null;
}

export interface DecisionHistory {
  items: DecisionRecord[];
  next_before: string | null;
}

export interface TaskList {
  today: string;
  for_you: Task[];
  for_agents: Task[];
  done: Task[];
}
