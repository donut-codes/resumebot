// What the Focus Inbox shows, worked out from the store's data: the rail's entries in order, a row's words, a packet's
// state. No markup here, so the components stay about looks and this stays about meaning.

import type { BoardCard, PacketProgress, SearchRow } from "../../api/types/pipeline";
import { CLOSED, FAVORITES, GROUPS, laneOf, RANKED, WORK } from "../../state/pipelineStore";

export interface RailEntry {
  key: string;
  label: string;
  /** A smaller line: the intake stages a person rarely opens. */
  small?: boolean;
  /** The stage that waits on the owner: its count is marked. */
  yours?: boolean;
  /** One of the stages of the entry above it: set in from the edge. */
  nested?: boolean;
}

export interface RailGroup {
  label: string | null;
  entries: RailEntry[];
}

export const RAIL: RailGroup[] = [
  {
    label: null,
    entries: [
      { key: FAVORITES, label: "Favorites" },
      { key: "ready_to_launch", label: "Ready to Launch", yours: true },
      { key: "applied", label: "Applied" },
      { key: "active_discussion", label: "Active Discussion" },
    ],
  },
  // The ranked list and work in progress, apart (docs/PRD.md 24.17): each is one list of its own, with its stages as
  // lists of their own under it. Ready to Launch, a stage of the ranked list, stays above with the stages the owner acts
  // in, and shows in the ranked list too.
  {
    label: null,
    entries: [
      { key: RANKED, label: "Ranked list" },
      { key: "first_impression", label: "First Impression", nested: true },
      { key: "second_impression", label: "Second Impression", nested: true },
    ],
  },
  {
    label: null,
    entries: [
      { key: WORK, label: "Work in progress" },
      { key: "added_links", label: "Added links", small: true, nested: true },
      { key: "followup_needed", label: "Followup needed", small: true, nested: true },
      { key: "landing_zone", label: "Landing Zone", small: true, nested: true },
      { key: "cleansed_data", label: "Cleansed Data", small: true, nested: true },
    ],
  },
  { label: null, entries: [{ key: CLOSED, label: "Closed" }] },
];

export const RAIL_ENTRIES: RailEntry[] = RAIL.flatMap((g) => g.entries);

export function railLabel(key: string): string {
  return RAIL_ENTRIES.find((e) => e.key === key)?.label ?? key;
}

/** Where the owner can put a role with one click, in the order of the process, with the control's short words. */
export const STAGE_STEPS: { status: string; short: string }[] = [
  { status: "first_impression", short: "First look" },
  { status: "second_impression", short: "Second look" },
  { status: "ready_to_launch", short: "Ready" },
  { status: "applied", short: "Applied" },
  { status: "active_discussion", short: "Discussion" },
];

/** The closed status a reason closes a role into: the employer's no is Rejected; anything else is the owner's call. */
export function closedStatusFor(reason: string): string {
  return reason === "they_said_no" ? "rejected" : "withdrew";
}

export type Tone = "plain" | "yours" | "closed";

export interface RowView {
  id: string;
  /** A role opens beside the list; a posting that is no role yet only links out. */
  openable: boolean;
  title: string;
  company: string;
  fit: number | null;
  /** The New tag: "New (+20)" with the points of the card's number that are its first hours (docs/PRD.md 24.15, 24.18), a
   * bare "New" when they add none, null for a role that is not new. */
  newTag: string | null;
  meta: string[];
  pay: string | null;
  next: string | null;
  nextTone: Tone;
  /** What is wrong with the check made when the role came into Ready to Launch (docs/PRD.md 24.16), set apart from
   * `next`; null when it found nothing wrong or none was made. */
  check: string | null;
  stage: string | null;
  stageTone: Tone;
  favorite: boolean;
  url: string | null;
  progress: PacketProgress | null;
}

function toneOf(status: string): Tone {
  if (status === "ready_to_launch") return "yours";
  if (laneOf(status) === CLOSED) return "closed";
  return "plain";
}

/** How a role's note is coloured: only a Ready to Launch role that Launch has not opened asks something of the
 * owner (its packet failing is bad news, a held packet waits). The words are the server's (`card.next`); this is the
 * only thing the screen adds to them. */
function nextToneOf(card: BoardCard): Tone {
  if (card.status !== "ready_to_launch" || card.opened_at) return "plain";
  const status = card.packet?.status;
  if (status === "failed") return "closed";
  if (status === "held") return "plain";
  return "yours";
}

/** A new role's number has its first hours in it (the bonus, docs/PRD.md 24.15): the points the server says, none for a rank
 * of 0, for a role with no number or when the owner turned the bonus off. */
export function countsFirstHours(card: BoardCard): boolean {
  return (card.first_hours_bonus ?? 0) > 0;
}

/** The New tag of a role in its first hours: "New (+20)" with the points of its number that are the hours', a bare "New"
 * when they add none; null for a role that is not new. */
export function newTag(card: BoardCard): string | null {
  if (!card.new) return null;
  return countsFirstHours(card) ? `New (+${card.first_hours_bonus})` : "New";
}

/** The words the server gives a Ready to Launch role's posting check when it is not the normal run
 * (`row._posting_check_words`): not confirmed by the page, or the browser unable to check it. A posting found open says
 * nothing. */
const CHECK_WORDS = /^(Posting not confirmed when checked|The browser could not check the posting)/;
const JOIN = " · ";
/** What the posting's pay says when it gives none: the row prints no label for an absent pay. */
const NO_PAY = new Set(["", "Not stated", "Not posted"]);

/** A Ready to Launch role's note taken in two: the check of its posting (docs/PRD.md 24.16) and the rest. The server puts
 * the check last, after a " · "; no other stage's note, and none without a check, is touched. */
export function splitCheck(card: BoardCard): { next: string | null; check: string | null } {
  if (card.status !== "ready_to_launch" || !card.next) return { next: card.next, check: null };
  const parts = card.next.split(JOIN);
  const at = parts.findIndex((part) => CHECK_WORDS.test(part));
  if (at < 0) return { next: card.next, check: null };
  const rest = parts.filter((_, i) => i !== at).join(JOIN);
  return { next: rest || null, check: parts[at] };
}

/** The building bar of a Ready to Launch role that Launch has not opened, while its packet is being written; null
 * otherwise. */
export function buildingProgress(card: BoardCard): PacketProgress | null {
  if (card.status !== "ready_to_launch" || card.opened_at) return null;
  return card.packet?.status === "writing" ? (card.packet.progress ?? null) : null;
}

/** A board card as a row: the server's words as they are, in the order and colours the list gives them. `showStage` for
 * a list that mixes stages (Favorites). */
export function fromCard(card: BoardCard, showStage: boolean): RowView {
  const age = card.age?.days === 0 ? "today" : card.age?.days != null ? `${card.age.days}d` : null;
  const { next, check } = splitCheck(card);
  return {
    id: card.id,
    openable: card.origin !== "posting",
    title: card.title,
    company: card.company,
    fit: card.score ?? card.fit_pct ?? null,
    newTag: newTag(card),
    meta: [card.work?.words, age].filter((x): x is string => !!x && x !== "Not stated"),
    pay: card.pay && !NO_PAY.has(card.pay) ? card.pay : null,
    next,
    nextTone: nextToneOf(card),
    check,
    stage: showStage ? card.status_label : null,
    stageTone: toneOf(card.status),
    favorite: !!card.favorite,
    url: card.url,
    progress: buildingProgress(card),
  };
}

/** A search's row. Every search row names its stage, since a search crosses them all. */
export function fromSearch(row: SearchRow): RowView {
  const closed = row.lane === CLOSED;
  return {
    id: row.id,
    openable: row.kind === "role",
    title: row.title,
    company: row.company,
    fit: row.fit_pct,
    newTag: null,
    meta: row.where ? [row.where] : [],
    pay: null,
    next: closed ? [row.closed_label, row.reason].filter(Boolean).join(": ") || null : row.reason,
    nextTone: "plain",
    check: null,
    stage: row.label,
    stageTone: closed ? "closed" : toneOf(row.status),
    favorite: row.favorite,
    url: row.url,
    progress: null,
  };
}

/** How many of a search's matches are in each rail entry: each stage, each group of stages, and the favorites. */
export function searchCounts(stages: Record<string, number>, favorites: number): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [status, n] of Object.entries(stages)) {
    const lane = laneOf(status);
    out[lane] = (out[lane] ?? 0) + n;
  }
  for (const [group, statuses] of Object.entries(GROUPS)) {
    const n = statuses.reduce((sum, status) => sum + (out[status] ?? 0), 0);
    if (n) out[group] = n;
  }
  if (favorites) out[FAVORITES] = favorites;
  return out;
}
