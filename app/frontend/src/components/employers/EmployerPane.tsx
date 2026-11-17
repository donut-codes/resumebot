// The open employer (the screens rebuild spec, screen 2): its head (name, a meta line with its links and its board's
// last read), its actions in one row (Watch its job board, Hard pass with a reason, More: Edit facts, Merge, Split),
// then sheets: the facts strip, the rest of its facts (edited in place), its events, its roles, its hiring and its
// history. Everything comes from and goes through the employers store.

import { useEffect, useRef, useState, type ReactNode } from "react";
import type { Employer } from "../../api/types/employers";
import { dateInWords, dateTimeInWords, daysBetween, isBefore, ownerToday, shortDateInWords } from "../../lib/dates";
import { safeHref } from "../../lib/links";
import { roleAddress } from "../../lib/route";
import { stageFor } from "../../lib/stages";
import { useEmployers, useEmployersStore } from "../../state/employersStore";
import GrowingText from "../primitives/GrowingText";
import { ChevronIcon } from "../focus/icons";
import { FactStrip, Menu, PaneHead, Sheet, Switch, type Fact } from "../focus/parts";
import { MergeForm, SplitForm } from "./EmployerMove";
import { changesOf, draftOf, FIELDS, OWNERSHIP_WORDS, shownValue, WORK_STYLE_WORDS, yesNo, type Field } from "./employerFields";

/** The facts the strip shows; the facts sheet shows the rest. */
const IN_STRIP = new Set<keyof Employer>(["name", "headquarters", "employees", "industry", "local"]);
const CLOSED = new Set(["gated_out", "rejected", "gone_quiet", "withdrew"]);

const WATCH_WORDS: Record<string, string> = {
  waiting: "Starts tonight", learning: "Learning to read it", working: "Reading it nightly", needs_you: "Needs you",
  stopped: "Stopped",
};

/** A source in a few words: a web address is its site's name ("trueup.io"), anything else as it is. */
export function sourceName(source: string): string {
  try {
    const url = new URL(source);
    if (url.protocol === "http:" || url.protocol === "https:") return url.hostname.replace(/^www\./, "");
  } catch {
    // not an address
  }
  return source;
}

/** Where a fact came from, shown only for one the owner set or one older than the board's last read (the rest say it
 * on hover). */
export function sourceWords(e: Employer, key: string): { shown: string | null; hover: string | null } {
  const src = e.sources[key];
  if (!src) return { shown: null, hover: null };
  const words = `${src.source === "you" ? "set by you" : sourceName(src.source)}${src.at ? `, ${shortDateInWords(src.at)}` : ""}`;
  const older = !!(src.at && e.fetched_at && isBefore(src.at, e.fetched_at));
  return src.source === "you" || older ? { shown: words, hover: null } : { shown: null, hover: words };
}

function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noreferrer noopener">
      {children}
    </a>
  );
}

/** The head's meta line: what it is and where, its links, and when its board was last read. */
function metaOf(e: Employer): ReactNode[] {
  const out: ReactNode[] = [];
  const what = [e.industry, e.headquarters].filter(Boolean).join(" · ");
  if (what) out.push(what);
  if (safeHref(e.website)) out.push(<ExternalLink href={safeHref(e.website)!}>Website</ExternalLink>);
  if (safeHref(e.careers_url)) {
    out.push(
      <ExternalLink href={safeHref(e.careers_url)!}>{e.board_url === e.careers_url && e.ats_label ? `Careers (${e.ats_label})` : "Careers"}</ExternalLink>,
    );
  }
  if (safeHref(e.board_url) && e.board_url !== e.careers_url) out.push(<ExternalLink href={safeHref(e.board_url)!}>Job board ({e.ats_label})</ExternalLink>);
  if (e.fetched_at) {
    out.push(
      e.fetch_ok === false
        ? `Board read ${dateInWords(e.fetched_at)}: ${e.fetch_note ?? "it didn't answer"}`
        : `Board read ${dateInWords(e.fetched_at)}: ${e.open_roles ?? 0} open${e.fetch_note ? ` (${e.fetch_note})` : ""}`,
    );
  }
  return out;
}

/** Watch its job board: the switch, and what the watch is doing (in Where to look's words). */
function Watch({ employer, busy }: { employer: Employer; busy: boolean }) {
  const store = useEmployersStore();
  const site = employer.site;
  if (!site) return <span className="fi-emp-quiet">No careers page or job board is known for it yet.</span>;
  return <Switch checked={site.on} disabled={busy} label="Watch its job board" shown="Watch its job board" onChange={(on) => void store.watch(employer, on)} />;
}

function watchWords(employer: Employer): string | null {
  const site = employer.site;
  if (!site) return null;
  // A job system the owner switched off, or a site the app switched off because it can't be read, says so whatever the
  // site's state. A waiting site says whose turn it is tonight; one read in the owner's browser says where it is.
  const systemOff = site.status.startsWith("Off:");
  if (!site.on && !systemOff) return null;
  const words = systemOff || site.state === "waiting" || site.state === "browser" ? site.status : WATCH_WORDS[site.state];
  if (!words) return null;
  return `${words}${site.needs ? `: ${site.needs}` : ""}${site.on && site.watched_for === "in_play" ? ". Watched because it has a role in play" : ""}`;
}

/** Hard pass: a menu under the button asks why (optional) and confirms. On, it says so and offers to take it back. */
function HardPass({ employer, busy }: { employer: Employer; busy: boolean }) {
  const store = useEmployersStore();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => !box.current?.contains(e.target as Node) && setOpen(false);
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  if (employer.gated) {
    return (
      <>
        <span className="fi-closed-as">Hard pass{employer.gated_reason ? `: ${employer.gated_reason}` : ""}</span>
        <button type="button" className="fi-btn fi-btn-quiet" disabled={busy} onClick={() => void store.hardPass(employer, false)}>
          Take it back
        </button>
      </>
    );
  }
  return (
    <div className="fi-close" ref={box}>
      <button type="button" className="fi-btn" aria-expanded={open} aria-haspopup="dialog" disabled={busy} onClick={() => setOpen((o) => !o)}>
        Hard pass
        <ChevronIcon className="fi-btn-icon" />
      </button>
      {open && (
        <form
          className="fi-close-menu fi-emp-pass"
          role="dialog"
          aria-label="Hard pass on its roles"
          ref={(form) => form?.querySelector("input")?.focus()}
          onSubmit={(ev) => {
            ev.preventDefault();
            setOpen(false);
            void store.hardPass(employer, true, reason.trim());
          }}
        >
          <label className="fi-close-head" htmlFor="hard-pass-reason">
            Hard pass, because
          </label>
          <input id="hard-pass-reason" className="fi-input" type="text" placeholder="Why (optional)" value={reason} onChange={(ev) => setReason(ev.target.value)} />
          <button type="submit" className="fi-btn fi-btn-go">
            Hard pass
          </button>
        </form>
      )}
    </div>
  );
}

function FieldInput({ field, value, onChange }: { field: Field; value: string; onChange: (v: string) => void }) {
  const id = `employer-${field.key}`;
  let input: ReactNode;
  if (field.kind === "yesno") {
    input = (
      <select id={id} className="fi-input" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">Not known</option>
        <option value="yes">Yes</option>
        <option value="no">No</option>
      </select>
    );
  } else if (field.kind === "style" || field.kind === "ownership") {
    const words = field.kind === "style" ? WORK_STYLE_WORDS : OWNERSHIP_WORDS;
    input = (
      <select id={id} className="fi-input" value={value} onChange={(e) => onChange(e.target.value)}>
        {Object.entries(words).map(([k, w]) => (
          <option key={k} value={k}>
            {w}
          </option>
        ))}
      </select>
    );
  } else if (field.kind === "long") {
    input = <GrowingText id={id} className="fi-input" minRows={3} value={value} onText={onChange} />;
  } else {
    input = (
      <input
        id={id}
        className="fi-input"
        type={field.kind === "number" ? "number" : "text"}
        value={value}
        placeholder={field.kind === "list" ? "Separate with semicolons" : undefined}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  return (
    <div className="fi-field">
      <label htmlFor={id}>{field.label}</label>
      {input}
    </div>
  );
}

/** Every fact, as a form: Save sends only what changed (each becomes the owner's). */
function FactsForm({ employer, onDone }: { employer: Employer; onDone: () => void }) {
  const store = useEmployersStore();
  const [draft, setDraft] = useState(() => draftOf(employer));
  const [busy, setBusy] = useState(false);
  return (
    <form
      className="fi-card fi-emp-form"
      aria-label="Edit its facts"
      onSubmit={(ev) => {
        ev.preventDefault();
        setBusy(true);
        void store.editFacts(employer.id, changesOf(employer, draft)).then((ok) => {
          setBusy(false);
          if (ok) onDone();
        });
      }}
    >
      <div className="fi-emp-fields">
        {FIELDS.map((f) => (
          <FieldInput key={f.key} field={f} value={draft[f.key]} onChange={(v) => setDraft({ ...draft, [f.key]: v })} />
        ))}
      </div>
      <div className="fi-actions">
        <button type="submit" className="fi-btn fi-btn-go" disabled={busy}>
          Save
        </button>
        <button type="button" className="fi-btn fi-btn-quiet" onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  );
}

/** The facts strip: where it is, how big, what it does, how it posts, whether it has a local office. */
function stripOf(e: Employer): Fact[] {
  const fact = (label: string, key: string, value: string): Fact => {
    const src = sourceWords(e, key);
    // The whole value on hover (a long one is cut at two lines), with where it came from when that is not shown.
    return { label, value: <span title={src.hover ? `${value} (${src.hover})` : value}>{value}</span>, sub: src.shown };
  };
  const system: Fact = {
    label: "Job system",
    value: e.ats ? (e.ats_label ?? e.ats) : "Not known",
    sub: e.ats && e.ats_confirmed === false ? "found under its name, not confirmed" : null,
  };
  return [
    fact("Headquarters", "headquarters", e.headquarters ?? "–"),
    fact("People", "employees", e.employees != null ? e.employees.toLocaleString() : "–"),
    fact("Industry", "industry", e.industry ?? "–"),
    system,
    fact("Local office", "local", yesNo(e.local)),
  ];
}

/** Whether its board has been counted for less than the span the figures cover. */
function countingSince(h: NonNullable<Employer["hiring"]>, today: string = ownerToday()): boolean {
  return h.since !== null && daysBetween(h.since, today) < h.days;
}

function HiringLine({ hiring: h }: { hiring: NonNullable<Employer["hiring"]> }) {
  const young = countingSince(h);
  return (
    <p className="fi-emp-line">
      {h.open} open now
      {young ? `; counting since ${dateInWords(h.since!)}, ${h.closed} closed so far` : `; ${h.opened} opened and ${h.closed} closed in the last ${h.days} days`}
      <span className="fi-emp-quiet"> (counted from its board)</span>.
    </p>
  );
}

type Mode = "facts" | "merge" | "split" | null;

/** The open employer. `onMoved` is told the employer to look at next after a merge or split. */
export default function EmployerPane({ employer: e, onMoved }: { employer: Employer; onMoved: (to: Employer) => void }) {
  const busy = useEmployers((s) => !!s.busy[e.id]);
  const [mode, setMode] = useState<Mode>(null);
  const roles = e.roles ?? [];
  const rest = FIELDS.filter((f) => !IN_STRIP.has(f.key));
  const watching = watchWords(e);
  const moved = (to: Employer) => {
    setMode(null);
    onMoved(to);
  };

  return (
    <>
      <PaneHead title={e.name} meta={metaOf(e)} />
      <div className="fi-actions" role="group" aria-label="What to do with it">
        <Watch employer={e} busy={busy} />
        <HardPass employer={e} busy={busy} />
        <Menu
          label="More"
          heading="More"
          disabled={busy}
          items={[
            { key: "facts", label: "Edit facts" },
            { key: "merge", label: "Merge into another employer" },
            { key: "split", label: "Split roles out" },
          ]}
          onPick={(key) => setMode(key as Mode)}
        />
      </div>
      {watching && <p className="fi-emp-watching">{watching}</p>}
      {mode === "merge" && <MergeForm employer={e} onMerged={moved} onCancel={() => setMode(null)} />}
      {mode === "split" && <SplitForm employer={e} onSplit={moved} onCancel={() => setMode(null)} />}

      <FactStrip facts={stripOf(e)} />

      <Sheet title="Facts">
        {mode === "facts" ? (
          <FactsForm employer={e} onDone={() => setMode(null)} />
        ) : (
          <dl className="fi-emp-facts">
            {rest.map((f) => {
              const src = sourceWords(e, f.key);
              return (
                <div key={f.key}>
                  <dt>{f.label}</dt>
                  <dd title={src.hover ?? undefined}>
                    {shownValue(e, f)}
                    {src.shown && <span className="fi-emp-src">{src.shown}</span>}
                  </dd>
                </div>
              );
            })}
          </dl>
        )}
      </Sheet>

      {e.events.length > 0 && (
        <Sheet title="Events" count={e.events.length}>
          <ul className="fi-emp-lines">
            {e.events.map((ev, i) => (
              <li key={i}>
                <div className="fi-emp-quiet">
                  {ev.date} · {ev.kind}
                </div>
                {ev.note && <div>{ev.note}</div>}
              </li>
            ))}
          </ul>
        </Sheet>
      )}

      <Sheet title="Roles" count={roles.length}>
        {roles.length === 0 ? (
          <p className="fi-emp-quiet">No roles from here yet.</p>
        ) : (
          <ul className="fi-emp-lines">
            {roles.map((r) => {
              const stage = stageFor(r.status);
              const cls = r.status === "ready_to_launch" ? " is-yours" : CLOSED.has(r.status) ? " is-closed" : "";
              return (
                <li key={r.id} className="fi-emp-role">
                  <div>
                    <a className="fi-emp-role-title" href={roleAddress(r.id)}>
                      {r.title}
                    </a>
                    {r.fit != null && <div className="fi-emp-quiet">Fit {r.fit}</div>}
                  </div>
                  <span className={`fi-badge fi-badge-stage${cls}`}>{stage?.label ?? r.status.replace(/_/g, " ")}</span>
                </li>
              );
            })}
          </ul>
        )}
      </Sheet>

      {e.hiring && (
        <Sheet title="Hiring">
          <HiringLine hiring={e.hiring} />
        </Sheet>
      )}

      {(e.history ?? []).length > 0 && (
        <section className="fi-sec">
          <h3>History</h3>
          <ul className="fi-hist">
            {(e.history ?? []).map((h, i) => (
              <li key={i}>
                <small>{dateTimeInWords(h.at)}</small>
                {h.sentence}
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
