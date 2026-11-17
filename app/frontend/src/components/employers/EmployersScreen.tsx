// Employers (the screens rebuild spec, screen 2): see the companies roles come from, and decide which to watch, pass
// on, merge or split. The rail holds the views with their counts, the list a page of employers at a time (more as it
// scrolls), and the pane the open one. Everything comes from the employers store, which follows the live stream; nothing
// here calls the API.

import { useEffect, useRef } from "react";
import type { Employer, EmployerView } from "../../api/types/employers";
import { useRefreshKey, useRoute } from "../../lib/appContext";
import { shortDateInWords } from "../../lib/dates";
import { SAID_TOAST_MS } from "../../lib/useDraftPage";
import { routeFor } from "../../lib/route";
import { NARROW, useMedia } from "../../lib/useMedia";
import { useEmployers, useEmployersStore } from "../../state/employersStore";
import { useToast } from "../toast/ToastProvider";
import { ListHead, Pane, Rail, RailEntry, RailGroup, Row, Switch } from "../focus/parts";
import EmployerPane from "./EmployerPane";

export const VIEWS: { key: EmployerView; label: string }[] = [
  { key: "open", label: "With open roles" },
  { key: "watched", label: "Watched" },
  { key: "local", label: "Has a local office" },
  { key: "hard_pass", label: "Hard pass" },
];
const ALL = { key: "all" as const, label: "All employers" };

const EMPTY: Record<EmployerView, string> = {
  open: "No employer has an open role right now.",
  watched: "You watch no employer's job board yet.",
  local: "No employer is known to have a local office.",
  hard_pass: "No employer is on your hard pass list.",
  all: "No employers yet. They are made as the search finds roles.",
};

/** A row's meta line: what it does, how big, how it posts, when its board was last read. */
export function rowMeta(e: Employer): string[] {
  return [
    e.industry,
    e.employees != null ? `${e.employees.toLocaleString()} people` : null,
    e.ats ? e.ats_label : null,
    e.fetched_at ? `Board read ${shortDateInWords(e.fetched_at)}` : null,
  ].filter((m): m is string => !!m);
}

/** The open-role count at the row's start: washed when a role is in Ready to Launch or later, faint at none. */
function Chip({ e }: { e: Employer }) {
  const n = e.roles_open ?? 0;
  const cls = n === 0 ? " is-none" : (e.roles_ahead ?? 0) > 0 ? " is-high" : "";
  return (
    <span className={`fi-fit${cls}`} role="img" aria-label={`${n} open ${n === 1 ? "role" : "roles"}`}>
      {n}
    </span>
  );
}

export default function EmployersScreen() {
  const store = useEmployersStore();
  const { route, go } = useRoute();
  const narrow = useMedia(NARROW);
  const { showToast } = useToast();
  const more = useRef<HTMLLIElement>(null);

  const status = useEmployers((s) => s.status);
  const error = useEmployers((s) => s.error);
  const view = useEmployers((s) => s.view);
  const q = useEmployers((s) => s.q);
  const industry = useEmployers((s) => s.industry);
  const rows = useEmployers((s) => s.rows);
  const next = useEmployers((s) => s.next);
  const total = useEmployers((s) => s.total);
  const loadingMore = useEmployers((s) => s.loadingMore);
  const counts = useEmployers((s) => s.counts);
  const industries = useEmployers((s) => s.industries);
  const open = useEmployers((s) => s.open);
  const busy = useEmployers((s) => s.busy);
  const notice = useEmployers((s) => s.notice);
  const said = useEmployers((s) => s.said);

  useEffect(() => store.start(), [store]);
  // Clicking the tab you are on reads it again.
  const refreshKey = useRefreshKey();
  useEffect(() => {
    if (refreshKey > 0) void store.reload();
  }, [refreshKey, store]);

  // What a hard pass changed, said once.
  useEffect(() => {
    if (!said) return;
    showToast(said, SAID_TOAST_MS);
    store.dismissSaid();
  }, [said, showToast, store]);

  // The employer the address names, else (on a wide window) the first row.
  const picked = route.employerId;
  const openId = picked ?? (narrow ? null : (rows[0]?.id ?? null));
  useEffect(() => {
    if (openId !== store.getState().open.id) void store.openEmployer(openId);
  }, [openId, store]);

  // More rows as the list's end comes into view.
  useEffect(() => {
    const end = more.current;
    if (!end || !next || typeof IntersectionObserver === "undefined") return;
    const watcher = new IntersectionObserver((seen) => seen.some((s) => s.isIntersecting) && void store.loadMore());
    watcher.observe(end);
    return () => watcher.disconnect();
  }, [next, store, rows.length]);

  const pickView = (key: EmployerView) => {
    go(routeFor("employers"));
    void store.setView(key);
  };
  const openRow = (e: Employer) => go(routeFor("employers", { employerId: e.id }));
  const label = view === "all" ? ALL.label : (VIEWS.find((v) => v.key === view)?.label ?? ALL.label);
  const onItem = !!picked;

  if (status === "error" && rows.length === 0) {
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

  const showPane = !narrow || onItem;
  // No count until the first answer: a count rolls when it changes, not up from nothing on every visit.
  const counted = status === "ready" || rows.length > 0;
  const employer = open.employer && open.employer.id === openId ? open.employer : null;

  return (
    <div className={`fi fi-emp${onItem ? " is-on-role" : ""}`}>
      <Rail label="Employer views">
        <RailGroup>
          {VIEWS.map((v) => (
            <RailEntry key={v.key} label={v.label} count={counted ? counts[v.key] : null} current={view === v.key} onPick={() => pickView(v.key)} />
          ))}
        </RailGroup>
        <RailGroup label="Everything">
          <RailEntry label={ALL.label} count={counted ? counts.all : null} current={view === "all"} onPick={() => pickView("all")} />
        </RailGroup>
      </Rail>
      <section className="fi-list" aria-label={label}>
        <ListHead
          find={{ value: q, onChange: (text) => void store.find(text), placeholder: "Find an employer", label: "Find an employer" }}
          title={label}
          action={<span className="fi-emp-total">{total === 1 ? "1 employer" : `${total.toLocaleString()} employers`}</span>}
        >
          <label className="fi-emp-industry">
            <span className="fi-vh">Industry</span>
            <select className="fi-input" value={industry} onChange={(ev) => void store.setIndustry(ev.target.value)}>
              <option value="">Any industry</option>
              {industries.map((i) => (
                <option key={i} value={i}>
                  {i}
                </option>
              ))}
            </select>
          </label>
          {notice && (
            <p className="fi-alert" role="alert">
              {notice}{" "}
              <button type="button" className="fi-link" onClick={store.dismissNotice}>
                Dismiss
              </button>
            </p>
          )}
        </ListHead>
        {status !== "ready" && rows.length === 0 ? (
          <p className="fi-empty">Loading…</p>
        ) : (
          <ul className="fi-rows">
            {rows.length === 0 && <li className="fi-empty">{q.trim() || industry ? "No employer matches." : EMPTY[view]}</li>}
            {rows.map((e) => (
              <Row
                key={e.id}
                title={
                  <>
                    {e.name}
                    {e.gated && <span className="fi-badge fi-emp-pass-badge">Hard pass</span>}
                  </>
                }
                meta={rowMeta(e).map((m) => (
                  <span key={m}>{m}</span>
                ))}
                lead={<Chip e={e} />}
                end={
                  e.site ? (
                    <Switch checked={e.site.on} disabled={!!busy[e.id]} label={`Watch ${e.name}'s job board`} onChange={(on) => void store.watch(e, on)} />
                  ) : null
                }
                current={e.id === openId}
                onOpen={() => openRow(e)}
              />
            ))}
            {next && (
              <li ref={more} className="fi-emp-more">
                <button type="button" className="fi-btn fi-btn-quiet" disabled={loadingMore} onClick={() => void store.loadMore()}>
                  {loadingMore ? "Loading…" : "Show more"}
                </button>
              </li>
            )}
          </ul>
        )}
      </section>
      {showPane && (
        <Pane openKey={openId ?? "none"} label={employer ? employer.name : label} backLabel={label} onBack={() => go(routeFor("employers"))}>
          {employer ? (
            <EmployerPane employer={employer} onMoved={(to) => go(routeFor("employers", { employerId: to.id }))} />
          ) : open.status === "error" && open.id === openId ? (
            <p className="fi-alert" role="alert">
              {open.error}
            </p>
          ) : (
            <p className="fi-pane-note">{openId ? "Loading…" : rows.length ? "Pick an employer to see it here." : ""}</p>
          )}
        </Pane>
      )}
    </div>
  );
}
