// A board card for a test, made from the one row on file (`api/role-row.json`: the file the server's `role_row.Row`
// and the page's `BoardCard` are both checked against). A test says only what it cares about; every key of the row is
// there, so a card cannot lack one the server sends. Everything is made up.

import onFile from "../api/role-row.json";
import type { BoardCard } from "../api/types/pipeline";

/** What the row on file holds, every key of it: a role with something to say in each. */
export const ROW_ON_FILE = onFile as unknown as BoardCard;

/** The row on file for a plain role in play: nothing opened, no packet, no star, no reason, no ask, no link. */
const PLAIN: Partial<BoardCard> = {
  next: null, packet: null, opened_at: null, favorite: false, favorited_at: null, new: false, first_hours_bonus: 0,
  ask: null, ended_reason: null, ended_words: null, reason: null, url: null, apply_url: null, employer_id: null,
};

/** A card of the row's keys: a plain role in play (`PLAIN`) with `extra` laid over it. */
export function makeCard(extra: Partial<BoardCard> = {}): BoardCard {
  return { ...ROW_ON_FILE, ...PLAIN, ...extra };
}
