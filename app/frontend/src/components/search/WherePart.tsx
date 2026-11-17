// Where to look (the screens rebuild spec, screen 3): the sites the search reads, the job systems employers post
// through, and the sites to avoid. The list holds them under small heads, each with its switch; watching every employer
// with a role in play is its first line; Add a site is its action. The pane holds the open one: why it stands as it
// does, the roles it brought, how it is read, and (for a site to avoid) taking it off. Everything comes from the search
// store; nothing here calls the API.

import { useCallback, useId, useState, type ReactNode } from "react";
import type { JobSystem, Site, SiteList, SiteReadBy, TermsEntry } from "../../api/types/where";
import { errorMessage } from "../../api/client";
import { shortDateInWords } from "../../lib/dates";
import { hostOf } from "../../lib/links";
import { blankRoute, routeFor, serializeRoute } from "../../lib/route";
import { effectiveRead } from "../../lib/sites";
import { lowercaseFirst } from "../../lib/text";
import { useSearch, useSearchStore } from "../../state/searchStore";
import { FactStrip, ListHead, Pane, PaneHead, Row, Sheet, Switch } from "../focus/parts";
import RolesFound from "./RolesFound";
import { TermsNote, termsFor, termsForHosts } from "./termsNote";
import type { PartProps } from "./SearchScreen";

/** The three ways a site is read, each with what it is for and what it costs. The first is the default. */
const READ_BY: { key: SiteReadBy; label: string; line: string }[] = [
  {
    key: "auto",
    label: "Ask resumebot to find out",
    line: "Tried directly first, which uses no Claude allowance; if that doesn't work, read in your browser, which does.",
  },
  { key: "app", label: "From the server", line: "Its public pages, read directly, using no Claude allowance. For sites anyone can see without signing in." },
  { key: "browser", label: "In my browser", line: "Read in your own signed-in Chrome by an agent, so it uses your Claude allowance and your computer has to be on." },
];

const href = (url: string) => (/^[a-z][a-z0-9+.-]*:\/\//i.test(url) ? url : `https://${url}`);
const nameIsAddress = (s: Pick<Site, "label" | "url">) => hostOf(s.url).toLowerCase() === s.label.trim().toLowerCase();

/** A site's status in a word or a line: Error (the app switched it off), Off, or the server's own words. */
function statusOf(site: Site): string {
  if (!site.on && site.off_by === "app") return "Error";
  if (!site.on && site.state === "off") return "Off";
  return site.status;
}

const roles = (n: number) => (n === 1 ? "1 role found" : `${n.toLocaleString()} roles found`);

/** The open item: a site, a job system, or the form that adds a site. */
type Open = { kind: "site"; site: Site } | { kind: "system"; system: JobSystem } | { kind: "add" } | null;

export default function WherePart({ open, onOpen, narrow }: PartProps) {
  const store = useSearchStore();
  const page = useSearch((s) => s.sites);
  const status = useSearch((s) => s.sitesStatus);
  const error = useSearch((s) => s.sitesError);
  const notice = useSearch((s) => s.notice);
  const [q, setQ] = useState("");
  const [onlyOn, setOnlyOn] = useState(false);

  if (!page) {
    return (
      <section className="fi-list" aria-label="Where to look">
        <ListHead title="Where to look" />
        {status === "error" ? (
          <p className="fi-alert" role="alert">
            {error}{" "}
            <button type="button" className="fi-link" onClick={() => void store.reload()}>
              Try again
            </button>
          </p>
        ) : (
          <p className="fi-empty">Loading…</p>
        )}
      </section>
    );
  }

  const t = q.trim().toLowerCase();
  const hit = (...words: string[]) => !t || words.some((w) => w.toLowerCase().includes(t));
  const byRoles = <T,>(rows: T[], n: (r: T) => number, name: (r: T) => string) => [...rows].sort((a, b) => n(b) - n(a) || name(a).localeCompare(name(b)));
  // The sites to search: the ones the owner added and the companies they watch (one that is off is not listed, unless the
  // app switched it off because it can't be read: the owner switches it on again here).
  const searching = byRoles(
    page.sites.filter((s) => s.list === "search" && (!s.employer_id || s.on || s.off_by === "app") && (!onlyOn || s.on) && hit(s.label, s.url)),
    (s) => s.roles,
    (s) => s.label,
  );
  const own = searching.filter((s) => !s.by_code);
  const byCode = searching.filter((s) => s.by_code);
  const systems = byRoles(page.job_systems.filter((j) => (!onlyOn || j.on) && hit(j.name, j.key)), (j) => j.roles, (j) => j.name);
  const avoided = page.sites.filter((s) => s.list === "avoid" && hit(s.label, s.url)).sort((a, b) => a.label.localeCompare(b.label));

  const all = [...own, ...byCode];
  const find = (key: string | null): Open => {
    if (!key) return null;
    if (key === "add") return { kind: "add" };
    const [kind, id] = [key.slice(0, key.indexOf(":")), key.slice(key.indexOf(":") + 1)];
    if (kind === "system") {
      const system = page.job_systems.find((j) => j.key === id);
      return system ? { kind: "system", system } : null;
    }
    const site = page.sites.find((s) => s.id === id);
    return site ? { kind: "site", site } : null;
  };
  // The one the owner opened, else (on a wide window) the first one listed.
  const firstKey = all[0] ? `site:${all[0].id}` : systems[0] ? `system:${systems[0].key}` : avoided[0] ? `site:${avoided[0].id}` : null;
  const openKey = open ?? (narrow ? null : firstKey);
  const item = find(openKey);

  const terms = page.terms;
  const siteRow = (s: Site) => (
    <Row
      key={s.id}
      title={s.list === "avoid" ? hostOf(s.url) : s.label}
      meta={
        s.list === "avoid"
          ? [<span key="added">Added {shortDateInWords(s.created_at)}</span>]
          : [
              statusOf(s) && <span key="status">{statusOf(s)}</span>,
              <span key="roles">{roles(s.roles)}</span>,
              <TermsNote key="terms" entry={termsFor(s.url, terms)} />,
            ].filter(Boolean)
      }
      lead={<span className={`fi-fit${s.roles === 0 ? " is-none" : ""}`} aria-hidden="true">{s.list === "avoid" ? "×" : s.roles}</span>}
      end={s.list === "search" ? <Switch checked={s.on} label={`Look here: ${s.label}`} onChange={(on) => void store.setSite(s, on)} /> : null}
      current={openKey === `site:${s.id}`}
      onOpen={() => onOpen(`site:${s.id}`)}
    />
  );

  const head = (label: string, n: number) => (
    <li className="fi-ss-group" key={`head-${label}`}>
      <span>{label}</span>
      <span>{n}</span>
    </li>
  );

  return (
    <>
      <section className="fi-list" aria-label="Where to look">
        <ListHead
          find={{ value: q, onChange: setQ, placeholder: "Find a site", label: "Find a site" }}
          title="Where to look"
          action={
            <button type="button" className="fi-btn" aria-pressed={openKey === "add"} onClick={() => onOpen("add")}>
              Add a site
            </button>
          }
        >
          <Switch checked={onlyOn} label="Switched on only" shown="Switched on only" onChange={setOnlyOn} />
          {notice && (
            <p className="fi-alert" role="alert">
              {notice}{" "}
              <button type="button" className="fi-link" onClick={store.dismissNotice}>
                Dismiss
              </button>
            </p>
          )}
        </ListHead>
        <ul className="fi-rows">
          <li className="fi-ss-line">
            <span id="fi-ss-watch">Watch the job board of every employer with a role in play</span>
            <Switch
              checked={page.watch_in_play ?? false}
              label="Watch the job board of every employer with a role in play"
              onChange={(on) => void store.setWatchInPlay(on)}
            />
          </li>
          {head("Sites to search", own.length)}
          {own.length === 0 && <li className="fi-ss-none">{t ? "None match." : "No sites yet. Add one, or watch an employer from Employers."}</li>}
          {own.map(siteRow)}
          {byCode.length > 0 && head("Read by API", byCode.length)}
          {byCode.map(siteRow)}
          {page.job_systems.length > 0 && head("Job systems", systems.length)}
          {systems.map((j) => (
            <Row
              key={j.key}
              title={j.name}
              meta={[
                j.status && <span key="status">{j.status}</span>,
                <span key="roles">{roles(j.roles)}</span>,
                <TermsNote key="terms" entry={termsForHosts(j.domains, terms)} />,
              ].filter(Boolean)}
              lead={<span className={`fi-fit${j.roles === 0 ? " is-none" : ""}`} aria-hidden="true">{j.roles}</span>}
              end={j.can_read ? <Switch checked={j.on} label={`Look here: ${j.name}`} onChange={(on) => void store.setJobSystem(j, on)} /> : null}
              current={openKey === `system:${j.key}`}
              onOpen={() => onOpen(`system:${j.key}`)}
            />
          ))}
          {head("Sites to avoid", avoided.length)}
          {avoided.length === 0 && <li className="fi-ss-none">{t ? "None match." : "Nothing to avoid yet."}</li>}
          {avoided.map(siteRow)}
        </ul>
      </section>
      {(!narrow || open) && (
        <Pane
          openKey={openKey ?? "none"}
          label={item?.kind === "site" ? item.site.label : item?.kind === "system" ? item.system.name : item?.kind === "add" ? "Add a site" : "Where to look"}
          backLabel="Where to look"
          onBack={() => onOpen(null)}
        >
          {item?.kind === "site" ? (
            <SitePane site={item.site} terms={terms} />
          ) : item?.kind === "system" ? (
            <SystemPane system={item.system} terms={terms} />
          ) : item?.kind === "add" ? (
            <AddSite terms={terms} onAdded={(site) => onOpen(`site:${site.id}`)} onCancel={() => onOpen(null)} />
          ) : (
            <p className="fi-pane-note">Pick a site to see it here.</p>
          )}
        </Pane>
      )}
    </>
  );
}

function SitePane({ site, terms }: { site: Site; terms: TermsEntry[] | undefined }) {
  const store = useSearchStore();
  const trying = useSearch((s) => s.trying === site.id);
  const [removing, setRemoving] = useState(false);
  const load = useCallback((offset: number, limit: number) => store.siteRoles(site.id, offset, limit), [store, site.id]);
  const avoid = site.list === "avoid";
  const read = effectiveRead(site);
  // A company's own site is read by the app; the choice shows where it can matter.
  const canChoose = !avoid && (!site.employer_id || read === "browser" || site.state === "needs_you");
  const reason = (site.state === "needs_you" || site.state === "stopped") && site.needs;
  const appOff = !site.on && site.off_by === "app";
  const meta: ReactNode[] = [
    !nameIsAddress(site) && (
      <a key="url" href={href(site.url)} target="_blank" rel="noreferrer noopener">
        {hostOf(site.url)}
      </a>
    ),
    site.employer_id && (
      <a key="emp" href={serializeRoute(routeFor("employers", { employerId: site.employer_id }))}>
        Employer page
      </a>
    ),
    site.state === "needs_you" && (
      <a key="inbox" href={serializeRoute(blankRoute("inbox"))}>
        Open Notifications
      </a>
    ),
  ];
  return (
    <>
      <PaneHead title={avoid ? hostOf(site.url) : site.label} meta={meta} />
      {!avoid && <p className="fi-body"><TermsNote entry={termsFor(site.url, terms)} /></p>}
      {avoid ? (
        <div className="fi-actions">
          <button
            type="button"
            className="fi-btn"
            disabled={removing}
            onClick={async () => {
              setRemoving(true);
              if (!(await store.removeSite(site))) setRemoving(false);
            }}
          >
            {removing ? "Taking it off…" : "Take it off the list"}
          </button>
        </div>
      ) : (
        <div className="fi-actions">
          <Switch checked={site.on} label={`Look here: ${site.label}`} shown="Look here" onChange={(on) => void store.setSite(site, on)} />
        </div>
      )}
      <FactStrip
        facts={[
          ...(avoid ? [] : [{ label: "Status", value: statusOf(site) || "Waiting" }, { label: "Roles found", value: site.roles.toLocaleString() }]),
          { label: "Added", value: shortDateInWords(site.created_at) },
          ...(avoid ? [] : [{ label: "Read", value: read === "browser" ? "In your browser" : read === "app" ? "From the server" : "Not tested yet" }]),
        ]}
      />
      {avoid && <p className="fi-body">Never read. A posting that links to this site is stopped, and says so.</p>}
      {appOff && <p className="fi-body">{`resumebot turned this off, because it couldn't be read. Turn it back on above if that changes${site.off_at ? ` (${shortDateInWords(site.off_at)})` : ""}.`}</p>}
      {reason && <p className="fi-body">{site.needs}</p>}
      {!avoid && (
        <Sheet title="Roles it brought" count={site.roles || undefined}>
          <RolesFound load={load} total={site.roles} />
        </Sheet>
      )}
      {canChoose && (
        <Sheet title="How it is read">
          <ReadBy value={trying ? "auto" : site.read_by} disabled={trying} onChange={(readBy) => readBy !== site.read_by && void store.setReadBy(site, readBy)} />
          {trying ? (
            <p className="fi-ss-quiet" role="status">
              Trying the site…
            </p>
          ) : (
            site.read_note &&
            (site.read_by === "auto" || read === "browser") && <p className="fi-ss-quiet">{`Found out: ${lowercaseFirst(site.read_note)}`}</p>
          )}
        </Sheet>
      )}
    </>
  );
}

function SystemPane({ system, terms }: { system: JobSystem; terms: TermsEntry[] | undefined }) {
  const store = useSearchStore();
  const load = useCallback((offset: number, limit: number) => store.jobSystemRoles(system.key, offset, limit), [store, system.key]);
  return (
    <>
      <PaneHead title={system.name} meta={[system.status]} />
      {termsForHosts(system.domains, terms) && <p className="fi-body"><TermsNote entry={termsForHosts(system.domains, terms)} /></p>}
      {system.can_read && (
        <div className="fi-actions">
          <Switch checked={system.on} label={`Look here: ${system.name}`} shown="Look here" onChange={(on) => void store.setJobSystem(system, on)} />
        </div>
      )}
      {system.employer_names.length > 0 && (
        <Sheet title={system.can_read ? "Read for" : "Employers you watch on it, not read"} count={system.employer_names.length}>
          <p className="fi-ss-names">
            {system.employer_names.map((e, i) => (
              <span key={e.id}>
                {i > 0 && ", "}
                <a href={serializeRoute(routeFor("employers", { employerId: e.id }))}>{e.name}</a>
              </span>
            ))}
          </p>
        </Sheet>
      )}
      {(system.can_read || system.roles > 0) && (
        <Sheet title="Roles it brought" count={system.roles || undefined}>
          <RolesFound load={load} total={system.roles} />
        </Sheet>
      )}
    </>
  );
}

/** One choice of several: a radio, its name, and the one line of what it means. */
function Choice({ name, checked, onPick, label, line }: { name: string; checked: boolean; onPick: () => void; label: string; line?: string }) {
  const id = useId();
  return (
    <div className="fi-ss-choice">
      <input id={id} type="radio" name={name} checked={checked} onChange={onPick} />
      <label htmlFor={id}>
        <strong>{label}</strong>
        {line && <span>{line}</span>}
      </label>
    </div>
  );
}

/** How a site is read: three choices, each with its one line of what it costs. */
function ReadBy({ value, onChange, disabled }: { value: SiteReadBy; onChange: (readBy: SiteReadBy) => void; disabled?: boolean }) {
  const name = useId();
  return (
    <fieldset className="fi-ss-choices" disabled={disabled}>
      <legend className="fi-vh">How should resumebot read it?</legend>
      {READ_BY.map((c) => (
        <Choice key={c.key} name={name} checked={value === c.key} onPick={() => onChange(c.key)} label={c.label} line={c.line} />
      ))}
    </fieldset>
  );
}

/** Adds a site by its address, to search (read the way chosen) or to avoid. */
function AddSite({ terms, onAdded, onCancel }: { terms: TermsEntry[] | undefined; onAdded: (site: Site) => void; onCancel: () => void }) {
  const store = useSearchStore();
  const urlId = useId();
  const listName = useId();
  const [url, setUrl] = useState("");
  const [list, setList] = useState<SiteList>("search");
  const [readBy, setReadBy] = useState<SiteReadBy>("auto");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const add = async () => {
    setBusy(true);
    setError(null);
    try {
      onAdded(await store.addSite(url.trim(), list, list === "search" ? readBy : "app"));
    } catch (err) {
      setError(errorMessage(err, "Couldn't add that site."));
      setBusy(false);
    }
  };
  return (
    <>
      <PaneHead title="Add a site" />
      <form
        className="fi-ss-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (url.trim()) void add();
        }}
      >
        <div className="fi-field">
          <label htmlFor={urlId}>Site address</label>
          <input id={urlId} className="fi-input" type="text" inputMode="url" placeholder="jobs.example.org" value={url} onChange={(e) => setUrl(e.target.value)} />
          {list === "search" && termsFor(url, terms) && <p className="fi-body"><TermsNote entry={termsFor(url, terms)} /></p>}
        </div>
        <fieldset className="fi-ss-choices">
          <legend>What should resumebot do with it?</legend>
          <Choice name={listName} checked={list === "search"} onPick={() => setList("search")} label="Look at this site" />
          <Choice name={listName} checked={list === "avoid"} onPick={() => setList("avoid")} label="Avoid this site" line="Never read, and postings that link to it are stopped." />
        </fieldset>
        {list === "search" && (
          <div className="fi-field">
            <span className="fi-ss-label">How should resumebot read it?</span>
            <ReadBy value={readBy} onChange={setReadBy} />
          </div>
        )}
        {error && (
          <p className="fi-alert" role="alert">
            {error}
          </p>
        )}
        <div className="fi-actions">
          <button type="submit" className="fi-btn fi-btn-go" disabled={busy || !url.trim()}>
            {busy ? "Adding…" : "Add"}
          </button>
          <button type="button" className="fi-btn fi-btn-quiet" onClick={onCancel}>
            Cancel
          </button>
          {busy && list === "search" && readBy === "auto" && (
            <span className="fi-ss-quiet" role="status">
              Trying the site…
            </span>
          )}
        </div>
      </form>
    </>
  );
}
