// Hard pass (the screens rebuild spec, screen 3): what a posting must not be. The scope (every role, or one target
// role) is the list head's control; the list holds the parts of a posting, each with its On switch, and under each its
// rules, every one with how many roles and postings it stopped (a rule that stops nothing is a rule to question). The pane
// edits the open rule, saving as it goes: work location, lowest pay (with how the ask is worked out, open), the employers
// on the hard pass list, and each sentence or employer rule. Everything comes from the search store.

import { useEffect, useState } from "react";
import type { Employer } from "../../api/types/employers";
import type { CompMethod, Gate, GateSet, PayAgainst, Profile, RoleGates, SectionId } from "../../api/types/profile";
import { newId } from "../../lib/ids";
import { routeFor, serializeRoute } from "../../lib/route";
import { useSearch, useSearchStore } from "../../state/searchStore";
import { ListHead, Pane, PaneHead, Row, Sheet, Switch } from "../focus/parts";
import AskMethod from "../profile/AskMethod";
import { problemsOf } from "../profile/askMethodNumbers";
import { GateLine } from "../profile/GateLine";
import { compOf, gatesOf, linesIn, scopeView, tidyGates, withScope } from "../profile/gateScopes";
import { gateSection, inPageOrder, locationSays, SECTIONS } from "../profile/profileSpec";
import { ALL_TAB, roleLabel, rolesFor } from "../profile/scope";
import { WAYS, WayRow } from "../profile/WayRow";
import { Draft, HistorySheet, SaveHead, ScopeControl } from "./common";
import { claimed } from "./dials";
import DialRows from "./DialRows";
import { LIMIT_WARNING } from "./limitWarning";
import type { PartProps } from "./SearchScreen";

const AGAINST: [PayAgainst, string][] = [
  ["minimum", "the posted minimum"],
  ["maximum", "the posted maximum"],
  ["ask", "the estimated ask"],
];

/** The id a stop keeps for a structured line: All's own, or a role's with the role after a colon. */
const lineId = (line: "location" | "compensation", scope: string) => (scope === ALL_TAB ? line : `${line}:${scope}`);

const stoppedWords = (n: number | undefined) => (n ? `Stopped ${n.toLocaleString()}` : "Stopped none");

export default function HardPassPart({ open, onOpen, narrow }: PartProps) {
  const store = useSearchStore();
  const profile = useSearch((s) => s.profile) as Profile;
  const stopped = useSearch((s) => s.stopped);
  const [scope, setScope] = useState(ALL_TAB);
  // A rule just added: it is not saved until it says something.
  const [pending, setPending] = useState<Gate | null>(null);
  // The warning before the limit of two open applications is turned off.
  const [warning, setWarning] = useState(false);

  const roles = (profile.criteria?.target_roles ?? []).map((r) => ({ id: r.id, name: r.name ?? "" }));
  const roleIds = new Set(roles.map((r) => r.id));
  const tab = roleIds.has(scope) ? scope : ALL_TAB;
  const gates = gatesOf(profile);
  const view = scopeView(gates, tab);
  const save = (next: GateSet, typed: string | null = null) => store.edit("gates", tidyGates(next, roleIds), typed);
  const setOwn = (patch: Partial<RoleGates>) => save(withScope(gates, tab, patch));
  const sectionOn = (id: SectionId) => !view.sections_off.includes(id);
  const setSection = (id: SectionId, on: boolean) => {
    const rest = view.sections_off.filter((x) => x !== id);
    void setOwn({ sections_off: inPageOrder(on ? rest : [...rest, id]) });
  };

  const add = (section: SectionId) => {
    const id = newId({ name: "employer fact" }, gates.other);
    const blank: Gate = { id, kind: "employer_fact", section, text: "", fact: "ownership", op: "!=", value: "", roles: rolesFor(tab) };
    setPending(blank);
    onOpen(`new:${id}`);
  };

  const locSays = scopeView(gates, tab).location;
  const anyWay = locSays.remote.on || locSays.hybrid.on || locSays.in_person.on;
  const payAmount = view.compensation.amount;
  const firstKey = "location";
  const openKey = open ?? (narrow ? null : firstKey);
  const gate = openKey?.startsWith("gate:") ? gates.other.find((g) => g.id === openKey.slice(5)) : undefined;
  const fresh = openKey?.startsWith("new:") && pending && openKey === `new:${pending.id}` ? pending : null;

  const says = (g: Gate) => (g.kind === "rule" ? g.text : (profile.says.gates.other[g.id] ?? g.text)) || "A new rule";
  // The lines a dial row shows are not shown again in the list (all of them are on the All tab).
  const dialIds = tab === ALL_TAB ? claimed(profile) : new Set<string>();
  const rowsFor = (section: SectionId) => {
    const rows = [];
    if (section === "company" && tab === ALL_TAB) {
      rows.push(
        <Row
          key="employers"
          title="Employers on your hard pass list"
          meta={<span>{stoppedWords(stopped.employer)}</span>}
          lead={<Stopped n={stopped.employer} />}
          current={openKey === "employers"}
          onOpen={() => onOpen("employers")}
        />,
        <Row
          key="limit"
          title="At most two open applications at one employer"
          meta={<span>{gates.employer_limit ? "On" : "Off"}</span>}
          current={openKey === "limit"}
          onOpen={() => onOpen("limit")}
        />,
      );
    }
    if (section === "posting") {
      rows.push(
        <Row
          key="location"
          title={anyWay ? locationSays(locSays) : tab === ALL_TAB ? "Work location: not checked" : "Work location: as for every role"}
          meta={<span>{stoppedWords(stopped[lineId("location", tab)])}</span>}
          lead={<Stopped n={stopped[lineId("location", tab)]} />}
          current={openKey === "location"}
          onOpen={() => onOpen("location")}
        />,
      );
    }
    if (section === "compensation") {
      rows.push(
        <Row
          key="pay"
          title={payAmount ? `Pay at least ${payAmount.toLocaleString()} a year` : tab === ALL_TAB ? "Lowest pay: not set" : "Lowest pay: as for every role"}
          meta={<span>{stoppedWords(stopped[lineId("compensation", tab)])}</span>}
          lead={<Stopped n={stopped[lineId("compensation", tab)]} />}
          current={openKey === "pay"}
          onOpen={() => onOpen("pay")}
        />,
      );
    }
    for (const g of linesIn(gates, tab, roleIds, section).filter((x) => !dialIds.has(x.id))) {
      rows.push(
        <Row
          key={g.id}
          title={says(g)}
          meta={<span>{stoppedWords(stopped[g.id])}</span>}
          lead={<Stopped n={stopped[g.id]} />}
          current={openKey === `gate:${g.id}`}
          onOpen={() => onOpen(`gate:${g.id}`)}
        />,
      );
    }
    if (pending && gateSection(pending) === section && (pending.roles ?? []).join() === rolesFor(tab).join()) {
      rows.push(<Row key={pending.id} title="A new rule" meta={<span>Not saved until it says something</span>} current={openKey === `new:${pending.id}`} onOpen={() => onOpen(`new:${pending.id}`)} />);
    }
    return rows;
  };

  const paneLabel = gate ? says(gate) : fresh ? "A new rule" : openKey === "location" ? "Work location" : openKey === "pay" ? "Lowest pay" : openKey === "employers" ? "Employers" : openKey === "limit" ? "Open applications" : "Hard pass";

  return (
    <>
      <section className="fi-list" aria-label="Hard pass">
        <ListHead title="Hard pass">{roles.length > 0 && <ScopeControl roles={roles} scope={tab} onScope={setScope} />}</ListHead>
        <ul className="fi-rows">
          {SECTIONS.map(([id, name]) => {
            const on = sectionOn(id);
            return (
              <li key={id} className={`fi-ss-section${on ? "" : " is-off"}`}>
                <div className="fi-ss-group">
                  <span>{name}</span>
                  <Switch checked={on} label={`${name} on`} onChange={(next) => setSection(id, next)} />
                </div>
                <ul className="fi-ss-sub">
                  {rowsFor(id)}
                  {id === "company" && (
                    <li className="fi-ss-add">
                      <button type="button" className="fi-link" onClick={() => add(id)}>
                        Add a rule about the employer
                      </button>
                    </li>
                  )}
                </ul>
              </li>
            );
          })}
          {tab === ALL_TAB && (
            <li className="fi-ss-section">
              <div className="fi-ss-group">
                <span>Measured dials</span>
              </div>
              <DialRows part="hard_pass" />
            </li>
          )}
        </ul>
      </section>
      {(!narrow || open) && (
        <Pane openKey={`${tab}-${openKey ?? "none"}`} label={paneLabel} backLabel="Hard pass" onBack={() => onOpen(null)}>
          <PaneHead
            title={paneLabel}
            meta={[tab === ALL_TAB ? "Every role" : roleLabel(roles.find((r) => r.id === tab)?.name), gate || openKey === "location" || openKey === "pay" ? stoppedWords(stopped[gate ? gate.id : lineId(openKey === "pay" ? "compensation" : "location", tab)]) : null]}
            aside={<SaveHead parts={["gates", "settings"]} />}
          />
          {openKey === "location" && (
            <Sheet>
              <Draft value={view.location} onSave={(location) => void setOwn({ location })}>
                {(loc, change) => (
                  <div className="fi-ss-ways">
                    {WAYS.map(({ key, name, field }) => (
                      <WayRow key={key} name={name} field={field} way={loc[key]} onChange={(next) => change({ ...loc, [key]: next })} />
                    ))}
                  </div>
                )}
              </Draft>
            </Sheet>
          )}
          {openKey === "pay" && (
            <>
              <Sheet>
                <Draft value={view.compensation} onSave={(compensation) => void setOwn({ compensation })}>
                  {(pay, change) => (
                    <div className="fi-ss-fields fi-ss-pair">
                      <div className="fi-field">
                        <label htmlFor="fi-ss-pay">At least (a year)</label>
                        <input
                          id="fi-ss-pay"
                          className="fi-input"
                          type="number"
                          min={0}
                          value={pay.amount ?? ""}
                          onChange={(e) => change({ ...pay, amount: e.target.value === "" ? null : Number(e.target.value) })}
                        />
                      </div>
                      <div className="fi-field">
                        <label htmlFor="fi-ss-against">Compared with</label>
                        <select id="fi-ss-against" className="fi-input" value={pay.against} onChange={(e) => change({ ...pay, against: e.target.value as PayAgainst })}>
                          {AGAINST.map(([v, l]) => (
                            <option key={v} value={v}>
                              {l}
                            </option>
                          ))}
                        </select>
                      </div>
                    </div>
                  )}
                </Draft>
              </Sheet>
              {tab === ALL_TAB && (
                <Sheet title="How the ask is worked out">
                  <Draft value={compOf(profile.settings)} onSave={(comp: CompMethod) => Object.keys(problemsOf(comp)).length === 0 && void store.edit("settings", { ...profile.settings, ...comp })}>
                    {(comp, change) => <AskMethod value={comp} onChange={change} example={store.compExample} />}
                  </Draft>
                </Sheet>
              )}
            </>
          )}
          {openKey === "employers" && <GatedEmployers />}
          {openKey === "limit" && (
            <Sheet>
              <div className="fi-ss-fields">
                <p className="fi-ss-quiet">
                  When an employer has two open applications (Applied or Active Discussion), every other role there is held back, in Gated out with the reason. When an application ends, the held roles come back into the list.
                </p>
                <div className="fi-ss-group">
                  <span>Work at most two open applications at one employer.</span>
                  <Switch
                    checked={gates.employer_limit}
                    label="At most two open applications at one employer"
                    onChange={(next) => {
                      if (next) {
                        setWarning(false);
                        void save({ ...gates, employer_limit: true });
                      } else setWarning(true);
                    }}
                  />
                </div>
                {warning && gates.employer_limit && (
                  <div role="group" aria-label="Before you turn this off" className="fi-ss-quiet">
                    <p>{LIMIT_WARNING.intro}</p>
                    <p>
                      <strong>{LIMIT_WARNING.heading}</strong>
                    </p>
                    <ul>
                      {LIMIT_WARNING.points.map(([lead, rest]) => (
                        <li key={lead}>
                          <strong>{lead}:</strong> {rest}
                        </li>
                      ))}
                    </ul>
                    <button
                      type="button"
                      className="fi-link"
                      onClick={() => {
                        setWarning(false);
                        void save({ ...gates, employer_limit: false });
                      }}
                    >
                      Turn it off
                    </button>{" "}
                    <button type="button" className="fi-link" onClick={() => setWarning(false)}>
                      Keep it on
                    </button>
                  </div>
                )}
              </div>
            </Sheet>
          )}
          {gate && (
            <Sheet>
              <Draft value={gate} onSave={(next) => void save({ ...gates, other: gates.other.map((x) => (x.id === gate.id ? next : x)) }, next.text || null)}>
                {(g, change) => (
                  <ul className="fi-ss-gate">
                    <GateLine
                      gate={g}
                      says={profile.says.gates.other[g.id] ?? ""}
                      focus={false}
                      onChange={change}
                      onRemove={() => {
                        void save({ ...gates, other: gates.other.filter((x) => x.id !== gate.id) });
                        onOpen(null);
                      }}
                    />
                  </ul>
                )}
              </Draft>
            </Sheet>
          )}
          {fresh && (
            <Sheet>
              <Draft
                value={fresh}
                onSave={(next) => {
                  const said = next.kind === "rule" ? next.text.trim() : String(next.value ?? "").trim();
                  if (!said) {
                    setPending(next);
                    return;
                  }
                  setPending(null);
                  void save({ ...gates, other: [...gates.other, next] }, next.text || null);
                  onOpen(`gate:${next.id}`);
                }}
              >
                {(g, change) => (
                  <ul className="fi-ss-gate">
                    <GateLine
                      gate={g}
                      says=""
                      focus
                      onChange={change}
                      onRemove={() => {
                        setPending(null);
                        onOpen(null);
                      }}
                    />
                  </ul>
                )}
              </Draft>
            </Sheet>
          )}
          {!gate && !fresh && !["location", "pay", "employers", "limit"].includes(openKey ?? "") && <p className="fi-pane-note">Pick a rule to see it here.</p>}
          {(gate || fresh || openKey === "location" || openKey === "pay") && <HistorySheet part="gates" />}
        </Pane>
      )}
    </>
  );
}

/** How many a rule stopped, at the row's start: faint at none. */
function Stopped({ n }: { n: number | undefined }) {
  return (
    <span className={`fi-fit${n ? "" : " is-none"}`} aria-hidden="true">
      {n ? (n > 999 ? `${Math.floor(n / 1000)}k` : n) : 0}
    </span>
  );
}

/** The employers on the hard pass list, with the reason: each is put on it from its own page in Employers. */
function GatedEmployers() {
  const store = useSearchStore();
  const [list, setList] = useState<Employer[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    store.gatedEmployers().then(
      (e) => live && setList(e),
      () => live && setFailed(true),
    );
    return () => {
      live = false;
    };
  }, [store]);
  return (
    <Sheet title="On the list" count={list?.length}>
      {failed ? (
        <p className="fi-alert">Couldn&rsquo;t load the employers.</p>
      ) : !list ? (
        <p className="fi-ss-quiet">Loading…</p>
      ) : list.length === 0 ? (
        <p className="fi-ss-quiet">No employer is on it. One is put on it from its page in Employers.</p>
      ) : (
        <ul className="fi-ss-lines">
          {list.map((e) => (
            <li key={e.id} className="fi-ss-role">
              <div>
                <a className="fi-ss-role-title" href={serializeRoute(routeFor("employers", { employerId: e.id }))}>
                  {e.name}
                </a>
                {e.gated_reason && <div className="fi-ss-quiet">{e.gated_reason}</div>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Sheet>
  );
}
