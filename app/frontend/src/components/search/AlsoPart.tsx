// Also sent to agents (docs/PRD.md 30.11): the settings agents are sent that no other part of Search Settings shows. The
// owner's positioning, the rules (by the part of the work each steers) and the notes an older profile left, each in the
// owner's words with no limit on their length, edited or removed here and saved through the same save as every other
// part (the Saved line, Undo, History). Nothing is added here. Everything comes from the search store.

import { useState } from "react";
import type { Criteria, GateSet, ProfileNote, ProfileSettings, Scoring } from "../../api/types/profile";
import { useSearch, useSearchStore } from "../../state/searchStore";
import { Field, PaneHead, Sheet } from "../focus/parts";
import { HistorySheet, SaveHead } from "./common";

type Rule = ProfileSettings["rules"][number];

/** The areas a rule steers, in the order shown, with the group's title and what it says of who reads it. */
const AREAS: { area: string; title: string; hint: string }[] = [
  { area: "search", title: "Rules for the search", hint: "Sent to the search agent." },
  { area: "screening", title: "Rules for screening", hint: "Sent to the search agent." },
  { area: "packets", title: "Rules for packets", hint: "Sent to the writer of your resumes and cover letters." },
  { area: "apply", title: "Rules for applying", hint: "Kept with your profile." },
  { area: "email", title: "Rules for email", hint: "Kept with your profile." },
];
const OTHER = { area: "", title: "Other rules", hint: "" };

/** A long field that saves what was typed when it is left; emptied to nothing it saves nothing and goes back to its words
 * (Remove is how a rule or note goes). */
function Item({ label, value, onSave, onRemove, removeLabel }: { label: string; value: string; onSave: (text: string) => void; onRemove: () => void; removeLabel: string }) {
  const [again, setAgain] = useState(0);
  return (
    <div className="fi-ss-also-item">
      <Field
        key={again}
        label={label}
        long
        value={value}
        onSave={(text) => {
          if (text.trim()) onSave(text);
          else setAgain((n) => n + 1);
        }}
      />
      <button type="button" className="fi-btn" aria-label={removeLabel} onClick={onRemove}>
        Remove
      </button>
    </div>
  );
}

export default function AlsoPart() {
  const store = useSearchStore();
  const profile = useSearch((s) => s.profile);
  if (!profile) return null;
  const { criteria, gates, scoring, settings } = profile;
  const rules = settings.rules ?? [];
  const known = new Set(AREAS.map((a) => a.area));

  const saveRules = (next: Rule[]) => void store.edit("settings", { ...settings, rules: next });
  const setRule = (id: string, says: string) => saveRules(rules.map((r) => (r.id === id ? { ...r, says } : r)));
  const dropRule = (id: string) => saveRules(rules.filter((r) => r.id !== id));

  // The notes of the four documents, each saved with the document that holds it.
  const noteLists: { part: "criteria" | "gates" | "scoring" | "settings"; notes: ProfileNote[] }[] = [
    { part: "criteria", notes: criteria.notes ?? [] },
    { part: "gates", notes: gates.notes ?? [] },
    { part: "scoring", notes: scoring.notes ?? [] },
    { part: "settings", notes: settings.notes ?? [] },
  ];
  const saveNotes = (part: "criteria" | "gates" | "scoring" | "settings", notes: ProfileNote[]) => {
    if (part === "criteria") void store.edit("criteria", { ...criteria, notes } as Criteria);
    else if (part === "gates") void store.edit("gates", { ...gates, notes } as GateSet);
    else if (part === "scoring") void store.edit("scoring", { ...scoring, notes } as Scoring);
    else void store.edit("settings", { ...settings, notes });
  };
  const anyNotes = noteLists.some((l) => l.notes.length > 0);

  const groups = [...AREAS, OTHER]
    .map((g) => ({ ...g, rules: rules.filter((r) => (g.area ? r.area === g.area : !known.has(r.area))) }))
    .filter((g) => g.rules.length > 0);

  return (
    <section className="fi-pane" aria-label="Also sent to agents">
      <div className="fi-pane-in">
        <PaneHead title="Also sent to agents" meta={["What the agents are told that no other part shows"]} aside={<SaveHead parts={["criteria", "gates", "scoring", "settings"]} />} />

        <Sheet title="How you present yourself">
          <Field
            label="Your positioning"
            long
            value={criteria.positioning ?? ""}
            onSave={(text) => void store.edit("criteria", { ...criteria, positioning: text }, text)}
          />
          <p className="fi-ss-quiet">
            Read by the writer of your resumes and cover letters and by the ranking of roles. A change applies to the next resume, letter or ranking; roles
            already ranked are not re-read.
          </p>
        </Sheet>

        {groups.length > 0 && (
          <Sheet title="Rules">
            {groups.map((g) => (
              <div key={g.area || "other"} role="group" aria-label={g.title}>
                <h4>{g.title}</h4>
                {g.hint && <p className="fi-ss-quiet">{g.hint}</p>}
                {g.rules.map((r) => (
                  <Item key={r.id} label="Rule" value={r.says} onSave={(text) => setRule(r.id, text)} onRemove={() => dropRule(r.id)} removeLabel={`Remove this rule: ${r.says.slice(0, 40)}`} />
                ))}
              </div>
            ))}
          </Sheet>
        )}

        {anyNotes && (
          <Sheet title="Notes not placed yet">
            <p className="fi-ss-quiet">Kept word for word from an older profile. The search agent is sent them as written.</p>
            {noteLists.flatMap((l) =>
              l.notes.map((n) => (
                <Item
                  key={`${l.part}:${n.id}`}
                  label={n.source || "Note"}
                  value={n.text}
                  onSave={(text) => saveNotes(l.part, l.notes.map((x) => (x.id === n.id ? { ...x, text } : x)))}
                  onRemove={() => saveNotes(l.part, l.notes.filter((x) => x.id !== n.id))}
                  removeLabel={`Remove this note: ${n.text.slice(0, 40)}`}
                />
              )),
            )}
          </Sheet>
        )}

        <HistorySheet part="criteria" />
        <HistorySheet part="settings" />
      </div>
    </section>
  );
}
