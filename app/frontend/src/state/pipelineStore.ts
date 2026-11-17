// The Pipeline's data layer: the one place the board's state lives. It reads the board and the favorites, applies the
// live stream (GET /api/v1/live) as it arrives, makes moves and stars at once and takes them back if the server
// refuses, and reads everything again when the stream says it fell behind (`resync`). Screens read only from here
// (`usePipeline`) and change things only through its actions; nothing in it knows how anything looks, so a new look
// swaps the components and keeps this. It stands on state/store.ts, as every screen's store does.

import { api as defaultApi, errorMessage } from "../api/client";
import type { LiveConnection, LiveHandler, LiveTopic } from "../api/live";
import { counted, createState, optimistic, storeContext, topicsOf, type OpenLive } from "./store";
import type {
  ApplyGo,
  Board,
  BoardCard,
  EndedReason,
  InterviewInput,
  LaneCounts,
  LiveEvents,
  LivePacket,
  LiveRole,
  RoleAdded,
  RoleDetail,
  SearchPage,
} from "../api/types/pipeline";

/** The list of every starred role, whatever its stage: first in the rail. */
export const FAVORITES = "favorites";
export const CLOSED = "closed";
/** The statuses the board shows as one lane, Closed. */
export const CLOSED_STATUSES = new Set(["gated_out", "rejected", "gone_quiet", "withdrew"]);
/** The two groups the page draws apart (docs/PRD.md 24.17), each the same roles as its lanes: the ranked list (First
 * Impression, Second Impression and Ready to Launch as one list by the one number, 24.15) and work in progress (the roles
 * still being got ready, which have no number yet). The server gives each as one column of its own
 * (`GET /api/v1/board?column=ranked`, `?column=work`). */
export const RANKED = "ranked";
export const WORK = "work";
export const GROUPS: Record<string, string[]> = {
  [RANKED]: ["first_impression", "second_impression", "ready_to_launch"],
  [WORK]: ["followup_needed", "landing_zone", "cleansed_data"],
};
export function isGroup(key: string): boolean {
  return key in GROUPS;
}
/** The group a status (or the lane of one) is in, or null. */
export function groupOf(status: string | null | undefined): string | null {
  if (!status) return null;
  return Object.keys(GROUPS).find((key) => GROUPS[key].includes(status)) ?? null;
}

export type PipelineApi = Pick<
  typeof defaultApi,
  | "board" | "favorites" | "role" | "roleRow" | "search" | "setFavorite" | "moveRole" | "moveBack" | "applyGo" | "applyGoProgress"
  | "askPacket" | "savePacketFiles" | "askSummary" | "profile" | "addRole" | "correctRequirement"
  | "addInterview" | "editInterview" | "removeInterview" | "prepareInterview"
>;
export type { OpenLive };
/** What the Pipeline hears on the live stream. `changed` is read for Go's progress. */
export const PIPELINE_TOPICS: LiveTopic[] = ["role", "counts", "packet", "summary", "changed"];

export interface Lane {
  key: string;
  label: string;
  cards: BoardCard[];
  /** How many the lane holds on the server (its cards may be the first page of them). */
  count: number;
  page: number;
  loadingMore: boolean;
}

export interface SearchState {
  q: string;
  status: "idle" | "loading" | "ready" | "error";
  result: SearchPage | null;
  error: string | null;
}

export interface DetailState {
  status: "loading" | "ready" | "error";
  data: RoleDetail | null;
  error: string | null;
}

export interface PipelineState {
  status: "idle" | "loading" | "ready" | "error";
  error: string | null;
  counts: LaneCounts | null;
  /** The lanes by key: the board's, Favorites, and a group (`ranked`, `work`) once a screen asked for it. A group's cards
   * are the same roles as its lanes', as the server's column of them gives them, so a lane's cards are never looked up in
   * one. */
  lanes: Record<string, Lane>;
  /** Where each group a screen asked for stands: being read, read, or not readable. */
  groupStatus: Record<string, "loading" | "ready" | "error">;
  endedReasons: EndedReason[];
  live: "connecting" | "open" | "closed";
  /** Roles with a move or a star on its way to the server. */
  busy: Record<string, true>;
  /** The last thing that went wrong with an action, in words, until it is dismissed. */
  notice: string | null;
  search: SearchState;
  details: Record<string, DetailState>;
  launch: ApplyGo | null;
  /** The target role the lanes are narrowed to (#/board?role=<id>), and its name; null for every role. */
  targetRole: { id: string; name: string | null } | null;
}

export function laneOf(status: string): string {
  return CLOSED_STATUSES.has(status) ? CLOSED : status;
}

/** The order a lane's cards are put in when one arrives here: the highest score first, then the highest fit. */
function rank(c: BoardCard): number {
  return c.score ?? c.fit_pct ?? -1;
}

function initialState(): PipelineState {
  return {
    status: "idle",
    error: null,
    counts: null,
    lanes: {},
    groupStatus: {},
    endedReasons: [],
    live: "connecting",
    busy: {},
    notice: null,
    search: { q: "", status: "idle", result: null, error: null },
    details: {},
    launch: null,
    targetRole: null,
  };
}

/** Strips the counts off a move's answer, leaving the card. */
function cardOf<T extends BoardCard>(answer: T & { counts?: LaneCounts }): BoardCard {
  const { counts: _counts, ...card } = answer;
  void _counts;
  return card as BoardCard;
}

export function createPipelineStore(opts: { api?: PipelineApi; openLive?: OpenLive } = {}) {
  const api = opts.api ?? defaultApi;
  const openLive = opts.openLive ?? topicsOf(PIPELINE_TOPICS);
  const core = createState(initialState());
  const set = core.set;
  // The state as it stands, read where the old code read its own variable.
  const current = () => core.getState();
  let searchSeq = 0;
  let loadSeq = 0;

  // --- reading the lanes ------------------------------------------------------------------------------------------

  function find(id: string): { card: BoardCard; lane: string; index: number } | null {
    for (const [key, lane] of Object.entries(current().lanes)) {
      if (key === FAVORITES || isGroup(key)) continue;
      const index = lane.cards.findIndex((c) => c.id === id);
      if (index >= 0) return { card: lane.cards[index], lane: key, index };
    }
    // A card only Favorites or a group's list holds is in none of its own lane's cards (`index` -1).
    for (const key of [FAVORITES, ...Object.keys(GROUPS)]) {
      const held = current().lanes[key]?.cards.find((c) => c.id === id);
      if (held) return { card: held, lane: laneOf(held.status), index: -1 };
    }
    return null;
  }

  function withLane(lanes: Record<string, Lane>, key: string, cards: (c: BoardCard[]) => BoardCard[], dCount = 0) {
    const lane = lanes[key];
    if (!lane) return lanes;
    return { ...lanes, [key]: { ...lane, cards: cards(lane.cards), count: Math.max(0, lane.count + dCount) } };
  }

  /** Puts a card in a lane: at `index` when given, else where its score puts it. */
  function insert(cards: BoardCard[], card: BoardCard, index?: number): BoardCard[] {
    const rest = cards.filter((c) => c.id !== card.id);
    let at = index ?? rest.findIndex((c) => rank(c) < rank(card));
    if (at < 0 || at > rest.length) at = rest.length;
    return [...rest.slice(0, at), card, ...rest.slice(at)];
  }

  /** Replaces a card wherever it is shown, keeping its place. */
  function patchEverywhere(lanes: Record<string, Lane>, id: string, patch: (c: BoardCard) => BoardCard) {
    const out: Record<string, Lane> = {};
    for (const [key, lane] of Object.entries(lanes)) {
      out[key] = lane.cards.some((c) => c.id === id)
        ? { ...lane, cards: lane.cards.map((c) => (c.id === id ? patch(c) : c)) }
        : lane;
    }
    return out;
  }

  /** Keeps the groups' lists as the card's status says: patched in its place while it stays in a group, taken out when it
   * left one, put in by its number when it came into one. `was` is the status (or lane) it had; with it unknown, the list
   * a card is in says where it was. */
  function regroup(lanes: Record<string, Lane>, card: BoardCard, was: string | null, countsToo: boolean) {
    const now = groupOf(card.status);
    const before = groupOf(was);
    let out = lanes;
    for (const key of Object.keys(GROUPS)) {
      const lane = out[key];
      if (!lane) continue;
      const inList = lane.cards.some((c) => c.id === card.id);
      if (now === key) {
        if (inList) out = withLane(out, key, (cs) => cs.map((c) => (c.id === card.id ? card : c)));
        else if (before !== key) out = withLane(out, key, (cs) => insert(cs, card), countsToo ? 1 : 0);
      } else if (inList || before === key) {
        out = withLane(out, key, (cs) => cs.filter((c) => c.id !== card.id), countsToo ? -1 : 0);
      }
    }
    return out;
  }

  /** Moves a card to the lane its status names (counts included), or patches it where it is. `wasStatus` is its status
   * before, for the groups' lists (the lane it comes from, when not given). */
  function place(lanes: Record<string, Lane>, card: BoardCard, fromLane: string | null, countsToo: boolean, wasStatus: string | null = null) {
    const toLane = laneOf(card.status);
    let out = lanes;
    if (fromLane && fromLane !== toLane) {
      out = withLane(out, fromLane, (cs) => cs.filter((c) => c.id !== card.id), countsToo ? -1 : 0);
      out = withLane(out, toLane, (cs) => insert(cs, card), countsToo ? 1 : 0);
    } else if (fromLane) {
      out = withLane(out, toLane, (cs) => cs.map((c) => (c.id === card.id ? card : c)));
    } else {
      out = withLane(out, toLane, (cs) => insert(cs, card), countsToo ? 1 : 0);
    }
    // The favorites list holds the same card, so it shows the new stage too.
    out = withLane(out, FAVORITES, (cs) => cs.map((c) => (c.id === card.id ? card : c)));
    return regroup(out, card, wasStatus ?? fromLane, countsToo);
  }

  function shiftCounts(counts: LaneCounts | null, from: string | null, to: string | null, favorites = 0) {
    if (!counts) return counts;
    const lanes = { ...counts.lanes };
    if (from && from in lanes) lanes[from] = Math.max(0, lanes[from] - 1);
    if (to && to in lanes) lanes[to] = lanes[to] + 1;
    // A move into or out of a group changes its count; one inside a group changes neither.
    const groups = { ...counts.groups };
    const [groupFrom, groupTo] = [groupOf(from), groupOf(to)];
    if (groupFrom !== groupTo) {
      if (groupFrom && groupFrom in groups) groups[groupFrom] = Math.max(0, groups[groupFrom] - 1);
      if (groupTo && groupTo in groups) groups[groupTo] = groups[groupTo] + 1;
    }
    let closedTotal = counts.closed_total;
    if (from === CLOSED) closedTotal = Math.max(0, closedTotal - 1);
    if (to === CLOSED) closedTotal += 1;
    return { ...counts, lanes, groups, closed_total: closedTotal, favorites: Math.max(0, counts.favorites + favorites) };
  }

  // --- loading ----------------------------------------------------------------------------------------------------

  function lanesFrom(board: Board): Record<string, Lane> {
    const lanes: Record<string, Lane> = {};
    for (const c of board.columns) {
      lanes[c.key] = { key: c.key, label: c.label, cards: c.cards, count: c.count, page: 1, loadingMore: false };
    }
    return lanes;
  }

  // The groups on a screen: read with the board every time it is read, so they stay current while they are shown. A group
  // is wanted from when it is picked (`loadGroup`) until another entry is (`releaseGroup`): the ranked list is about ten
  // thousand roles sorted on the server, and a read of the board must not read one that is not on screen.
  const wantedGroups = new Set<string>();

  /** Takes the "loading" off a group nothing is reading any more (its read was overtaken by a read of the board, or the
   * screen moved on). Never touches "ready" or "error": those are somebody's answer. */
  function stopLoading(key: string) {
    if (current().groupStatus[key] !== "loading") return;
    set((s) => {
      const groupStatus = { ...s.groupStatus };
      delete groupStatus[key];
      return { groupStatus };
    });
  }

  async function reload(): Promise<void> {
    const seq = ++loadSeq;
    if (current().status !== "ready") set({ status: "loading", error: null });
    const wanted = [...wantedGroups];
    try {
      const role = current().targetRole?.id;
      const [board, favorites, groups] = await Promise.all([
        api.board(undefined, role),
        api.favorites(),
        // A group that cannot be read now does not stop the board: it keeps the list it had.
        Promise.all(wanted.map((key) => api.board(key, role, undefined, undefined, 1).catch(() => null))),
      ]);
      if (seq !== loadSeq) return;
      const lanes = lanesFrom(board);
      lanes[FAVORITES] = { key: FAVORITES, label: "Favorites", cards: favorites.cards, count: favorites.count, page: 1, loadingMore: false };
      wanted.forEach((key, i) => {
        const read = groups[i];
        if (!wantedGroups.has(key)) return; // another entry was picked while this read was on
        if (read) Object.assign(lanes, lanesFrom({ columns: read.columns.filter((c) => c.key === key) }));
        else if (current().lanes[key]) lanes[key] = current().lanes[key];
      });
      // Each wanted group ends this read as read, or as not readable: never as "loading", which nothing would end. One
      // asked for while this read was on has a read of its own on, or yet to start.
      const groupStatus: PipelineState["groupStatus"] = {};
      for (const key of wantedGroups) {
        const asked = current().groupStatus[key];
        if (lanes[key]) groupStatus[key] = "ready";
        else if (wanted.includes(key)) groupStatus[key] = "error";
        else if (asked) groupStatus[key] = asked;
      }
      set({ status: "ready", error: null, lanes, groupStatus, endedReasons: board.ended_reasons ?? [] });
      // A group asked for while this read was on is read now.
      for (const key of wantedGroups) if (!lanes[key] && !wanted.includes(key)) void loadGroup(key);
    } catch (err) {
      if (seq !== loadSeq) return;
      set((s) => {
        const unread = [...wantedGroups].filter((key) => !s.lanes[key]);
        return {
          status: "error",
          error: errorMessage(err, "Couldn't load the pipeline."),
          groupStatus: { ...s.groupStatus, ...Object.fromEntries(unread.map((key) => [key, "error" as const])) },
        };
      });
    }
  }

  /** Reads one of the groups the page draws apart (`ranked`, `work`) as one column, its first page, and keeps it with the
   * board from then on, until `releaseGroup`. Asked while the board is being read, it comes when the board does. */
  async function loadGroup(key: string): Promise<void> {
    if (!isGroup(key)) return;
    wantedGroups.add(key);
    if (current().status !== "ready" || current().groupStatus[key] === "loading") return;
    const seq = loadSeq;
    set((s) => ({ groupStatus: { ...s.groupStatus, [key]: "loading" } }));
    try {
      const board = await api.board(key, current().targetRole?.id, undefined, undefined, 1);
      if (seq !== loadSeq || !wantedGroups.has(key)) return stopLoading(key); // overtaken by a read of the board, or let go
      const lane = lanesFrom({ columns: board.columns.filter((c) => c.key === key) })[key];
      if (!lane) throw new Error(`the server sent no ${key} list`);
      set((s) => ({ lanes: { ...s.lanes, [key]: lane }, groupStatus: { ...s.groupStatus, [key]: "ready" } }));
    } catch (err) {
      if (seq !== loadSeq || !wantedGroups.has(key)) return stopLoading(key); // the read of the board that overtook it answers
      set((s) => ({
        groupStatus: { ...s.groupStatus, [key]: "error" },
        notice: errorMessage(err, "Couldn't load that list."),
      }));
    }
  }

  /** The screen no longer shows this group: a read of the board stops reading it (its list, if it has one, stays as it was
   * until the board is read next, and still follows the roles that move). Picking it again reads it. */
  function releaseGroup(key: string): void {
    if (!wantedGroups.delete(key)) return;
    stopLoading(key);
    if (current().groupStatus[key]) {
      set((s) => {
        const groupStatus = { ...s.groupStatus };
        delete groupStatus[key];
        return { groupStatus };
      });
    }
  }

  async function loadMore(key: string): Promise<void> {
    const lane = current().lanes[key];
    if (!lane || lane.loadingMore || lane.cards.length >= lane.count || key === FAVORITES) return;
    set((s) => ({ lanes: { ...s.lanes, [key]: { ...lane, loadingMore: true } } }));
    try {
      const page = lane.page + 1;
      const board = await api.board(key, current().targetRole?.id, undefined, undefined, page);
      const col = board.columns.find((c) => c.key === key);
      set((s) => {
        const now = s.lanes[key];
        const have = new Set(now.cards.map((c) => c.id));
        const more = (col?.cards ?? []).filter((c) => !have.has(c.id));
        return { lanes: { ...s.lanes, [key]: { ...now, cards: [...now.cards, ...more], count: col?.count ?? now.count, page, loadingMore: false } } };
      });
    } catch (err) {
      set((s) => ({
        lanes: { ...s.lanes, [key]: { ...s.lanes[key], loadingMore: false } },
        notice: errorMessage(err, "Couldn't load more roles."),
      }));
    }
  }

  async function loadDetail(id: string): Promise<void> {
    const had = current().details[id]?.data ?? null;
    set((s) => ({ details: { ...s.details, [id]: { status: had ? "ready" : "loading", data: had, error: null } } }));
    try {
      const data = await api.role(id);
      set((s) => ({ details: { ...s.details, [id]: { status: "ready", data, error: null } } }));
      askSummary(id, data);
    } catch (err) {
      set((s) => ({ details: { ...s.details, [id]: { status: "error", data: had, error: errorMessage(err, "Couldn't load this role.") } } }));
    }
  }

  // A researched role opened without a summary asks the analyst for one, once a page; the live stream's `summary`
  // reads the role again when it is written.
  const summaryAsked = new Set<string>();
  function askSummary(id: string, data: RoleDetail): void {
    if (!data.summary_askable || summaryAsked.has(id)) return;
    summaryAsked.add(id);
    void api.askSummary(id).catch(() => summaryAsked.delete(id));
  }

  function openRole(id: string): void {
    const d = current().details[id];
    if (!d || d.status === "error") void loadDetail(id);
  }

  // --- the live stream --------------------------------------------------------------------------------------------

  // A role's row: read on its own (`GET /roles/{id}/row`), unless the role is open, whose whole page is read (its top
  // is the same row) so the page and the card change together.
  async function fetchCard(id: string): Promise<BoardCard | null> {
    try {
      if (!current().details[id]) return await api.roleRow(id);
      const detail = await api.role(id);
      set((s) => ({ details: s.details[id] ? { ...s.details, [id]: { status: "ready", data: detail, error: null } } : s.details }));
      return detail.role;
    } catch {
      return null;
    }
  }

  // The server's row for a role carries its note in the server's own words, so a change the stream announces is read
  // again and the row it answers replaces the card wherever it is shown, and the open role. Only the latest read for a
  // role is kept.
  const reading = new Map<string, number>();
  function refreshCard(id: string): void {
    const seq = (reading.get(id) ?? 0) + 1;
    reading.set(id, seq);
    void fetchCard(id).then((fresh) => {
      if (!fresh || reading.get(id) !== seq) return;
      set((s) => ({ lanes: favoritesFor(patchEverywhere(s.lanes, id, () => fresh), fresh) }));
    });
  }

  function applyRole(ev: LiveRole): void {
    const found = find(ev.id);
    const toLane = ev.lane_to ?? (ev.to ? laneOf(ev.to) : null);
    const reread = !!found && ev.changed.length > 0;
    if (found) {
      // The card moves at once, with the three things the event says; the server's row follows and replaces it.
      const card: BoardCard = {
        ...found.card,
        status: ev.to ?? found.card.status,
        status_label: ev.label ?? found.card.status_label,
        favorite: ev.favorite,
      };
      set((s) => ({ lanes: favoritesFor(place(s.lanes, card, found.index >= 0 ? found.lane : null, !!s.targetRole, found.card.status), card) }));
      if (reread) refreshCard(ev.id);
    } else if (current().targetRole) {
      // Narrowed to one target role: a role this page does not hold may be another's, so only the counts it can trust
      // change (its own lanes'); the next read brings the rest.
    } else if ((toLane && current().lanes[toLane]) || ev.favorite) {
      void fetchCard(ev.id).then((card) => {
        if (!card || find(card.id)) return;
        set((s) => ({ lanes: favoritesFor(place(s.lanes, card, null, false), card) }));
      });
    }
    // A search's rows say the role's stage too.
    const result = current().search.result;
    if (result?.items.some((r) => r.kind === "role" && r.id === ev.id)) {
      set((s) => ({
        search: {
          ...s.search,
          result: s.search.result && {
            ...s.search.result,
            items: s.search.result.items.map((r) =>
              r.kind === "role" && r.id === ev.id
                ? { ...r, status: ev.to ?? r.status, lane: toLane ?? r.lane, label: ev.label ?? r.label, favorite: ev.favorite, closed_reason: ev.closed_reason, reason: ev.reason ?? r.reason }
                : r,
            ),
          },
        },
      }));
    }
    if (current().details[ev.id] && !reread) void loadDetail(ev.id);
  }

  /** The favorites list as the card's star says: added first when starred, left out when not. */
  function favoritesFor(lanes: Record<string, Lane>, card: BoardCard): Record<string, Lane> {
    const fav = lanes[FAVORITES];
    if (!fav) return lanes;
    const has = fav.cards.some((c) => c.id === card.id);
    if (card.favorite && !has) return { ...lanes, [FAVORITES]: { ...fav, cards: [card, ...fav.cards], count: fav.count + 1 } };
    if (!card.favorite && has) return { ...lanes, [FAVORITES]: { ...fav, cards: fav.cards.filter((c) => c.id !== card.id), count: Math.max(0, fav.count - 1) } };
    return lanes;
  }

  function applyPacket(ev: LivePacket): void {
    const packet = { id: ev.id, version: ev.version, status: ev.status, progress: ev.progress };
    set((s) => {
      const detail = s.details[ev.role_id];
      return {
        lanes: patchEverywhere(s.lanes, ev.role_id, (c) => ({ ...c, packet })),
        details: detail?.data
          ? { ...s.details, [ev.role_id]: { ...detail, data: { ...detail.data, role: { ...detail.data.role, packet } } } }
          : s.details,
      };
    });
    // A packet that changed state is read again: its documents on an open role, and the words of the role's row, which
    // only a held or a failed packet changes (24.18: one written or still to build says nothing). While one is being
    // written, the bar and its label come from the event above.
    if (ev.from !== ev.status && (find(ev.role_id) || current().details[ev.role_id])) refreshCard(ev.role_id);
  }

  function applyCounts(counts: LaneCounts): void {
    set((s) => {
      // The lanes' own totals follow the server's counts, so "Show more" knows what is left.
      // Narrowed to one target role, the server's counts are every role's: the lanes keep their own.
      if (s.targetRole) return { counts };
      const lanes: Record<string, Lane> = { ...s.lanes };
      for (const [key, n] of Object.entries(counts.lanes)) if (lanes[key]) lanes[key] = { ...lanes[key], count: n };
      for (const [key, n] of Object.entries(counts.groups)) if (lanes[key]) lanes[key] = { ...lanes[key], count: n };
      if (lanes[CLOSED]) lanes[CLOSED] = { ...lanes[CLOSED], count: counts.closed_total };
      return { counts, lanes };
    });
  }

  const onEvent: LiveHandler = (event, data) => {
    if (event === "hello") {
      const said = (data as LiveEvents["hello"]).counts;
      if (said) applyCounts(said);
    }
    else if (event === "counts") applyCounts(data as LiveEvents["counts"]);
    else if (event === "role") applyRole(data as LiveEvents["role"]);
    else if (event === "packet") applyPacket(data as LiveEvents["packet"]);
    else if (event === "summary") {
      const id = (data as LiveEvents["summary"]).id;
      if (current().details[id]) void loadDetail(id);
    }
    else if (event === "changed") {
      if ((data as LiveEvents["changed"]).area === "go") void readLaunch();
    }
    else if (event === "resync") {
      void reload();
      void readLaunch();
      Object.keys(current().details).forEach((id) => void loadDetail(id));
    }
  };

  // --- actions ----------------------------------------------------------------------------------------------------

  function setBusy(id: string, on: boolean) {
    set((s) => {
      const busy = { ...s.busy };
      if (on) busy[id] = true;
      else delete busy[id];
      return { busy };
    });
  }

  /** Moves a role at once, then asks the server; puts it back where it was if the server refuses. */
  async function move(id: string, to: string, endedReason?: string): Promise<boolean> {
    return moveWith(id, to, () => api.moveRole(id, to, undefined, endedReason));
  }

  /** Moves a closed role back to where it was before it closed. The server knows where that is, so it waits for it. */
  async function moveBack(id: string): Promise<boolean> {
    const found = find(id);
    // A role open in the pane may be in no loaded lane; its detail is then all there is, and the server's answer
    // brings the counts.
    if (!found && !current().details[id]?.data) return false;
    setBusy(id, true);
    try {
      const answer = await api.moveBack(id);
      const card = cardOf(answer);
      set((s) => ({ lanes: found ? place(s.lanes, card, found.index >= 0 ? found.lane : null, true, found.card.status) : s.lanes, counts: answer.counts }));
      afterChange(id);
      return true;
    } catch (err) {
      set({ notice: errorMessage(err, "Couldn't move it back.") });
      return false;
    } finally {
      setBusy(id, false);
    }
  }

  async function moveWith(id: string, to: string, call: () => ReturnType<PipelineApi["moveRole"]>): Promise<boolean> {
    const found = find(id);
    const before = found?.card ?? current().details[id]?.data?.role ?? null;
    if (!before || before.status === to) return false;
    const fromLane = found && found.index >= 0 ? found.lane : null;
    const toLane = laneOf(to);
    const label = current().lanes[toLane]?.label ?? before.status_label;
    const moved: BoardCard = { ...before, status: to, status_label: CLOSED_STATUSES.has(to) ? before.status_label : label };
    const inDetail = (s: PipelineState, card: BoardCard) => {
      const d = s.details[id];
      return d?.data ? { ...s.details, [id]: { ...d, data: { ...d.data, role: { ...d.data.role, status: card.status, status_label: card.status_label } } } } : s.details;
    };
    try {
      return await optimistic(
        () => {
          set((s) => ({
            lanes: found ? place(s.lanes, moved, fromLane, true, before.status) : s.lanes,
            details: inDetail(s, moved),
            counts: shiftCounts(s.counts, laneOf(before.status), toLane),
          }));
          setBusy(id, true);
        },
        call,
        // Back where it was, at the same place in its lane.
        (err) =>
          set((s) => {
            let lanes = s.lanes;
            if (found) {
              lanes = withLane(lanes, laneOf(moved.status), (cs) => cs.filter((c) => c.id !== id), -1);
              if (fromLane) lanes = withLane(lanes, fromLane, (cs) => insert(cs, before, found.index), 1);
              lanes = withLane(lanes, FAVORITES, (cs) => cs.map((c) => (c.id === id ? before : c)));
              lanes = regroup(lanes, before, moved.status, true);
            }
            return { lanes, details: inDetail(s, before), counts: shiftCounts(s.counts, toLane, laneOf(before.status)), notice: errorMessage(err, "Couldn't move that role.") };
          }),
        (answer) => {
          const card = cardOf(answer);
          set((s) => ({ lanes: found ? place(s.lanes, card, toLane, true) : s.lanes, counts: answer.counts }));
          afterChange(id);
        },
      );
    } finally {
      setBusy(id, false);
    }
  }

  /** Stars or unstars a role at once, then asks the server; takes it back if the server refuses. */
  async function toggleFavorite(id: string): Promise<boolean> {
    // A role open in the pane may be in no loaded lane; its detail then says whether it is starred.
    const found = find(id);
    const base = found?.card ?? current().details[id]?.data?.role ?? null;
    if (!base) return false;
    const was = !!base.favorite;
    const favIndex = current().lanes[FAVORITES]?.cards.findIndex((c) => c.id === id) ?? -1;
    const flipped = { ...base, favorite: !was };
    const inDetail = (s: PipelineState, favorite: boolean) => {
      const d = s.details[id];
      return d?.data ? { ...s.details, [id]: { ...d, data: { ...d.data, role: { ...d.data.role, favorite } } } } : s.details;
    };
    try {
      return await optimistic(
        () => {
          set((s) => ({
            lanes: favoritesFor(patchEverywhere(s.lanes, id, (c) => ({ ...c, favorite: !was })), flipped),
            details: inDetail(s, !was),
            counts: shiftCounts(s.counts, null, null, was ? -1 : 1),
          }));
          setBusy(id, true);
        },
        () => api.setFavorite(id, !was),
        (err) =>
          set((s) => {
            let lanes = patchEverywhere(s.lanes, id, (c) => ({ ...c, favorite: was }));
            const fav = lanes[FAVORITES];
            if (fav) {
              const rest = fav.cards.filter((c) => c.id !== id);
              const cards = was ? insert(rest, { ...base, favorite: true }, Math.max(favIndex, 0)) : rest;
              lanes = { ...lanes, [FAVORITES]: { ...fav, cards, count: cards.length } };
            }
            return { lanes, details: inDetail(s, was), counts: shiftCounts(s.counts, null, null, was ? 1 : -1), notice: errorMessage(err, "Couldn't change the star.") };
          }),
        (answer) => {
          const card = answer.role;
          set((s) => {
            const at = find(id);
            // A role in no loaded lane is not put into one; Favorites and the counts say it.
            const lanes = found || at ? place(s.lanes, card, at && at.index >= 0 ? at.lane : null, true) : s.lanes;
            return { lanes: favoritesFor(lanes, card), counts: answer.counts };
          });
          afterChange(id);
        },
      );
    } finally {
      setBusy(id, false);
    }
  }

  /** After the server confirms a change, the open role and a search say it too. */
  function afterChange(id: string) {
    if (current().details[id]) void loadDetail(id);
    const card = find(id)?.card;
    if (card && current().search.result?.items.some((r) => r.id === id)) {
      set((s) => ({
        search: {
          ...s.search,
          result: s.search.result && {
            ...s.search.result,
            items: s.search.result.items.map((r) =>
              r.kind === "role" && r.id === id ? { ...r, status: card.status, label: card.status_label, lane: laneOf(card.status), favorite: !!card.favorite } : r,
            ),
          },
        },
      }));
    }
  }

  /** Narrows the lanes to one target role (or every role, with null) and reads them again. */
  function setTargetRole(id: string | null): void {
    if ((current().targetRole?.id ?? null) === id) return;
    set({ targetRole: id ? { id, name: null } : null });
    if (life.users() > 0 || current().status !== "idle") void reload();
    if (id) {
      void api.profile().then((p) => {
        const name = p.criteria.target_roles.find((t) => t.id === id)?.name ?? null;
        if (current().targetRole?.id === id) set({ targetRole: { id, name } });
      }, () => {});
    }
  }

  /** Asks the writer for a packet, or a new version of one with the owner's words; the role is read again. */
  async function askPacket(id: string, feedback = ""): Promise<boolean> {
    setBusy(id, true);
    try {
      await api.askPacket(id, feedback);
      await loadDetail(id);
      return true;
    } catch (err) {
      set({ notice: errorMessage(err, feedback ? "Couldn't ask for changes." : "Couldn't start the packet.") });
      return false;
    } finally {
      setBusy(id, false);
    }
  }

  /** Saves a ready packet's files to the packet folder again; the role is read again so the problem line follows. */
  async function savePacketFiles(roleId: string, packetId: string): Promise<boolean> {
    setBusy(roleId, true);
    try {
      await api.savePacketFiles(packetId);
      await loadDetail(roleId);
      return true;
    } catch (err) {
      set({ notice: errorMessage(err, "Couldn't save the files.") });
      return false;
    } finally {
      setBusy(roleId, false);
    }
  }

  /** An interview added, changed, removed or prepared again (docs/PRD.md 15.2): the role is read again so its Interviews part
   * and its History follow. A refusal is thrown, in words, for the form or button that asked to show. */
  async function interviewChange(roleId: string, change: () => Promise<unknown>): Promise<void> {
    await change();
    await loadDetail(roleId);
  }

  async function search(q: string): Promise<void> {
    const seq = ++searchSeq;
    if (!q.trim()) {
      set({ search: { q, status: "idle", result: null, error: null } });
      return;
    }
    set((s) => ({ search: { ...s.search, q, status: "loading", error: null } }));
    try {
      const result = await api.search(q, 1, 100);
      if (seq !== searchSeq) return;
      set({ search: { q, status: "ready", result, error: null } });
    } catch (err) {
      if (seq !== searchSeq) return;
      set((s) => ({ search: { ...s.search, status: "error", error: errorMessage(err, "Couldn't search.") } }));
    }
  }

  /** Adds a job by its link (the server checks it and says where it landed). The role list is read again unless the
   * owner already had it; a refusal is thrown, in words, for the box that asked to show. */
  async function addRole(url: string): Promise<RoleAdded> {
    const answer = await api.addRole(url);
    if (!answer.already) void reload();
    return answer;
  }

  async function launch(): Promise<void> {
    try {
      const went = await api.applyGo();
      set({ launch: went });
    } catch (err) {
      set({ notice: errorMessage(err, "Couldn't start Launch in Browser.") });
    }
  }

  /** Reads where Go stands; shown when it is running, or when the page was already showing it (so its end shows too). */
  async function readLaunch(): Promise<void> {
    try {
      const went = await api.applyGoProgress();
      if ((!went.done && went.phase !== "idle") || current().launch !== null) set({ launch: went });
    } catch {
      // What is shown stays; the next notice reads it again.
    }
  }

  // --- life --------------------------------------------------------------------------------------------------------

  /** A screen that shows the pipeline starts it when it opens; the stream stays open while any screen is open. */
  const life = counted(() => {
    void reload();
    set({ live: "connecting" });
    // A stream that dropped and came back may have missed messages: read everything again.
    const connection: LiveConnection = openLive(onEvent, (s) => {
      const back = s === "open" && current().live === "closed";
      set({ live: s });
      if (back) {
        void reload();
        void readLaunch();
      }
    });
    void readLaunch();
    return () => {
      connection.close();
    };
  });

  return {
    getState: core.getState,
    subscribe: core.subscribe,
    start: life.start,
    reload,
    /** Reads one open role again (after its qualification grade was changed). */
    reloadRole: loadDetail,
    /** Changes a required item's class with a reason; the role is read again, so the mark and the fit follow. */
    correctRequirement: async (id: string, index: number, cls: string, why: string) => {
      const said = await api.correctRequirement(id, index, cls, why);
      void loadDetail(id);
      return said;
    },
    loadMore,
    loadGroup,
    releaseGroup,
    openRole,
    move,
    moveBack,
    toggleFavorite,
    search,
    launch,
    addRole,
    setTargetRole,
    askPacket,
    savePacketFiles,
    addInterview: (roleId: string, body: InterviewInput) => interviewChange(roleId, () => api.addInterview(roleId, body)),
    editInterview: (roleId: string, id: string, body: InterviewInput) => interviewChange(roleId, () => api.editInterview(id, body)),
    removeInterview: (roleId: string, id: string) => interviewChange(roleId, () => api.removeInterview(id)),
    prepareInterview: (roleId: string, id: string) => interviewChange(roleId, () => api.prepareInterview(id)),
    dismissNotice: () => set({ notice: null }),
    /** For tests: what the stream would have said. */
    receive: onEvent,
  };
}

export type PipelineStore = ReturnType<typeof createPipelineStore>;

const context = storeContext<PipelineStore, PipelineState>("usePipelineStore");

/** Hands a store to the screens below (a test's own, or the app's). */
export const PipelineStoreProvider = context.Provider;
export const usePipelineStore = context.useStore;
/** Reads part of the pipeline's state; the screen redraws only when that part changes. */
export const usePipeline = context.useSlice;
