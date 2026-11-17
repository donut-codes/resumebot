import { useState } from "react";
import { api } from "../../api/client";
import type { Writing } from "../../api/types/me";
import type { TargetRole } from "../../api/types/profile";
import { routeFor, serializeRoute } from "../../lib/route";
import { plural } from "../../lib/text";
import { useResource } from "../../lib/useResource";
import SaveBar from "./SaveBar";
import FoldPart, { useFolds } from "./FoldPart";
import { MeSection } from "./MeSection";
import { Field } from "../primitives/fields";
import RuleList from "./RuleList";
import { FOLD_OVER_LINES, RULE_DOCS, linesOf, preview, pruneWriting, type RuleDoc, type TabProps } from "./meParts";
import { useMeDoc } from "./useMeDoc";

type Rules = { id: string; says: string }[];

const sayings = (items: Rules) => items.map((r) => r.says);
const roleFoldId = (id: string) => `role-${id}`;
/** A deleted role's id as words ("ops-lead" as "ops lead"): all that is left of its name. */
const wordsOf = (id: string) => id.replace(/-+/g, " ").trim() || "a role";

/** Where the writer's instruction file and its response template are edited. */
const HOW_THEY_WORK = serializeRoute(routeFor("agents", { agentsHow: true, howPath: ["jobsearch_packet"] }));

const marginText = (n: number | null) => (n === null ? "" : String(n));

/** The page margin of the Word export, in inches: kept as typed while editing ("0." is not yet a number), and an emptied
 * box is no margin (null), which leaves the export its own. A number out of range is left for the save to refuse, in
 * words, in the save bar. */
function MarginField({ value, onChange }: { value: number | null; onChange: (next: number | null) => void }) {
  const [text, setText] = useState(marginText(value));
  // Changed from outside (put back by Undo): show it, unless what is typed already says it.
  const [seen, setSeen] = useState(value);
  if (value !== seen) {
    setSeen(value);
    if ((text === "" ? null : Number(text)) !== value) setText(marginText(value));
  }
  return (
    <Field label="Page margins (inches)" hint="From 0.25 to 1.5, on all four sides of the Word resume and cover letter. Leave empty for the default margins.">
      {({ id, describedBy }) => (
        <input
          id={id}
          aria-describedby={describedBy}
          type="number"
          inputMode="decimal"
          step={0.05}
          className="fi-input fi-me-number"
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            const n = e.target.value === "" ? null : Number(e.target.value);
            if (n === null || Number.isFinite(n)) onChange(n);
          }}
          onBlur={() => setText(marginText(value))}
        />
      )}
    </Field>
  );
}

/** Which parts start open: the short ones. A target role's rules start open when it has some. */
function foldDefaults(w: Writing, doc: RuleDoc): Record<string, boolean> {
  const open = (lines: number) => lines <= FOLD_OVER_LINES;
  const out: Record<string, boolean> = { global: open(linesOf(...sayings(w[doc.globalField]))) };
  for (const s of doc.sections) out[s.id] = open(linesOf(...sayings(w[s.field])));
  for (const [id, rules] of Object.entries(w[doc.roleField])) out[roleFoldId(id)] = rules.length > 0;
  return out;
}

/** What the document is for, when it is worth writing, and seven things that help it work: fixed words, the same for
 * everyone. */
function Crib({ doc }: { doc: RuleDoc }) {
  return (
    <MeSection id="me-crib" title={doc.crib.title}>
      <div className="fi-me-crib">
        {doc.crib.paragraphs.map((p) => (
          <p key={p}>{p}</p>
        ))}
        <ol>
          {doc.crib.practices.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ol>
      </div>
    </MeSection>
  );
}

/** The layout of the built document as a skeleton: the sections in the order it prints them, each with how many rules it
 * has, and the page margin. Read-only; it follows the rules as they are being edited. */
function Skeleton({ w, doc }: { w: Writing; doc: RuleDoc }) {
  return (
    <MeSection id="me-skeleton" title={doc.skeleton.title} hint={doc.skeleton.hint}>
      <div>
        <p className="fi-me-skeleton-page">
          {doc.skeleton.whole}: {plural(w[doc.globalField].length, "Global rule")}. {doc.skeleton.margin(w.page_margin_inches)}
        </p>
        <ol className="fi-me-skeleton-list">
          {doc.sections.map((s) => (
            <li key={s.id}>
              <div className="fi-me-skeleton-head">
                <span className="fi-me-skeleton-name">{s.printed}</span>
                <span className="fi-me-count">{plural(w[s.field].length, "rule")}</span>
              </div>
              <p className="fi-me-skeleton-shape">{s.shape}</p>
            </li>
          ))}
        </ol>
      </div>
    </MeSection>
  );
}

/** Resume Rules and Cover Letter Rules (`kind`), one page built two ways: the fixed crib, then one-point rules for the whole
 * document (Global, with the page margin on the resume's, and a fold for each target role), then for each section of the
 * document in the order it prints. The right-hand column is read-only: the document as built, and a line linking to where the writer's instructions are edited.
 * Saves only its own parts of the one writing document. */
export default function SectionedRulesTab({ saved, version, onSaved, kind }: TabProps<Writing> & { kind: "resume" | "letter" }) {
  const doc = RULE_DOCS[kind];
  const globalRules = (w: Writing): Rules => w[doc.globalField];
  const me = useMeDoc<Writing>({
    saved,
    version,
    parts: doc.parts,
    prune: pruneWriting,
    onSaved,
    save: async (doc, why, base) => (await api.saveProfile("writing", doc, why, base)).version ?? base,
  });
  const w = me.doc;
  const profile = useResource(() => api.profile(), [], { fallback: "Couldn't load your target roles." });
  const roles: TargetRole[] = profile.data?.criteria.target_roles ?? [];
  const rolesError = profile.error;
  // Until the profile has answered, no role is known, so none can be called deleted.
  const rolesKnown = profile.data !== null || rolesError !== null;
  // A role's rules start open when it has some; a role with none starts folded.
  const folds = useFolds(() => foldDefaults(saved, doc), false);
  const set = (patch: Partial<Writing>) => me.edit({ ...w, ...patch });
  const known = new Set(roles.map((r) => r.id));
  const byRole = w[doc.roleField];
  const gone = rolesKnown && !rolesError ? Object.keys(byRole).filter((id) => !known.has(id)) : [];
  const roleRules = (id: string): Rules => byRole[id] ?? [];
  const setRole = (id: string, items: Rules) => set({ [doc.roleField]: { ...byRole, [id]: items } });

  const roleFold = (id: string, title: string, note?: string) => (
    <li key={id} className="fi-me-item">
      <div className="fi-me-item-body">
        <FoldPart
          id={roleFoldId(id)}
          level={3}
          title={title}
          open={folds.isOpen(roleFoldId(id))}
          onToggle={() => folds.toggle(roleFoldId(id))}
          count={plural(roleRules(id).length, "rule")}
          summary={roleRules(id).length ? preview(sayings(roleRules(id)), 2) : "No rules yet"}
        >
          {note && <p className="fi-me-hint">{note}</p>}
          <RuleList items={roleRules(id)} onChange={(items) => setRole(id, items)} noun="rule" label={`${title} rule`} />
        </FoldPart>
      </div>
    </li>
  );

  return (
    <div className="fi-me-page">
      <Crib doc={doc} />

      <div className="fi-me-rules-layout">
        <div className="fi-me-parts">
          <FoldPart
            id={doc.globalPart.id}
            title={doc.globalPart.title}
            hint={doc.globalPart.hint}
            open={folds.isOpen(doc.globalPart.id)}
            onToggle={() => folds.toggle(doc.globalPart.id)}
            dirty={me.dirtyIds.includes(doc.globalPart.id)}
            count={plural(globalRules(w).length, "rule")}
            summary={preview(sayings(globalRules(w)), 3)}
          >
            {/* One margin serves the Word resume and the Word letter, so it is set here on the resume's page only. */}
            {kind === "resume" && <MarginField value={w.page_margin_inches} onChange={(page_margin_inches) => set({ page_margin_inches })} />}
            <RuleList items={globalRules(w)} onChange={(items) => set({ [doc.globalField]: items })} noun="rule" label="Global rule" />
            <h3 className="fi-me-list-name">
              Rules for each role <span className="fi-me-count">{roles.length + gone.length}</span>
            </h3>
            <p className="fi-me-hint">{doc.roleHint}</p>
            {rolesError && <p className="fi-me-hint">{rolesError}</p>}
            {!rolesKnown && <p className="fi-me-hint">Loading your target roles&hellip;</p>}
            {rolesKnown && roles.length === 0 && gone.length === 0 && !rolesError && <p className="fi-me-hint">You have no target roles yet.</p>}
            <ul className="fi-me-items fi-me-role-rules">
              {roles.map((r) => roleFold(r.id, r.name.trim() ? `For ${r.name} roles` : "For a role with no name yet"))}
              {gone.map((id) => roleFold(id, `For ${wordsOf(id)} roles`, doc.goneNote))}
            </ul>
          </FoldPart>

          {doc.sections.map((s) => (
            <FoldPart
              key={s.id}
              id={s.id}
              title={s.title}
              hint={s.hint}
              open={folds.isOpen(s.id)}
              onToggle={() => folds.toggle(s.id)}
              dirty={me.dirtyIds.includes(s.id)}
              count={plural(w[s.field].length, "rule")}
              summary={preview(sayings(w[s.field]), 3)}
            >
              <RuleList items={w[s.field]} onChange={(items) => set({ [s.field]: items })} noun="rule" label={`${s.title} rule`} />
            </FoldPart>
          ))}
        </div>

        <aside className="fi-me-rules-side" aria-label={doc.sideLabel}>
          <Skeleton w={w} doc={doc} />
          <p className="fi-me-hint">The writer's instructions are edited on <a href={HOW_THEY_WORK}>How they work</a>.</p>
        </aside>
      </div>

      {me.dirty && <SaveBar ref={me.bar.ref} what={me.bar.what} why={me.bar.why} onWhy={me.bar.onWhy} busy={me.bar.busy} error={me.bar.error} stale={me.bar.stale} onUndo={me.bar.onUndo} onSave={me.bar.onSave} />}
    </div>
  );
}
