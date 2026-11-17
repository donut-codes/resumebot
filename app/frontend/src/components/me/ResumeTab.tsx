import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../../api/client";
import type { About, Resume, ResumeBullet, ResumeEvidence, ResumeLine, ResumeRole } from "../../api/types/me";
import type { ParsedResume } from "../../api/types/profile";
import { plural } from "../../lib/text";
import SaveBar from "./SaveBar";
import FoldPart, { useFolds } from "./FoldPart";
import { MeEmpty } from "./MeSection";
import FactsList, { filledFacts } from "./Facts";
import { Check, Note, Terms, Text } from "../primitives/fields";
import ItemList, { nameOf } from "./ItemList";
import {
  BLANK_BULLET, BLANK_COMPETENCY, BLANK_DEGREE, BLANK_EVIDENCE, BLANK_LINE, BLANK_ROLE, FACTS, FACTS_PART, FOLD_OVER_LINES, MASTER_PARTS, type Master,
  hasExport, linesOf, masterOf, preview, pruneResume, splitMaster, wordCount,
} from "./meParts";
import { planImport, type ImportPlan } from "./importMerge";
import ImportReview from "./ImportReview";
import ClaudeSuggestions, { type SuggestionCalls } from "./ClaudeSuggestions";
import { useMeDoc } from "./useMeDoc";

const open = (lines: number) => lines <= FOLD_OVER_LINES;
const roleLines = (x: ResumeRole) => linesOf(x.intro, ...x.bullets.map((b) => b.text));

/** Which parts start open: the short ones. A part longer than about 30 lines starts folded. Inside Experience a role starts
 * folded when it is that long itself, or when Experience is (opening a long Experience shows the roles to pick from, not
 * every bullet of every role). */
function foldDefaults(r: Resume): Record<string, boolean> {
  const rolesShort = open(r.roles.reduce((n, x) => n + roleLines(x), 0));
  return {
    summary: open(linesOf(r.summary)),
    achievements: open(linesOf(...r.achievements.map((a) => a.text))),
    competencies: open(linesOf(...r.competencies.map((c) => `${c.area}: ${c.items.join("; ")}`))),
    roles: rolesShort,
    education: open(linesOf(...r.education.map((e) => `${e.degree}, ${e.school}`))),
    philosophy: open(linesOf(r.philosophy)),
    awards: open(linesOf(...r.awards.map((a) => a.text))),
    projects: open(linesOf(...r.projects.map((a) => a.text))),
    ...Object.fromEntries(r.roles.map((x) => [`role-${x.id}`, rolesShort && open(roleLines(x))])),
  };
}

const roleTitle = (x: ResumeRole) => [x.title || "A new role", x.employer].filter(Boolean).join(", ");

/** A line of the resume, with its note under it when notes are shown. */
function LineFields({ item, set, label, notes }: { item: ResumeLine; set: (patch: Partial<ResumeLine>) => void; label: string; notes: boolean }) {
  return (
    <>
      <Text label={label} hideLabel value={item.text} onChange={(text) => set({ text })} />
      {notes && <Note value={item.note} onChange={(note) => set({ note })} />}
    </>
  );
}

/** A resume the person imported (Import your resume), as it was read: `n` counts the imports, so each one is taken once. */
export interface Imported {
  n: number;
  fields: ParsedResume;
}

const SUGGESTION_CALLS: SuggestionCalls = {
  open: () => api.openGapProposals(),
  accept: (id, text, baseVersion) => api.acceptGapProposal(id, text, baseVersion),
  reject: (id) => api.rejectGapProposal(id),
};

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

interface Props {
  /** The master resume as saved. */
  saved: Resume;
  /** The facts forms ask for live in the about document: the page edits them beside the resume. */
  savedAbout: About;
  /** The versions of the two documents that are (each save of one names its own, so a page left open cannot write over newer work). */
  versions: { resume: number; about: number };
  onSavedResume: (doc: Resume, version: number) => void;
  onSavedAbout: (doc: About, version: number) => void;
  imported: Imported | null;
}

/** The master resume, edited where it stands: the facts forms ask for first (a resume opens with a name and contact
 * details), then one folded or open part for each of its sections, with one Save for the page. The facts are the about
 * document and the rest is the resume: the page edits them as one draft, and a Save sends whichever of the two changed,
 * with the same why. A resume imported while the page is open (`imported`) is first shown as what it would add (ImportReview:
 * new lines, lines beside a similar one, repeats skipped; nothing removed or reworded), and once the owner confirms it is
 * added to the parts, and to the facts left empty, as unsaved changes, to look over and save or undo like any other. */
export default function ResumeTab({ saved, savedAbout, versions, onSavedResume, onSavedAbout, imported }: Props) {
  const master = useMemo(() => masterOf(saved, savedAbout), [saved, savedAbout]);
  // What is saved now, as the page last heard: a save that sends the resume and then fails on the about document has
  // still saved the resume, and sending again must not save it twice.
  const latest = useRef({ resume: saved, about: savedAbout });
  latest.current = { resume: saved, about: savedAbout };
  // The version a document got from a save of this draft that went through while the other failed: the draft's next
  // save names that one for it, not the version the draft was started from.
  const mine = useRef<Partial<Record<"resume" | "about", number>>>({});
  const me = useMeDoc<Master, { resume: number; about: number }>({
    saved: master,
    version: versions,
    parts: MASTER_PARTS,
    prune: pruneResume,
    onSaved: () => {},
    save: async (doc, why, base) => {
      const now = latest.current;
      const sent = splitMaster(doc, now.about);
      // What is saved, put through the same shaping, so a save that changed only the facts sends only the facts.
      const held = splitMaster(pruneResume(masterOf(now.resume, now.about)), now.about);
      const after = { ...base };
      if (!same(sent.resume, held.resume)) {
        const from = mine.current.resume ?? base.resume;
        after.resume = (await api.saveProfile("resume", sent.resume, why, from)).version ?? from;
        mine.current.resume = after.resume;
        latest.current = { ...latest.current, resume: sent.resume };
        onSavedResume(sent.resume, after.resume);
      }
      if (!same(sent.about, held.about)) {
        const from = mine.current.about ?? base.about;
        after.about = (await api.saveProfile("about", sent.about, why, from)).version ?? from;
        mine.current.about = after.about;
        latest.current = { ...latest.current, about: sent.about };
        onSavedAbout(sent.about, after.about);
      }
      mine.current = {};
      return after;
    },
  });
  // Nothing typed any more (saved or put back): the next draft starts from the versions the page holds.
  useEffect(() => {
    if (!me.dirty) mine.current = {};
  }, [me.dirty]);
  const r = me.doc;
  const [notes, setNotes] = useState(false);
  const folds = useFolds(() => ({ facts: true, ...foldDefaults(saved) }));
  const set = (patch: Partial<Master>) => me.edit({ ...r, ...patch });

  // An import that arrives while the page is open is taken once, into the draft; one that was already there when the page
  // opened (the person left and came back) was taken before.
  const taken = useRef(imported?.n ?? 0);
  // What it would add is shown first (ImportReview); nothing reaches the draft until the owner confirms it.
  const [review, setReview] = useState<ImportPlan | null>(null);
  const [filled, setFilled] = useState<{ contact: boolean } | null>(null);
  useEffect(() => {
    if (!imported || imported.n === taken.current) return;
    taken.current = imported.n;
    setReview(planImport(me.doc, imported.fields));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imported]);
  // Saved or put back: the note about a filled resume has done its job.
  useEffect(() => {
    if (!me.dirty) setFilled(null);
  }, [me.dirty]);

  const part = (id: string, extra: { count?: string; summary?: string }, children: ReactNode) => {
    const def = MASTER_PARTS.find((p) => p.id === id)!;
    return (
      <FoldPart
        id={id}
        title={def.title}
        hint={def.hint}
        open={folds.isOpen(id)}
        onToggle={() => folds.toggle(id)}
        dirty={me.dirtyIds.includes(id)}
        count={extra.count}
        summary={extra.summary}
      >
        {children}
      </FoldPart>
    );
  };

  const lines = (key: "achievements" | "awards" | "projects", noun: string, label: string) => (
    <ItemList<ResumeLine>
      items={r[key]}
      onChange={(items) => me.edit({ ...r, [key]: items })}
      noun={noun}
      blank={BLANK_LINE}
      describe={(a) => nameOf(a.text, `a new ${noun}`)}
      render={(a, i, update) => <LineFields item={a} set={update} label={`${label} ${i + 1}`} notes={notes} />}
    />
  );
  const lineCount = (items: ResumeLine[], one: string) => plural(items.length, one);
  const lineSummary = (items: ResumeLine[]) => preview(items.map((a) => a.text), 3);

  const empty = !r.summary && r.roles.length === 0 && r.achievements.length === 0;
  const dateOf = (x: ResumeRole) => [x.start, x.end].filter(Boolean).join(" – ");

  return (
    <div className="fi-me-page">
      <div className="fi-me-tools">
        {/* The saved version, as a JSON Resume file another tool can import: notes, guardrails and evidence stay out (PRD 8.4). */}
        {versions.resume > 0 && hasExport(saved) && (
          <>
            <a className="fi-btn" href="/api/v1/documents/resume/export.json" download="resume.json">
              Export the saved master (JSON Resume)
            </a>
            {me.dirty && <p className="fi-me-quiet">Your unsaved changes are not in it.</p>}
          </>
        )}
        <Check label="Show notes and groups" checked={notes} onChange={setNotes} />
      </div>
      {filled && (
        <p className="fi-me-filled" role="status">
          Added from your resume: look it over, then Save.
          {filled.contact && " Contact details you had not filled in were added too."}
        </p>
      )}
      {empty && <MeEmpty>No master resume yet. Write below, or use Import your resume above.</MeEmpty>}
      <ClaudeSuggestions calls={SUGGESTION_CALLS} baseVersion={versions.resume} dirty={me.dirty} onSavedResume={onSavedResume} />

      <div className="fi-me-parts">
        {part(
          FACTS_PART.id,
          { count: `${filledFacts(r)} of ${FACTS.length} filled in`, summary: preview([r.name, r.location, r.email, r.phone]) },
          <FactsList facts={r} onChange={(next) => me.edit({ ...r, ...next })} />,
        )}

        {part(
          "summary",
          { count: plural(wordCount(r.summary), "word"), summary: preview([r.summary], 1) },
          <Text label="Summary" hideLabel rows={3} value={r.summary} onChange={(summary) => set({ summary })} />,
        )}

        {part(
          "competencies",
          { count: plural(r.competencies.length, "area"), summary: preview(r.competencies.map((c) => c.area)) },
          <ItemList
            items={r.competencies}
            onChange={(competencies) => set({ competencies })}
            noun="area"
            blank={BLANK_COMPETENCY}
            describe={(c) => nameOf(c.area, "a new area")}
            className="fi-me-cols-area"
            heads={["Area", "Terms"]}
            render={(c, _i, update) => (
              <>
                <Text label="Area" column oneLine value={c.area} onChange={(area) => update({ area })} />
                <Terms label="Terms" column items={c.items} onChange={(items) => update({ items })} />
              </>
            )}
          />,
        )}

        {part("achievements", { count: lineCount(r.achievements, "achievement"), summary: lineSummary(r.achievements) }, lines("achievements", "achievement", "Achievement"))}

        {part(
          "roles",
          { count: plural(r.roles.length, "role"), summary: preview(r.roles.map(roleTitle), 4) },
          <ItemList<ResumeRole>
            items={r.roles}
            onChange={(roles) => set({ roles })}
            noun="role"
            blank={BLANK_ROLE}
            describe={(x) => nameOf(roleTitle(x), "a new role")}
            className="fi-me-flush"
            render={(x, _i, update) => {
              const id = `role-${x.id}`;
              return (
                <FoldPart
                  id={id}
                  level={3}
                  title={roleTitle(x)}
                  open={folds.isOpen(id)}
                  onToggle={() => folds.toggle(id)}
                  count={dateOf(x)}
                  tag={x.optional ? <span className="fi-badge fi-me-tag">Optional</span> : undefined}
                  summary={x.bullets.length ? `${plural(x.bullets.length, "bullet")}: ${preview(x.bullets.map((b) => b.text), 2)}` : "No bullets yet"}
                >
                  <div className="fi-me-role-grid">
                    <Text label="Title" oneLine className="fi-me-span-6" value={x.title} onChange={(title) => update({ title })} />
                    <Text label="Employer" oneLine className="fi-me-span-6" value={x.employer} onChange={(employer) => update({ employer })} />
                    <Text label="Where" oneLine className="fi-me-span-6" value={x.location} onChange={(location) => update({ location })} />
                    <Text label="From" oneLine className="fi-me-span-3" value={x.start} onChange={(start) => update({ start })} />
                    <Text label="To" oneLine className="fi-me-span-3" value={x.end} onChange={(end) => update({ end })} />
                  </div>
                  <Text label="Scope line" value={x.intro} onChange={(intro) => update({ intro })} />
                  <Text label="What the employer is" hint="A half line" oneLine value={x.descriptor} onChange={(descriptor) => update({ descriptor })} />
                  <Check label="Left off unless something in it serves a qualification the posting asks for." checked={x.optional} onChange={(optional) => update({ optional })} />
                  {notes && <Note value={x.note} onChange={(note) => update({ note })} />}
                  <Terms label="Tools used" hint="Separate with semicolons" items={x.tools} onChange={(tools) => update({ tools })} />
                  <h4 className="fi-me-list-name">
                    Bullets <span className="fi-me-count">{x.bullets.length}</span>
                  </h4>
                  <ItemList<ResumeBullet>
                    items={x.bullets}
                    onChange={(bullets) => update({ bullets })}
                    noun="bullet"
                    blank={BLANK_BULLET}
                    describe={(b) => nameOf(b.text, "a new bullet")}
                    render={(b, i, updateBullet) => (
                      <>
                        <Text label={`Bullet ${i + 1} of ${roleTitle(x)}`} hideLabel value={b.text} onChange={(text) => updateBullet({ text })} />
                        {notes && (
                          <div className="fi-me-bullet-meta">
                            <Text label="Group, for sorting" hideLabel placeholder="Group, for sorting" className="fi-me-note" oneLine value={b.group} onChange={(group) => updateBullet({ group })} />
                            <Note value={b.note} onChange={(note) => updateBullet({ note })} />
                          </div>
                        )}
                      </>
                    )}
                  />
                  <h4 className="fi-me-list-name">
                    Evidence <span className="fi-me-count">{x.evidence.length}</span>
                  </h4>
                  <p className="fi-me-hint">Never printed: detail from this job the analyst cites and the writer draws on.</p>
                  <ItemList<ResumeEvidence>
                    items={x.evidence}
                    onChange={(evidence) => update({ evidence })}
                    noun="entry"
                    addLabel="Add evidence"
                    blank={BLANK_EVIDENCE}
                    describe={(e) => nameOf(e.title, "a new entry")}
                    render={(e, _i, updateEvidence) => (
                      <>
                        <Text label={`Evidence title in ${roleTitle(x)}`} oneLine value={e.title} onChange={(title) => updateEvidence({ title })} />
                        <Text label={`Evidence detail in ${roleTitle(x)}`} rows={2} value={e.text} onChange={(text) => updateEvidence({ text })} />
                      </>
                    )}
                  />
                </FoldPart>
              );
            }}
          />,
        )}

        {part(
          "education",
          { count: plural(r.education.length, "degree"), summary: preview(r.education.map((e) => e.degree)) },
          <ItemList
            items={r.education}
            onChange={(education) => set({ education })}
            noun="degree"
            blank={BLANK_DEGREE}
            describe={(e) => nameOf(e.degree, "a new degree")}
            render={(e, _i, update) => (
              <>
                <div className="fi-me-pair">
                  <Text label="Degree" oneLine value={e.degree} onChange={(degree) => update({ degree })} />
                  <Text label="School" oneLine value={e.school} onChange={(school) => update({ school })} />
                </div>
                {notes && <Note value={e.note} onChange={(note) => update({ note })} />}
              </>
            )}
          />,
        )}

        {part("awards", { count: lineCount(r.awards, "award"), summary: lineSummary(r.awards) }, lines("awards", "award", "Award"))}

        {part("projects", { count: lineCount(r.projects, "project"), summary: lineSummary(r.projects) }, lines("projects", "project", "Project"))}

        {part(
          "philosophy",
          { count: plural(wordCount(r.philosophy), "word"), summary: preview([r.philosophy], 1) },
          <Text label="Leadership philosophy" hideLabel rows={3} value={r.philosophy} onChange={(philosophy) => set({ philosophy })} />,
        )}
      </div>

      {me.dirty && <SaveBar ref={me.bar.ref} what={me.bar.what} why={me.bar.why} onWhy={me.bar.onWhy} busy={me.bar.busy} error={me.bar.error} stale={me.bar.stale} onUndo={me.bar.onUndo} onSave={me.bar.onSave} />}
      {review && (
        <ImportReview
          plan={review}
          onConfirm={() => {
            me.edit(review.merged);
            setFilled({ contact: review.contact });
            setReview(null);
          }}
          onClose={() => setReview(null)}
        />
      )}
    </div>
  );
}
