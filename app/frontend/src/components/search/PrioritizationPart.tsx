// Prioritization (the screens rebuild spec, screen 3): how roles are put in order. The match weight is the list's first
// item and the pane's first sheet: one slider between "Can I do it" and "Does it fit", saved when let go (it ranks every
// role again, so the pane says Re-ranking until the stream says the new version landed). Below it the asks by the part of
// a posting they are about, each with its points, each part with its On switch; the suggestions left for the owner are an
// entry with a count. The pane edits the open ask, row or setting, saving as it goes. Everything comes from the search store.

import { useState, type ReactNode } from "react";
import type { Ask, Group, Profile, RoleSwitches, Scoring, SectionId } from "../../api/types/profile";
import GrowingText from "../primitives/GrowingText";
import { useSearch, useSearchStore } from "../../state/searchStore";
import { ListHead, Pane, PaneHead, Row, Sheet, Slider, Switch } from "../focus/parts";
import AskDecidedBy from "../profile/AskDecidedBy";
import AskVerify from "../profile/AskVerify";
import { AskFacts, PointsInput } from "../profile/EditParts";
import GradeRequirements from "../profile/GradeRequirements";
import { GRADE_START, GRADE_WORDS, inPageOrder, MATCH_WEIGHT_START, SECTIONS, SET_WORDS } from "../profile/profileSpec";
import { ALL_TAB, roleLabel, tabOf } from "../profile/scope";
import { AnyOfRow, FirstMatchRow } from "../profile/ScoringSets";
import { applySuggestion, cardOf, groupsIn, idsOf, isRowSuggestion, orderKey, overlapOf, tidy } from "../profile/setLogic";
import SuggestionBand, { type BandCard, type OverlapCard } from "../profile/SuggestionBand";
import { EARNS_NOTHING } from "../../lib/asks";
import { Draft, HistorySheet, SaveHead, ScopeControl } from "./common";
import { claimed } from "./dials";
import DialRows from "./DialRows";
import type { PartProps } from "./SearchScreen";

const asksIn = (asks: Ask[], scope: string, roleIds: ReadonlySet<string>, section: SectionId) =>
  asks.filter((a) => tabOf(a, roleIds) === scope && a.section === section);

const signed = (n: number) => (n > 0 ? `+${n}` : String(n));

/** A row's points at its start: green for a plus, plain for none. A line saved with minus points earns 0 (docs/PRD.md 30.10). */
function Points({ n }: { n: number }) {
  return (
    <span className={`fi-fit fi-ss-points${n > 0 ? " is-plus" : " is-none"}`} aria-hidden="true">
      {signed(Math.max(0, n))}
    </span>
  );
}

export default function PrioritizationPart({ open, onOpen, narrow }: PartProps) {
  const store = useSearchStore();
  const profile = useSearch((s) => s.profile) as Profile;
  const reranking = useSearch((s) => s.reranking);
  const [scope, setScope] = useState(ALL_TAB);

  const scoring = profile.scoring;
  const roles = (profile.criteria?.target_roles ?? []).map((r) => ({ id: r.id, name: r.name ?? "" }));
  const roleIds = new Set(roles.map((r) => r.id));
  const tab = roleIds.has(scope) ? scope : ALL_TAB;
  const asks = scoring.asks;
  const groups = scoring.groups ?? [];
  const weight = scoring.match_weight ?? MATCH_WEIGHT_START;
  const can = Math.round(weight * 100);

  /** Saves the asks and rows (a line with no sentence dropped, a row left with one statement an ask), and what else changed. */
  const save = (nextAsks: Ask[], nextGroups: Group[], extra: Partial<Scoring> = {}, typed: string | null = null) => {
    const sent = tidy(nextAsks, nextGroups);
    const doc: Scoring = { ...scoring, ...extra, asks: sent.asks };
    if (scoring.groups !== undefined || sent.groups.length > 0) doc.groups = sent.groups;
    return store.edit("scoring", doc, typed);
  };

  const offHere = tab === ALL_TAB ? inPageOrder(scoring.sections_off ?? []) : inPageOrder(scoring.by_role?.[tab]?.sections_off ?? []);
  const setSection = (id: SectionId, on: boolean) => {
    const next = inPageOrder(on ? offHere.filter((x) => x !== id) : [...offHere, id]);
    if (tab === ALL_TAB) void store.edit("scoring", { ...scoring, sections_off: next });
    else {
      const by: Record<string, RoleSwitches> = { ...(scoring.by_role ?? {}) };
      if (next.length > 0) by[tab] = { ...by[tab], sections_off: next };
      else delete by[tab];
      void store.edit("scoring", { ...scoring, by_role: by });
    }
  };

  // The suggestions the page can still use: one whose asks are gone or on different tabs is not offered.
  const partName = (id: SectionId) => SECTIONS.find(([s]) => s === id)?.[1] ?? id;
  const suggestions = scoring.suggestions ?? [];
  const grade = scoring.grade_requirements ?? GRADE_START;
  const cards: BandCard[] = suggestions.flatMap((suggestion) => {
    const card = cardOf(suggestion, asks, roleIds);
    if (!card) return [];
    const role = card.tab === ALL_TAB ? null : roleLabel(roles.find((r) => r.id === card.tab)?.name);
    return [{ suggestion, card, part: partName(card.section), role }];
  });
  const overlaps: OverlapCard[] = grade.on
    ? suggestions.flatMap((suggestion) => {
        const found = suggestion.kind === "remove" ? overlapOf(suggestion, asks) : [];
        return found.length > 0 ? [{ suggestion, asks: found }] : [];
      })
    : [];
  const offered = cards.length + overlaps.length;

  const openKey = open ?? (narrow ? null : "weight");
  const ask = openKey?.startsWith("ask:") ? asks.find((a) => a.id === openKey.slice(4)) : undefined;
  const group = openKey?.startsWith("group:") ? groups.find((g) => g.id === openKey.slice(6)) : undefined;

  // The asks a dial row shows are not shown again in the list (all of them are on the All tab).
  const dialIds = tab === ALL_TAB ? claimed(profile) : new Set<string>();
  // A part's rows sit highest first by their saved points, so a row does not move while it is edited.
  const rowsFor = (section: SectionId) => {
    const items: { key: number; node: ReactNode }[] = [];
    for (const a of asksIn(asks, tab, roleIds, section).filter((x) => !dialIds.has(x.id))) {
      items.push({
        key: orderKey(a),
        node: (
          <Row
            key={a.id}
            title={a.says || "A new ask"}
            meta={[a.points < 0 && <span key="nothing">Earns nothing</span>, a.checked_by === "app" && <span key="auto">Automatic</span>, a.derived && <span key="resp">Follows Responsibilities</span>].filter(Boolean)}
            lead={<Points n={a.points} />}
            current={openKey === `ask:${a.id}`}
            onOpen={() => onOpen(`ask:${a.id}`)}
          />
        ),
      });
    }
    for (const g of groupsIn(groups, tab, roleIds, section)) {
      const best = g.kind === "best" ? Math.max(0, ...g.members.map((m) => m.points)) : (g.points ?? 0);
      items.push({
        key: orderKey(g),
        node: (
          <Row
            key={g.id}
            title={g.members.find((m) => m.says.trim())?.says ?? SET_WORDS[g.kind].blank}
            meta={<span>{`${SET_WORDS[g.kind].title} · ${g.members.length} statements${g.kind === "any" && (g.points ?? 0) < 0 ? " · Earns nothing" : ""}`}</span>}
            lead={<Points n={best} />}
            current={openKey === `group:${g.id}`}
            onOpen={() => onOpen(`group:${g.id}`)}
          />
        ),
      });
    }
    return items.sort((x, y) => y.key - x.key).map((x) => x.node);
  };

  const label = ask
    ? ask.says || "A new ask"
    : group
      ? (group.members.find((m) => m.says.trim())?.says ?? SET_WORDS[group.kind].blank)
      : openKey === "grade"
        ? GRADE_WORDS.check
        : openKey === "suggestions"
          ? "Suggestions"
          : openKey === "weight"
            ? "Match weight"
            : "Prioritization";

  return (
    <>
      <section className="fi-list" aria-label="Prioritization">
        <ListHead title="Prioritization">{roles.length > 0 && <ScopeControl roles={roles} scope={tab} onScope={setScope} />}</ListHead>
        <ul className="fi-rows">
          <Row
            title="Match weight"
            meta={<span>{`Can I do it ${can}% · Does it fit ${100 - can}%`}</span>}
            lead={<span className="fi-fit" aria-hidden="true">{can}</span>}
            current={openKey === "weight"}
            onOpen={() => onOpen("weight")}
          />
          {offered > 0 && (
            <Row
              title="Suggestions"
              meta={<span>Left for you to use or put off</span>}
              lead={<span className="fi-fit fi-ss-yours" aria-hidden="true">{offered}</span>}
              current={openKey === "suggestions"}
              onOpen={() => onOpen("suggestions")}
            />
          )}
          {SECTIONS.map(([id, name]) => {
            const on = !offHere.includes(id);
            return (
              <li key={id} className={`fi-ss-section${on ? "" : " is-off"}`}>
                <div className="fi-ss-group">
                  <span>{name}</span>
                  <Switch checked={on} label={`${name} on`} onChange={(next) => setSection(id, next)} />
                </div>
                <ul className="fi-ss-sub">
                  {rowsFor(id)}
                  {id === "qualifications" && tab === ALL_TAB && (
                    <Row
                      title={GRADE_WORDS.check}
                      meta={<span>{grade.on ? `On · proven ${signed(grade.required_proven)}, transferable ${signed(grade.required_transferable)}, unproven ${signed(grade.required_unproven)}` : "Off"}</span>}
                      lead={<span className={`fi-fit${grade.on ? "" : " is-none"}`} aria-hidden="true">{grade.on ? "On" : "Off"}</span>}
                      current={openKey === "grade"}
                      onOpen={() => onOpen("grade")}
                    />
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
              <DialRows part="prioritization" />
            </li>
          )}
        </ul>
      </section>
      {(!narrow || open) && (
        <Pane openKey={`${tab}-${openKey ?? "none"}`} label={label} backLabel="Prioritization" onBack={() => onOpen(null)}>
          <PaneHead
            title={label}
            meta={[openKey === "weight" || openKey === "suggestions" || openKey === "grade" ? null : tab === ALL_TAB ? "Every role" : roleLabel(roles.find((r) => r.id === tab)?.name)]}
            aside={<SaveHead parts={["scoring"]} />}
          />
          {openKey === "weight" && (
            <Sheet>
              <Slider
                label="How much ‘can I do it’ counts against ‘does it fit what I want’"
                value={can}
                min={0}
                max={100}
                step={5}
                ends={(v) => [`Can I do it ${v}%`, `Does it fit ${100 - v}%`]}
                valueText={(v) => `Can I do it ${v}%, does it fit what I want ${100 - v}%`}
                onCommit={(v) => void store.setMatchWeight(v / 100)}
              />
              {reranking && (
                <p className="fi-ss-quiet" role="status">
                  Re-ranking every role…
                </p>
              )}
            </Sheet>
          )}
          {openKey === "suggestions" && (
            <SuggestionBand
              cards={cards}
              overlaps={overlaps}
              onUse={(id) => {
                const suggestion = suggestions.find((s) => s.id === id);
                if (!suggestion || !isRowSuggestion(suggestion) || !cardOf(suggestion, asks, roleIds)) return;
                const next = applySuggestion(suggestion, asks, groups);
                void save(next.asks, next.groups, { suggestions: suggestions.filter((s) => s.id !== id) });
                onOpen(`group:${next.group.id}`);
              }}
              onLater={(id) => void save(asks, groups, { suggestions: suggestions.filter((s) => s.id !== id) })}
              onDrop={(id) => {
                const suggestion = suggestions.find((s) => s.id === id);
                if (!suggestion) return;
                const gone = new Set(overlapOf(suggestion, asks).map((a) => a.id));
                void save(asks.filter((a) => !gone.has(a.id)), groups, { suggestions: suggestions.filter((s) => s.id !== id) });
              }}
            />
          )}
          {openKey === "grade" && (
            <Sheet>
              <Draft value={grade} onSave={(next) => void store.edit("scoring", { ...scoring, grade_requirements: next })}>
                {(g, change) => <GradeRequirements grade={g} onChange={change} />}
              </Draft>
            </Sheet>
          )}
          {ask && (
            <Sheet>
              <Draft value={ask} onSave={(next) => void save(asks.map((a) => (a.id === ask.id ? next : a)), groups, {}, next.says)}>
                {(a, change) => (
                  <AskEditor
                    ask={a}
                    onChange={change}
                    onRemove={() => {
                      void save(asks.filter((x) => x.id !== ask.id), groups);
                      onOpen(null);
                    }}
                  />
                )}
              </Draft>
            </Sheet>
          )}
          {group && (
            <Sheet>
              <Draft value={group} onSave={(next) => void save(asks, groups.map((g) => (g.id === group.id ? next : g)))}>
                {(g, change) => <GroupEditor group={g} taken={idsOf(asks, groups)} onChange={change} onRemove={() => {
                  void save(asks, groups.filter((x) => x.id !== group.id));
                  onOpen(null);
                }} />}
              </Draft>
            </Sheet>
          )}
          {openKey && openKey !== "suggestions" && <HistorySheet part="scoring" />}
          {!openKey && <p className="fi-pane-note">Pick an ask to see it here.</p>}
        </Pane>
      )}
    </>
  );
}

/** One ask: its points, its sentence, its Verify line, who decides it and the value the app decides it from, and Remove. */
function AskEditor({ ask, onChange, onRemove }: { ask: Ask; onChange: (next: Ask) => void; onRemove: () => void }) {
  const name = ask.says || "a new ask";
  return (
    <div className="fi-ss-ask">
      <div className="fi-ss-ask-line">
        <PointsInput value={ask.points} label={`Points for ${name}`} onChange={(points) => onChange({ ...ask, points })} />
        <GrowingText
          className="fi-input"
          aria-label="The ask, one sentence about a posting"
          placeholder="For example: The role manages a team"
          oneLine
          value={ask.says}
          onText={(says) => onChange({ ...ask, says })}
        />
      </div>
      {ask.points < 0 && <p className="fi-ss-quiet" role="note">{EARNS_NOTHING}</p>}
      <AskVerify ask={ask} name={name} onChange={onChange} />
      <AskDecidedBy ask={ask} name={name} onChange={onChange} />
      <AskFacts ask={ask} name={name} onChange={onChange} />
      <div className="fi-actions">
        <button type="button" className="fi-btn fi-btn-quiet" onClick={onRemove}>
          Remove
        </button>
      </div>
    </div>
  );
}

/** A "first match" or "any of" row, edited whole. */
function GroupEditor({ group, taken, onChange, onRemove }: { group: Group; taken: { id: string }[]; onChange: (next: Group) => void; onRemove: () => void }) {
  const Editor = group.kind === "best" ? FirstMatchRow : AnyOfRow;
  return (
    <ul className="fi-ss-gate">
      <li>
        <Editor group={group} focus={null} taken={taken} onChange={onChange} onRemove={onRemove} />
      </li>
    </ul>
  );
}
