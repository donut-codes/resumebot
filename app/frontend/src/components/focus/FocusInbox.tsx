// The Pipeline, as a focus inbox: the stages down the left with their live counts (Favorites first), the roles of the
// one picked (or of a search across every stage) in the middle, and the role open beside them. The ranked list (First
// Impression, Second Impression and Ready to Launch as one list by the one number) and work in progress (the roles still
// being got ready) are each an entry of their own, apart (docs/PRD.md 24.17). This file wires the store and the address
// to the pieces; the pieces only draw.

import { useEffect, useId, useMemo, useState } from "react";
import type { BoardCard, RoleAdded } from "../../api/types/pipeline";
import { useRoute } from "../../lib/appContext";
import { routeFor } from "../../lib/route";
import { NARROW, useMedia } from "../../lib/useMedia";
import { FAVORITES, GROUPS, isGroup, usePipeline, usePipelineStore } from "../../state/pipelineStore";
import AddJob from "./AddJob";
import { CrossIcon, FindIcon, PlusIcon } from "./icons";
import LaunchButton, { launchWords } from "./LaunchButton";
import { closedStatusFor, fromCard, fromSearch, RAIL_ENTRIES, railLabel, searchCounts } from "./model";
import RoleList from "./RoleList";
import RolePane from "./RolePane";
import StageRail from "./StageRail";

const SEARCH_WAIT_MS = 200;

const STAGE_KEYS = new Set(RAIL_ENTRIES.map((e) => e.key));

export default function FocusInbox() {
  const store = usePipelineStore();
  const { route, go } = useRoute();
  const narrow = useMedia(NARROW);
  const [stage, setStage] = useState<string>(() => (route.boardStage && STAGE_KEYS.has(route.boardStage) ? route.boardStage : "ready_to_launch"));
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState(false);
  const [added, setAdded] = useState<string | null>(null);
  const addId = useId();

  const status = usePipeline((s) => s.status);
  const error = usePipeline((s) => s.error);
  const lanes = usePipeline((s) => s.lanes);
  const groupStatus = usePipeline((s) => s.groupStatus);
  const counts = usePipeline((s) => s.counts);
  const search = usePipeline((s) => s.search);
  const busy = usePipeline((s) => s.busy);
  const notice = usePipeline((s) => s.notice);
  const endedReasons = usePipeline((s) => s.endedReasons);
  const details = usePipeline((s) => s.details);
  const went = usePipeline((s) => s.launch);
  const narrowed = usePipeline((s) => s.targetRole);
  const target = route.targetRole;

  // The target role the address names narrows the lanes; set before the store starts, so the first read is narrowed.
  useEffect(() => store.setTargetRole(target), [store, target]);
  useEffect(() => store.start(), [store]);
  // A group (the ranked list, work in progress) is the server's column of its own, read when it is picked and kept current
  // while it is shown: a read of the board does not read one that was let go.
  useEffect(() => {
    if (!isGroup(stage)) return;
    void store.loadGroup(stage);
    return () => store.releaseGroup(stage);
  }, [stage, store]);
  useEffect(() => {
    const t = setTimeout(() => void store.search(query), query.trim() ? SEARCH_WAIT_MS : 0);
    return () => clearTimeout(t);
  }, [query, store]);

  const searching = query.trim() !== "";
  const lane = lanes[stage];
  const rows = useMemo(() => {
    if (searching) return (search.result?.items ?? []).map(fromSearch);
    // A list that mixes stages says each role's stage.
    return (lane?.cards ?? []).map((c) => fromCard(c, stage === FAVORITES || isGroup(stage)));
  }, [searching, search.result, lane, stage]);

  // The rail's counts: the server's live counts, else what the board said.
  const railCounts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const [k, l] of Object.entries(lanes)) out[k] = l.count;
    if (counts) {
      // Narrowed to one target role, the lanes' own counts are the narrowed ones; the server's are every role's.
      if (!narrowed) {
        Object.assign(out, counts.lanes);
        out.closed = counts.closed_total;
      }
      out[FAVORITES] = counts.favorites;
    }
    // A group's count is the server's, else its list's, else the sum of its stages' (narrowed to one target role, the
    // stages' own).
    for (const [group, statuses] of Object.entries(GROUPS)) {
      const said = counts && !narrowed ? counts.groups[group] : undefined;
      out[group] = said ?? out[group] ?? statuses.reduce((n, status) => n + (out[status] ?? 0), 0);
    }
    return out;
  }, [lanes, counts, narrowed]);
  const labels = useMemo(() => Object.fromEntries(Object.values(lanes).map((l) => [l.key, l.label])), [lanes]);
  const hits = searching && search.result ? searchCounts(search.result.stages, search.result.items.filter((r) => r.favorite).length) : null;

  // The role open: the one the address names, else (with room for the pane) the first in the list.
  const firstOpenable = rows.find((r) => r.openable)?.id ?? null;
  const openId = route.roleId ?? (narrow ? null : firstOpenable);
  useEffect(() => {
    if (openId) store.openRole(openId);
  }, [openId, store]);
  const openCard = useMemo<BoardCard | null>(() => {
    if (!openId) return null;
    for (const l of Object.values(lanes)) {
      const c = l.cards.find((x) => x.id === openId);
      if (c) return c;
    }
    return null;
  }, [openId, lanes]);

  const pin = (id: string) => {
    if (route.roleId !== id) go(routeFor("board", { roleId: id, targetRole: target }));
  };
  const pickStage = (key: string) => {
    setQuery("");
    setStage(key);
    if (route.roleId) go(routeFor("board", { targetRole: target }));
  };

  /** The answer is said, and the role it names is opened; a new posting with no role yet shows in the stage it landed in. */
  const onJobAdded = (answer: RoleAdded) => {
    setAdded(answer.said);
    setAdding(false);
    setQuery("");
    if (answer.role) go(routeFor("board", { roleId: answer.role }));
    else {
      if (STAGE_KEYS.has(answer.stage)) setStage(answer.stage);
      if (route.roleId) go(routeFor("board", {}));
    }
  };

  const onRole = !!route.roleId;
  const listKey = searching ? `q:${search.q}` : `s:${stage}`;
  const heading = searching ? `${search.result?.total ?? 0} ${search.result?.total === 1 ? "match" : "matches"}` : (labels[stage] ?? railLabel(stage));
  const empty = searching
    ? search.status === "loading" ? "Searching…" : "Nothing matches. Try an employer or a word from the title."
    : stage === FAVORITES ? "Star a role to keep it here." : "Nothing here right now.";
  const said = launchWords(went);
  // A group not yet read (or not readable) has no list to draw: it says so rather than "nothing here".
  const waitingForGroup = !searching && isGroup(stage) && !lane;

  if (status === "error" && !Object.keys(lanes).length) {
    return (
      <div className="fi">
        <p className="fi-alert" role="alert">
          {error}{" "}
          <button type="button" className="fi-link" onClick={() => void store.reload()}>
            Try again
          </button>
        </p>
      </div>
    );
  }

  return (
    <div className={`fi${onRole ? " is-on-role" : ""}`}>
      <StageRail current={stage} counts={railCounts} hits={hits} labels={labels} onPick={pickStage} />
      <section className="fi-list" aria-label={searching ? "Search results" : heading}>
        <div className="fi-list-head">
          <label className="fi-find">
            <span className="fi-vh">Search every role</span>
            <FindIcon className="fi-find-icon" />
            <input type="search" value={query} placeholder="Search every role, any stage" autoComplete="off" onChange={(e) => setQuery(e.target.value)} />
          </label>
          <button type="button" className="fi-btn fi-add-open" aria-expanded={adding} aria-controls={addId} onClick={() => setAdding((v) => !v)}>
            <PlusIcon className="fi-btn-icon" />
            Add a job
          </button>
          {adding && <AddJob id={addId} add={store.addRole} onAdded={onJobAdded} onClose={() => setAdding(false)} />}
          {added && (
            <p className="fi-launch-said" role="status">
              {added}
            </p>
          )}
          {narrowed && (
            <p className="fi-filter">
              <span>Role: {narrowed.name ?? "one target role"}</span>
              <button type="button" className="fi-filter-off" aria-label="Remove the role filter" onClick={() => go(routeFor("board", { roleId: route.roleId }))}>
                <CrossIcon className="fi-btn-icon" />
              </button>
            </p>
          )}
          <div className="fi-list-title">
            <h1>{heading}</h1>
            {!searching && stage === "ready_to_launch" && <LaunchButton went={went} onLaunch={() => void store.launch()} />}
          </div>
          {!searching && stage === "ready_to_launch" && said && (
            <p className="fi-launch-said" role="status" title={said} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {said}
            </p>
          )}
          {searching && search.error && <p className="fi-alert" role="alert">{search.error}</p>}
          {notice && (
            <p className="fi-alert" role="alert">
              {notice}{" "}
              <button type="button" className="fi-link" onClick={store.dismissNotice}>
                Dismiss
              </button>
            </p>
          )}
        </div>
        {(status === "loading" && !lane && !searching) || (waitingForGroup && groupStatus[stage] !== "error") ? (
          <p className="fi-empty">Loading the pipeline…</p>
        ) : waitingForGroup ? (
          <p className="fi-empty">This list could not be read.</p>
        ) : (
          <RoleList rows={rows} listKey={listKey} currentId={openId} busy={busy} mark={searching ? search.q : ""} empty={empty} onOpen={pin} onStar={(id) => void store.toggleFavorite(id)} />
        )}
        {!searching && lane && lane.cards.length < lane.count && (
          <button type="button" className="fi-btn fi-more" disabled={lane.loadingMore} onClick={() => void store.loadMore(stage)}>
            {lane.loadingMore ? "Loading…" : `Show more (${lane.count - lane.cards.length} more)`}
          </button>
        )}
      </section>
      {(!narrow || onRole) && (
        <RolePane
          card={openCard}
          detail={openId ? details[openId] : undefined}
          endedReasons={endedReasons}
          busy={!!(openId && busy[openId])}
          backLabel={searching ? "Search" : (labels[stage] ?? railLabel(stage))}
          onBack={() => go(routeFor("board", { targetRole: target }))}
          onStar={() => {
            if (!openId) return;
            pin(openId);
            void store.toggleFavorite(openId);
          }}
          onMove={(to) => {
            if (!openId) return;
            pin(openId);
            void store.move(openId, to);
          }}
          onClose={(reason) => {
            if (!openId) return;
            pin(openId);
            void store.move(openId, closedStatusFor(reason), reason);
          }}
          onMoveBack={() => openId && void store.moveBack(openId)}
          onCorrect={(index, cls, why) => store.correctRequirement(openId!, index, cls, why)}
          onAskPacket={(feedback) => openId && void store.askPacket(openId, feedback)}
          onSavePacketFiles={(packetId) => openId && void store.savePacketFiles(openId, packetId)}
          onAddInterview={(body) => store.addInterview(openId!, body)}
          onEditInterview={(id, body) => store.editInterview(openId!, id, body)}
          onRemoveInterview={(id) => store.removeInterview(openId!, id)}
          onPrepareInterview={(id) => store.prepareInterview(openId!, id)}
          onRefresh={() => openId && void store.reloadRole(openId)}
        />
      )}
    </div>
  );
}
