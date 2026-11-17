// "First match" and "Any of" rows of asks on Prioritization, as plain functions (the prioritization groups spec).
// The page keeps them as drafts beside the flat list of asks and sends them with it; nothing here regroups or removes an ask
// by itself. The words on the screens never say "group". (The file is not named for ScoringSets.tsx: the two names would differ
// only in case, which a case-insensitive disk cannot tell apart.)

import type { Ask, Group, GroupKind, SectionId, Suggestion } from "../../api/types/profile";
import { newId } from "../../lib/ids";
import { tabOf } from "./scope";

/** The kinds of row the page has: "first match" (`best`) and "any of" (`any`). An "all of" row was dropped, so a group of any
 * other kind is kept exactly as it came and is neither shown nor counted. */
export const SET_KINDS: readonly GroupKind[] = ["best", "any"];

export const isSet = (g: Group): boolean => SET_KINDS.includes(g.kind);

/** A suggestion to put asks in one row (not one to remove asks). */
export type RowSuggestion = Suggestion & { kind: GroupKind };

export const isRowSuggestion = (s: Suggestion): s is RowSuggestion => (SET_KINDS as readonly string[]).includes(s.kind);

/** What an item sorts by within a part, highest first, kept from the last save so a row stays put while its numbers are
 * typed: an ask by its points; a first match row by its best statement; an "any of" row by the row's points. A row of
 * penalties sorts with the penalties, as an ask does. */
export function orderKey(item: Ask | Group): number {
  if (!("members" in item)) return item.points;
  if (item.kind === "any") return item.points ?? 0;
  return item.members.length > 0 ? Math.max(...item.members.map((m) => m.points)) : 0;
}

/** Whether a statement lower down earns more than the one above it. It is allowed (the first true one from the top still
 * counts), and the page says so in a quiet line. Only a statement that adds points counts: penalties are put worst first
 * because the first true statement counts, so a row of them rises on purpose. */
export function worthMoreBelow(statements: readonly Pick<Ask, "points">[]): boolean {
  return statements.some((s, i) => i > 0 && s.points > 0 && s.points > statements[i - 1].points);
}

/** Every id the document uses, in one list: ids are one namespace across asks, rows and statements. */
export function idsOf(asks: readonly Ask[], groups: readonly Group[]): { id: string }[] {
  return [...asks, ...groups.flatMap((g) => [g, ...g.members])].map(({ id }) => ({ id }));
}

/** The rows on a tab (and in one part of it, when `section` is given) that the page has. A row on no tab (it names a role that
 * is gone) is in none. */
export const groupsIn = (groups: readonly Group[], scope: string, roleIds: ReadonlySet<string>, section?: SectionId): Group[] =>
  groups.filter((g) => isSet(g) && tabOf(g, roleIds) === scope && (section === undefined || g.section === section));

/** The asks and groups as they are saved. An ask or statement with no sentence is dropped. A row left with two or more
 * statements is saved as it is; one left with a single sentence is saved as a plain ask (that sentence's own id and words, in
 * the row's part and tab, worth the statement's points or the row's), and one left with none is dropped. A group of a kind the
 * page does not have goes back as it came. */
export function tidy(asks: readonly Ask[], groups: readonly Group[]): { asks: Ask[]; groups: Group[] } {
  const kept = asks.filter((a) => a.says.trim() !== "");
  const out: Group[] = [];
  const single: Ask[] = [];
  for (const g of groups) {
    if (!isSet(g)) {
      out.push(g);
      continue;
    }
    const members = g.members.filter((m) => m.says.trim() !== "");
    if (members.length >= 2) out.push(members.length === g.members.length ? g : { ...g, members });
    else if (members.length === 1) {
      const [only] = members;
      single.push({ ...only, section: g.section, roles: [...(g.roles ?? [])], points: g.kind === "any" ? (g.points ?? 0) : only.points });
    }
  }
  return { asks: [...kept, ...single], groups: out };
}

/** Whether an ask is one the app decides: it names a way (an employer fact, a posting's age, a pay amount or the requirement
 * classes), or it is said to be the app's. Such an ask, and a reader's ask with a way the app uses once it holds the facts,
 * is not joined to a row by a suggestion. */
export const decidedByApp = (a: Ask): boolean =>
  a.checked_by === "app" || Boolean(a.fact) || (a.days !== null && a.days !== undefined) || (a.amount !== null && a.amount !== undefined) || Boolean(a.from_classes);

/** An ask a row can hold: not counted for each item found, not the built-in one, not decided by the app. */
const canJoin = (a: Ask): boolean => !a.per_item && !a.derived && !decidedByApp(a);

/** A suggestion as the page can show it, with the owner's own asks it names. */
export interface Card {
  /** The asks, in the order proposed. */
  members: Ask[];
  /** The ones that sit in another part than the first ask's and would move into it. */
  moves: Ask[];
  /** The part the row would be in (the first ask's) and the tab (the first ask's). */
  section: SectionId;
  tab: string;
}

/** The suggestion, with the asks it names, when the page can still use it; null when it cannot: it is not one to put asks in a
 * row, it names fewer than two asks, an ask is gone (removed, or already in a row), has no sentence, or cannot join (counted
 * for each item, the built-in one, one the app decides), or the asks are not all on one tab. */
export function cardOf(s: Suggestion, asks: readonly Ask[], roleIds: ReadonlySet<string>): Card | null {
  if (!isRowSuggestion(s) || s.asks.length < 2 || new Set(s.asks).size !== s.asks.length) return null;
  const members: Ask[] = [];
  for (const id of s.asks) {
    const found = asks.find((a) => a.id === id);
    if (!found || found.says.trim() === "" || !canJoin(found)) return null;
    members.push(found);
  }
  const tab = tabOf(members[0], roleIds);
  if (tab === null || members.some((m) => tabOf(m, roleIds) !== tab)) return null;
  const section = members[0].section;
  return { members, moves: members.filter((m) => m.section !== section), section, tab };
}

/** The asks and groups with a suggestion used: the asks it names leave the flat list and join one new row, in the order
 * proposed, in the first ask's part and tab. Each keeps its id and its sentence (so the answers on record still count); in an
 * "any of" row the statements carry no points and the row takes the suggestion's. Nothing is saved by this. */
export function applySuggestion(s: RowSuggestion, asks: readonly Ask[], groups: readonly Group[]): { asks: Ask[]; groups: Group[]; group: Group } {
  const members = s.asks.flatMap((id) => asks.filter((a) => a.id === id));
  const [first] = members;
  const section = first.section;
  const roles = [...(first.roles ?? [])];
  const anyOf = s.kind === "any";
  const group: Group = {
    id: newId({ name: anyOf ? "any-of" : "row" }, idsOf(asks, groups)),
    kind: s.kind,
    section,
    roles,
    points: anyOf ? s.points : 0,
    members: members.map((m) => ({ ...m, section, roles: [...roles], points: anyOf ? 0 : m.points })),
  };
  return { asks: asks.filter((a) => !s.asks.includes(a.id)), groups: [...groups, group], group };
}

/** The asks a suggestion to remove names that the page still has, in the order named; none when they are all gone. */
export function overlapOf(s: Suggestion, asks: readonly Ask[]): Ask[] {
  return s.asks.flatMap((id) => asks.filter((a) => a.id === id && a.says.trim() !== ""));
}
