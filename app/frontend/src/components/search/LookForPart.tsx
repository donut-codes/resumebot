// What to look for (the screens rebuild spec, screen 3): the kind of employer, shared by every role, and each target
// role. The list holds Company, the roles (each with the roles found for it this week) and, when there are any, the roles
// and postings that fit none of them; Add a role is its action. The pane edits the open one, each field saving itself,
// with Paste a job posting and Check my roles as its actions. Everything comes from the search store.

import { useCallback, useEffect, useId, useState } from "react";
import { errorMessage } from "../../api/client";
import type { Criteria, FitsNone, FitsNoneItem, ParsedRole, TargetRole } from "../../api/types/profile";
import { shortDateInWords } from "../../lib/dates";
import { roleAddress } from "../../lib/route";
import { stageFor } from "../../lib/stages";
import { useSearch, useSearchStore } from "../../state/searchStore";
import { Field, ListHead, Pane, PaneHead, Row, Sheet } from "../focus/parts";
import CheckRolesPanel from "../profile/CheckRolesPanel";
import ExamplePanel from "../profile/ExamplePanel";
import { blankRole, MAX_TARGET_ROLES, MAX_TITLES, mergeParsed, newRoleId, normalizeRole, titlesTyped } from "../profile/targetRoles";
import { HistorySheet, SaveHead } from "./common";
import type { PartProps } from "./SearchScreen";

/** How many of a fits-none list show at first, and how many more each Show more adds. */
export const FITS_PAGE = 10;

const found = (n: number | undefined) => (n == null ? null : n === 1 ? "1 found this week" : `${n} found this week`);

const ROLES_FULL = `You have ${MAX_TARGET_ROLES} target roles, the most there is room for. Delete one to add another.`;

/** A role's text parts, in the order a posting reads, each with its name on the page. */
const PARTS: { key: "description" | "responsibilities" | "qualifications" | "preferred" | "compensation"; label: string }[] = [
  { key: "description", label: "Description" },
  { key: "responsibilities", label: "Responsibilities" },
  { key: "qualifications", label: "Qualifications" },
  { key: "preferred", label: "Preferred" },
  { key: "compensation", label: "Benefits / Compensation" },
];

export default function LookForPart({ open, onOpen, narrow }: PartProps) {
  const store = useSearchStore();
  const criteria = useSearch((s) => s.profile?.criteria) as Criteria;
  const foundWeek = useSearch((s) => s.foundWeek);
  const [fits, setFits] = useState<FitsNone | null>(null);
  const [limit, setLimit] = useState(FITS_PAGE);
  const [seen, setSeen] = useState(0);
  const roles = (criteria.target_roles ?? []).map(normalizeRole);
  const full = roles.length >= MAX_TARGET_ROLES;

  useEffect(() => {
    let live = true;
    store.fitsNone(limit).then(
      (f) => live && setFits(f),
      () => live && setFits(null),
    );
    return () => {
      live = false;
    };
  }, [store, limit, seen]);

  const fitsCount = (fits?.pursued.count ?? 0) + (fits?.this_week.count ?? 0);
  const firstKey = roles[0] ? `role:${roles[0].id}` : "company";
  // A role the address names that is not there (deleted, or a link from before) is no role to open.
  const gone = !!open?.startsWith("role:") && !roles.some((r) => `role:${r.id}` === open);
  const openKey = (gone ? null : open) ?? (narrow ? null : firstKey);
  const role = openKey?.startsWith("role:") ? roles.find((r) => r.id === openKey.slice(5)) : undefined;

  /** Adds a role (with what a posting or a fits-none item filled in) and opens it. */
  const addRole = async (draft: TargetRole) => {
    const id = newRoleId(draft.name, roles);
    await store.edit("criteria", { ...criteria, target_roles: [...roles, { ...normalizeRole(draft), id }] }, draft.name);
    onOpen(`role:${id}`);
  };

  return (
    <>
      <section className="fi-list" aria-label="What to look for">
        <ListHead
          title="What to look for"
          action={
            <button type="button" className="fi-btn" aria-pressed={openKey === "add"} disabled={full} title={full ? ROLES_FULL : undefined} onClick={() => onOpen("add")}>
              Add a role
            </button>
          }
        />
        <ul className="fi-rows">
          <Row
            title="Company"
            meta={<span>{criteria.company?.trim() ? "The kind of employer, for every role" : "Not written yet"}</span>}
            lead={<span className="fi-fit is-none" aria-hidden="true">All</span>}
            current={openKey === "company"}
            onOpen={() => onOpen("company")}
          />
          <li className="fi-ss-group">
            <span>Target roles (up to {MAX_TARGET_ROLES})</span>
            <span>{roles.length}</span>
          </li>
          {roles.length === 0 && <li className="fi-ss-none">No roles yet. Add one to say what kind of job you want.</li>}
          {roles.map((r) => (
            <Row
              key={r.id}
              title={r.name.trim() || "Unnamed role"}
              meta={found(foundWeek[r.id]) ? <span>{found(foundWeek[r.id])}</span> : undefined}
              lead={<span className={`fi-fit${(foundWeek[r.id] ?? 0) === 0 ? " is-none" : ""}`} aria-hidden="true">{foundWeek[r.id] ?? "–"}</span>}
              current={openKey === `role:${r.id}`}
              onOpen={() => onOpen(`role:${r.id}`)}
            />
          ))}
          {fitsCount > 0 && (
            <Row
              title="Fits no role"
              meta={<span>Roles you pursued and postings this week that none of your roles claims</span>}
              lead={<span className="fi-fit" aria-hidden="true">{fitsCount}</span>}
              current={openKey === "fits"}
              onOpen={() => onOpen("fits")}
            />
          )}
        </ul>
      </section>
      {(!narrow || (open && !gone)) && (
        <Pane
          openKey={openKey ?? "none"}
          label={role ? role.name || "Unnamed role" : openKey === "company" ? "Company" : openKey === "fits" ? "Fits no role" : openKey === "add" ? "Add a role" : "What to look for"}
          backLabel="What to look for"
          onBack={() => onOpen(null)}
        >
          {role ? (
            <RolePane key={role.id} role={role} criteria={criteria} found={foundWeek[role.id]} onGone={() => onOpen(null)} />
          ) : openKey === "company" ? (
            <>
              <PaneHead title="Company" meta={["For every role"]} aside={<SaveHead parts={["criteria"]} />} />
              <Sheet>
                <Field
                  label="The kind of employer you want"
                  long
                  value={criteria.company ?? ""}
                  onSave={(company) => void store.edit("criteria", { ...criteria, company }, company)}
                />
              </Sheet>
              <HistorySheet part="criteria" />
            </>
          ) : openKey === "fits" && fits ? (
            <FitsPane fits={fits} full={full} more={() => setLimit((n) => n + FITS_PAGE)} onMade={addRole} onFine={() => setSeen((n) => n + 1)} />
          ) : openKey === "add" ? (
            full ? (
              <>
                <PaneHead title="Add a role" />
                <p className="fi-ss-quiet">{ROLES_FULL}</p>
              </>
            ) : (
            <AddRole onAdd={(name) => void addRole(blankRole("", name))} onCancel={() => onOpen(null)} />
            )
          ) : (
            <p className="fi-pane-note">Pick a role to see it here.</p>
          )}
        </Pane>
      )}
    </>
  );
}

/** One target role: its name and example titles, then the parts of a posting written for it, each saving itself. */
function RolePane({ role, criteria, found: week, onGone }: { role: TargetRole; criteria: Criteria; found?: number; onGone: () => void }) {
  const store = useSearchStore();
  const ready = useSearch((s) => s.ready);
  const [pasting, setPasting] = useState(false);
  const [checking, setChecking] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tooMany, setTooMany] = useState<number | null>(null);
  const roles = (criteria.target_roles ?? []).map(normalizeRole);
  const name = role.name.trim() || "Unnamed role";
  const noAgent = ready !== null && !ready.ready;

  useEffect(() => {
    void store.checkReady();
  }, [store]);

  const save = (next: TargetRole, typed: string | null = null) =>
    store.edit("criteria", { ...criteria, target_roles: roles.map((r) => (r.id === role.id ? next : r)) }, typed);

  const remove = async () => {
    setError(null);
    await store.edit("criteria", { ...criteria, target_roles: roles.filter((r) => r.id !== role.id) });
    const after = store.getState();
    if (after.saved.state === "failed") setError(after.saved.error);
    else onGone();
  };

  const fill = (fields: Partial<ParsedRole>) => {
    setPasting(false);
    void save(mergeParsed(role, fields));
  };

  const start = useCallback((text: string, roleId?: string) => store.parseExample(text, roleId), [store]);

  return (
    <>
      <PaneHead title={name} meta={[found(week)]} aside={<SaveHead parts={["criteria"]} />} />
      <div className="fi-actions">
        <button type="button" className="fi-btn" disabled={!ready?.ready || pasting} title={noAgent ? (ready?.why ?? undefined) : undefined} onClick={() => setPasting(true)}>
          Paste a job posting
        </button>
        <button type="button" className="fi-btn" onClick={() => setChecking(true)}>
          Check my roles
        </button>
        <span className="fi-actions-gap" />
        {confirming ? (
          <>
            <button type="button" className="fi-btn fi-ss-danger" onClick={() => void remove()}>
              Yes, delete it
            </button>
            <button type="button" className="fi-btn fi-btn-quiet" onClick={() => setConfirming(false)}>
              Keep it
            </button>
          </>
        ) : (
          <button type="button" className="fi-btn fi-btn-quiet" onClick={() => setConfirming(true)}>
            Delete role
          </button>
        )}
      </div>
      {noAgent && <p className="fi-ss-quiet">{ready?.why || "No agent is free to read a posting right now."}</p>}
      {error && (
        <p className="fi-alert" role="alert">
          {error}
        </p>
      )}
      <Sheet>
        <div className="fi-ss-fields">
          <Field label="Role name" value={role.name} onSave={(text) => void save({ ...role, name: text.trim() }, text)} />
          <Field
            label="Example titles, separated by semicolons"
            value={role.titles.join("; ")}
            onSave={(text) => {
              const titles = titlesTyped(text);
              if (titles.length > MAX_TITLES) {
                setTooMany(titles.length);
                return;
              }
              setTooMany(null);
              void save({ ...role, titles }, text);
            }}
          />
          <p className={tooMany ? "fi-alert" : "fi-ss-quiet"} role={tooMany ? "alert" : undefined}>
            {tooMany
              ? `A role holds up to ${MAX_TITLES} example titles, and you wrote ${tooMany}. Remove some, then leave the box again.`
              : `Up to ${MAX_TITLES} example titles (${role.titles.length} now).`}
          </p>
          {PARTS.map((p) => (
            <Field key={p.key} label={p.label} long value={role[p.key]} onSave={(text) => void save({ ...role, [p.key]: text }, text)} />
          ))}
        </div>
      </Sheet>
      <HistorySheet part="criteria" />
      {pasting && <ExamplePanel roleId={role.id} onFilled={fill} onClose={() => setPasting(false)} start={start} load={store.parseJob} />}
      {checking && <CheckRolesPanel ready={ready} onClose={() => setChecking(false)} begin={store.checkRoles} load={store.checkJob} />}
    </>
  );
}

/** The roles the owner pursued, and the postings found this week, that none of their roles claims: each can become a
 * role, or be called fine as it is. */
function FitsPane({ fits, full, more, onMade, onFine }: { fits: FitsNone; full: boolean; more: () => void; onMade: (draft: TargetRole) => Promise<void>; onFine: () => void }) {
  const store = useSearchStore();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const act = async (item: FitsNoneItem, make: boolean) => {
    setBusy(item.id);
    setError(null);
    try {
      if (make) await onMade(await store.roleFromRole(item.id));
      else {
        await store.dismissFitsNone(item.id);
        onFine();
      }
    } catch (err) {
      setError(errorMessage(err, make ? "Couldn't make a role from that." : "Couldn't mark that as fine."));
    } finally {
      setBusy(null);
    }
  };
  const list = (title: string, items: FitsNoneItem[], count: number) =>
    count > 0 && (
      <Sheet title={title} count={count}>
        <ul className="fi-ss-lines">
          {items.map((item) => {
            const stage = stageFor(item.stage);
            const bits = [item.company, stage?.label ?? item.stage_label, item.found_at ? shortDateInWords(item.found_at) : null].filter(Boolean);
            return (
              <li key={item.id} className="fi-ss-role">
                <div>
                  {item.kind === "role" ? (
                    <a className="fi-ss-role-title" href={roleAddress(item.id)}>
                      {item.title}
                    </a>
                  ) : (
                    <span className="fi-ss-role-title">{item.title}</span>
                  )}
                  <div className="fi-ss-quiet">{bits.join(" · ")}</div>
                </div>
                <span className="fi-ss-row-actions">
                  <button type="button" className="fi-btn" disabled={busy !== null || full} title={full ? ROLES_FULL : undefined} onClick={() => void act(item, true)}>
                    Make this a role
                  </button>
                  <button type="button" className="fi-btn fi-btn-quiet" disabled={busy !== null} onClick={() => void act(item, false)}>
                    Fine
                  </button>
                </span>
              </li>
            );
          })}
        </ul>
        {count > items.length && (
          <button type="button" className="fi-btn fi-btn-quiet fi-ss-more" onClick={more}>
            Show more
          </button>
        )}
      </Sheet>
    );
  return (
    <>
      <PaneHead title="Fits no role" aside={<SaveHead parts={["criteria"]} />} />
      {error && (
        <p className="fi-alert" role="alert">
          {error}
        </p>
      )}
      {list("Roles you pursued", fits.pursued.items, fits.pursued.count)}
      {list("Postings this week", fits.this_week.items, fits.this_week.count)}
    </>
  );
}

function AddRole({ onAdd, onCancel }: { onAdd: (name: string) => void; onCancel: () => void }) {
  const id = useId();
  const [name, setName] = useState("");
  return (
    <>
      <PaneHead title="Add a role" />
      <form
        className="fi-ss-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) onAdd(name.trim());
        }}
      >
        <div className="fi-field">
          <label htmlFor={id}>Name of the role</label>
          <input id={id} className="fi-input" value={name} autoComplete="off" onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="fi-actions">
          <button type="submit" className="fi-btn fi-btn-go" disabled={!name.trim()}>
            Add
          </button>
          <button type="button" className="fi-btn fi-btn-quiet" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </form>
    </>
  );
}
