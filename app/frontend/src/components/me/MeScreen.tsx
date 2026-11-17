import { useId, useState } from "react";
import { api } from "../../api/client";
import type { About, DocumentPart, Resume, Writing } from "../../api/types/me";
import type { ParsedResume } from "../../api/types/profile";
import { useRefreshKey, useRoute } from "../../lib/appContext";
import { ME_PAGES, routeFor, serializeRoute, type MePage } from "../../lib/route";
import { useAgentReady } from "../../lib/useAgentReady";
import { useResource } from "../../lib/useResource";
import { Rail, RailEntry, RailGroup } from "../focus/parts";
import { History } from "../primitives/History";
import { MeAlert, MeLoading, MeSection } from "./MeSection";
import FindGaps from "./FindGaps";
import ImportResume from "./ImportResume";
import ResumeTab, { type Imported } from "./ResumeTab";
import StoryLibraryTab from "./StoryLibraryTab";
import SectionedRulesTab from "./SectionedRulesTab";
import WritingTab from "./WritingTab";
import { plural } from "../../lib/text";

const PART_LABEL: Record<MePage, string> = {
  resume: "Master resume",
  "find-gaps": "Find Gaps",
  "story-library": "Story library",
  "resume-rules": "Resume Rules",
  "letter-rules": "Cover Letter Rules",
  "document-rules": "Document Creation Rules",
};

const PART_ABOUT: Record<MePage, string> = {
  resume: "The single source every tailored resume is cut from, with the facts forms ask for. Notes say how a line may be used; they are never printed.",
  "find-gaps": "What postings for your target roles ask for that your master resume does not yet show, and what you can add.",
  "story-library": "The scope lines, achievements and stories the writer draws on, built from your master resume.",
  "resume-rules": "How a resume is written and laid out: rules for the whole resume, for each of its sections and for each target role.",
  "letter-rules": "How a cover letter is written and laid out: rules for the whole letter, for each of its parts and for each target role.",
  "document-rules": "How anything in your name is written: your voice, ATS screening, and how documents are checked.",
};

/** The stored document a page shows the history of: the three rules pages are all the one writing document. */
const DOCUMENT_OF: Record<MePage, DocumentPart> = {
  resume: "resume",
  "find-gaps": "resume",
  "story-library": "resume",
  "resume-rules": "writing",
  "letter-rules": "writing",
  "document-rules": "writing",
};

/** Me: the master resume (with the facts forms ask for) and the writing rules in three pages, one at a time, each written
 * where it is read (PRD paths 8, 9 and 19). A Focus screen: the rail lists the six parts, and the pane holds the one open. Each
 * page is a list of parts (white sheets) that fold, with one Save for the page; the history of the page is beside its name.
 * Packets are cut from these. */
export default function MeScreen() {
  const refreshKey = useRefreshKey();
  const { route, go } = useRoute();
  const part = route.mePart;
  const [showHistory, setShowHistory] = useState(false);
  const [saves, setSaves] = useState(0);
  // A resume that was imported (Import your resume): the master resume takes it into its parts, and its contact details into
  // the facts at the top. Both are unsaved changes there, to look over and save or undo.
  const [imported, setImported] = useState<Imported | null>(null);
  const { data, error, setData } = useResource(() => api.documents(), [refreshKey, saves], { fallback: "Couldn't load your documents." });
  // Whether an agent is free to take work, asked only where something starts it: Import your resume on the master resume, and
  // Find gaps on its own page (the same rule, and the same sentence).
  const asks = part === "resume" || part === "find-gaps";
  const ready = useAgentReady(asks);
  const whyId = useId();
  const noOne = part === "find-gaps" ? "No agent is free to find gaps right now." : "No agent is free to read a resume right now.";
  const whyNot = asks && ready.data && !ready.data.ready ? ready.data.why || noOne : null;

  if (error && !data) return <MeAlert>{error}</MeAlert>;
  if (!data) return <MeLoading>Loading your documents&hellip;</MeLoading>;

  // A save shows at once; the page then loads again, for the version counts and the history.
  // The version in force goes in with it, so the next draft names the right one before the page has loaded again.
  const savedResume = (resume: Resume, version: number) => {
    setData((d) => d && { ...d, resume, versions: { ...d.versions, resume: version } });
    setSaves((n) => n + 1);
  };
  const savedAbout = (about: About, version: number) => {
    setData((d) => d && { ...d, about, versions: { ...d.versions, about: version } });
    setSaves((n) => n + 1);
  };
  const savedWriting = (writing: Writing, version: number) => {
    setData((d) => d && { ...d, writing, versions: { ...d.versions, writing: version } });
    setSaves((n) => n + 1);
  };

  const doc = DOCUMENT_OF[part];
  const versions = data.versions[doc] ?? 0;
  const writingVersion = data.versions.writing ?? 0;
  const takeImport = (fields: ParsedResume) => setImported((i) => ({ n: (i?.n ?? 0) + 1, fields }));
  const hasHistory = part !== "find-gaps" && part !== "story-library";
  // The two rules pages with a read-only column beside them (the document as built) take a wider pane.
  const wide = part === "resume-rules" || part === "letter-rules";
  return (
    <div className="fi fi-me">
      <Rail label="My Resume">
        <RailGroup>
          {ME_PAGES.map((p) => (
            <RailEntry key={p} label={PART_LABEL[p]} current={p === part} href={serializeRoute(routeFor("me", { mePart: p }))} onPick={() => go(routeFor("me", { mePart: p }))} />
          ))}
        </RailGroup>
      </Rail>
      <div className="fi-pane">
        <div className={`fi-pane-in${wide ? " is-wide" : ""}`} key={part}>
          <h1 className="fi-vh">My Resume</h1>
          {/* What the part is, and its History (on the master resume, Import your resume beside it). Find Gaps has no history
              of its own: what it adds is in the master resume's. */}
          <div className="fi-head fi-me-head">
            <div>
              <p className="fi-me-title">{PART_LABEL[part]}</p>
              <p className="fi-head-meta fi-me-about">{PART_ABOUT[part]}</p>
            </div>
            {hasHistory && (
              <div className="fi-head-aside fi-me-actions">
                <button type="button" className="fi-btn" aria-expanded={showHistory} onClick={() => setShowHistory((s) => !s)}>
                  {showHistory ? "Hide history" : `History: ${plural(versions, "version")}`}
                </button>
                {part === "resume" && (
                  <ImportResume ready={ready.data?.ready ?? false} why={whyNot} whyId={whyId} onImported={takeImport} job={route.importJob} onJobClosed={() => go(routeFor("me"))} />
                )}
              </div>
            )}
          </div>
          {/* Why Import your resume, or Find gaps, is off (no agent has checked in, Claude's peak hours): one plain line. */}
          {whyNot && (
            <p id={whyId} className="fi-me-why">
              {whyNot}
            </p>
          )}
          {showHistory && hasHistory && (
            <MeSection id="me-history" title="History" className="fi-me-history-sec">
              <History kind={doc} saves={saves} />
            </MeSection>
          )}

          <section aria-label={PART_LABEL[part]} className="fi-me-tab">
            {part === "resume" && (
              <ResumeTab
                key="resume"
                saved={data.resume}
                savedAbout={data.about}
                versions={{ resume: data.versions.resume ?? 0, about: data.versions.about ?? 0 }}
                onSavedResume={savedResume}
                onSavedAbout={savedAbout}
                imported={imported}
              />
            )}
            {part === "find-gaps" && (
              <FindGaps
                key={part}
                resume={data.resume}
                resumeVersion={data.versions.resume ?? 0}
                onSavedResume={savedResume}
                ready={ready.data?.ready ?? false}
                why={whyNot}
                whyId={whyId}
              />
            )}
            {part === "story-library" && <StoryLibraryTab key={part} />}
            {part === "resume-rules" && <SectionedRulesTab key={part} kind="resume" saved={data.writing} version={writingVersion} onSaved={savedWriting} />}
            {part === "letter-rules" && <SectionedRulesTab key={part} kind="letter" saved={data.writing} version={writingVersion} onSaved={savedWriting} />}
            {part === "document-rules" && <WritingTab key={part} page={part} saved={data.writing} version={writingVersion} onSaved={savedWriting} />}
          </section>
        </div>
      </div>
    </div>
  );
}
