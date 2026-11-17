import { type ReactNode } from "react";
import { api } from "../../api/client";
import { useResource } from "../../lib/useResource";
import type { Writing } from "../../api/types/me";
import type { WritingPage } from "../../lib/route";
import { plural } from "../../lib/text";
import SaveBar from "./SaveBar";
import FoldPart, { useFolds } from "./FoldPart";
import { Check, Text } from "../primitives/fields";
import ItemList, { nameOf } from "./ItemList";
import RuleList from "./RuleList";
import {
  ATS_PART, BANNED_LINE, BLANK_BANNED, BLANK_CREDENTIAL, BLANK_DRIFT, CHECKED_LINE, CREDENTIALS_LINE, FOLD_OVER_LINES, INTERVIEW_PART, QA_LINE, QA_PART,
  REVIEW_LINE, STYLE_LINE, VOICE_PART, WRITING_PAGE_PARTS, linesOf, preview, pruneWriting, withStyleSwitch, type PartDef, type TabProps,
} from "./meParts";
import { useMeDoc } from "./useMeDoc";

type RuleKey = "voice_rules" | "ats_rules" | "interview_rules" | "review_steps" | "review_flags" | "humanizing";

/** The pages of rules this tab shows: Resume Rules and Cover Letter Rules have parts of their own (`SectionedRulesTab`). */
export type RulesPage = Exclude<WritingPage, "resume-rules" | "letter-rules">;

const open = (lines: number) => lines <= FOLD_OVER_LINES;
const sayings = (items: { says: string }[]) => items.map((r) => r.says);

/** The lines in the QA section that are not rules (a phrase, a drift pattern), for how long the section reads. The style rules and
 * the certifications are not among them: the section's count of what it holds does not move for either. */
const qaLines = (w: Writing) => [
  ...sayings(w.review_steps),
  ...sayings(w.review_flags),
  ...sayings(w.humanizing),
  ...w.banned.map((b) => `${b.phrase} ${b.why}`),
  ...w.drift_patterns.map((d) => `${d.name} ${d.looks_like} ${d.fix}`),
];

/** Which parts start open: the short ones. */
function foldDefaults(w: Writing): Record<string, boolean> {
  return {
    voice: open(linesOf(...sayings(w.voice_rules))),
    ats: open(linesOf(...sayings(w.ats_rules))),
    interview: open(linesOf(...sayings(w.interview_rules))),
    qa: open(linesOf(...qaLines(w))),
  };
}

/** The page of writing rules (`page`) that is plain lists, edited where it stands and saving only its own sections of the
 * one writing document: Document Creation Rules (rules for your voice, for ATS screening, and settings for QA of
 * documentation). Each rule is one line, with Add and Remove. (Resume Rules and Cover Letter Rules have parts of their
 * own: `SectionedRulesTab`.) */
export default function WritingTab({ saved, version, onSaved, page }: TabProps<Writing> & { page: RulesPage }) {
  const me = useMeDoc<Writing>({
    saved,
    version,
    parts: WRITING_PAGE_PARTS[page],
    prune: pruneWriting,
    onSaved,
    save: async (doc, why, base) => (await api.saveProfile("writing", doc, why, base)).version ?? base,
  });
  const w = me.doc;
  const checks = useResource(() => api.ruleChecks(), [], { fallback: "Couldn't load the checks the app runs." });
  // The style rules the app ships; the writing document says which of them are off. A server that sends none draws none.
  const styleRules = checks.data?.style ?? [];
  const folds = useFolds(() => foldDefaults(saved));
  const set = (patch: Partial<Writing>) => me.edit({ ...w, ...patch });
  const shows = (def: PartDef) => WRITING_PAGE_PARTS[page].includes(def);

  const part = (def: PartDef, extra: { count?: string; summary?: string; hint?: string }, children: ReactNode) => (
    <FoldPart
      id={def.id}
      title={def.title}
      hint={extra.hint ?? def.hint}
      open={folds.isOpen(def.id)}
      onToggle={() => folds.toggle(def.id)}
      dirty={me.dirtyIds.includes(def.id)}
      count={extra.count}
      summary={extra.summary}
    >
      {children}
    </FoldPart>
  );

  const rulesList = (key: RuleKey, noun: string, label: string) => (
    <RuleList items={w[key]} onChange={(items) => me.edit({ ...w, [key]: items })} noun={noun} label={label} />
  );
  /** One of the sections that is only a list of rules. */
  const ruleSection = (def: PartDef, key: RuleKey, label: string) =>
    part(def, { count: plural(w[key].length, "rule"), summary: preview(sayings(w[key]), 3) }, rulesList(key, "rule", label));
  /** A list's name, with how many it holds. A list the owner reads as a whole (the style rules, the certifications) has no count. */
  const listName = (title: string, n?: number) => (
    <h3 className="fi-me-list-name">
      {n === undefined ? title : <>{title} <span className="fi-me-count">{n}</span></>}
    </h3>
  );

  return (
    <div className="fi-me-page">
      <div className="fi-me-parts">
        {shows(VOICE_PART) && ruleSection(VOICE_PART, "voice_rules", "Voice rule")}
        {shows(ATS_PART) && ruleSection(ATS_PART, "ats_rules", "ATS rule")}
        {shows(INTERVIEW_PART) && ruleSection(INTERVIEW_PART, "interview_rules", "Interview rule")}

        {shows(QA_PART) && part(
          QA_PART,
          { hint: QA_LINE, count: plural(qaLines(w).length, "item"), summary: QA_LINE },
          <>
            {listName("The review every packet gets", w.review_steps.length)}
            {rulesList("review_steps", "step", "Review step")}
            {listName("Drift reviewers catch", w.drift_patterns.length)}
            <ItemList
              items={w.drift_patterns}
              onChange={(drift_patterns) => set({ drift_patterns })}
              noun="pattern"
              blank={BLANK_DRIFT}
              describe={(d) => nameOf(d.name, "a new pattern")}
              className="fi-me-cols-drift"
              heads={["Pattern", "What it looked like", "Fix"]}
              render={(d, _i, update) => (
                <>
                  <Text label="Pattern" column value={d.name} onChange={(name) => update({ name })} />
                  <Text label="What it looked like" column value={d.looks_like} onChange={(looks_like) => update({ looks_like })} />
                  <Text label="Fix" column value={d.fix} onChange={(fix) => update({ fix })} />
                </>
              )}
            />
            {listName("Flagged for you, not fixed", w.review_flags.length)}
            {rulesList("review_flags", "flag", "Flag")}
            {listName("Never write", w.banned.length)}
            <p className="fi-me-hint">{BANNED_LINE}</p>
            <ItemList
              items={w.banned}
              onChange={(banned) => set({ banned })}
              noun="phrase"
              blank={BLANK_BANNED}
              describe={(b) => nameOf(b.phrase, "a new phrase")}
              className="fi-me-cols-banned"
              heads={["Phrase", "Why, and what to write instead"]}
              render={(b, _i, update) => (
                <>
                  <Text label="Phrase" column value={b.phrase} onChange={(phrase) => update({ phrase })} />
                  <Text label="Why, and what to write instead" column value={b.why} onChange={(why) => update({ why })} />
                </>
              )}
            />
            {styleRules.length > 0 && (
              <>
                {listName("Style rules")}
                <p className="fi-me-hint">{STYLE_LINE}</p>
                <ul className="fi-me-fixed">
                  {styleRules.map((s) => (
                    <li key={s.id}>
                      <Check
                        label={s.name}
                        checked={!w.style_off.includes(s.id)}
                        onChange={(on) => set({ style_off: withStyleSwitch(w.style_off, styleRules, s.id, on) })}
                      />
                      <span>{s.rule}</span>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {listName("Certifications and licences")}
            <p className="fi-me-hint">{CREDENTIALS_LINE}</p>
            <ItemList
              items={w.credentials}
              onChange={(credentials) => set({ credentials })}
              noun="certification or licence"
              blank={BLANK_CREDENTIAL}
              describe={(c) => nameOf(c.name, "a new certification or licence")}
              className="fi-me-cols-name"
              heads={["Name"]}
              render={(c, _i, update) => <Text label="Name" column oneLine value={c.name} onChange={(name) => update({ name })} />}
            />
            {checks.data && checks.data.checks.length > 0 && (
              <>
                {listName("Checked automatically", checks.data.checks.length)}
                <p className="fi-me-hint">{CHECKED_LINE}</p>
                <ul className="fi-me-fixed">
                  {checks.data.checks.map((c) => (
                    <li key={c.id}>
                      <strong>{c.name}</strong>
                      <span>{c.rule}</span>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {w.legacy_patterns.length > 0 && (
              <>
                {listName("To review", w.legacy_patterns.length)}
                <p className="fi-me-hint">{REVIEW_LINE}</p>
                <ul className="fi-me-fixed">
                  {w.legacy_patterns.map((l) => (
                    <li key={l.id}>
                      <strong>{l.pattern}</strong>
                      <span>{l.why || "Never written."}{" "}
                        <button type="button" className="fi-btn" aria-label={`Remove ${l.pattern}`}
                          onClick={() => set({ legacy_patterns: w.legacy_patterns.filter((x) => x.id !== l.id) })}>Remove</button>
                      </span>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {listName("Humanizing documents", w.humanizing.length)}
            {rulesList("humanizing", "rule", "Humanizing rule")}
          </>,
        )}
      </div>

      {me.dirty && <SaveBar ref={me.bar.ref} what={me.bar.what} why={me.bar.why} onWhy={me.bar.onWhy} busy={me.bar.busy} error={me.bar.error} stale={me.bar.stale} onUndo={me.bar.onUndo} onSave={me.bar.onSave} />}
    </div>
  );
}
