import { useState } from "react";
import { api } from "../../api/client";
import type { GapRunView, Resume } from "../../api/types/me";
import { useAction } from "../../lib/useAction";
import { MeAlert, MeSection } from "./MeSection";
import GapClarify, { type ClarifyActions } from "./GapClarify";
import GapTheme from "./GapTheme";
import { KIND_WORDS, runLine } from "./gaps";

interface Props {
  run: GapRunView;
  /** What the owner can ask of the Coach on a run started from a job. */
  clarifyActions: ClarifyActions;
  resume: Resume;
  resumeVersion: number;
  /** Whether an agent can take work now, and why not in words; Check again and New sample hold to the same rule. */
  ready: boolean;
  why: string | null;
  whyId: string;
  /** Another run is going, so none can be started. */
  going: boolean;
  onCheckAgain: () => void;
  onNewSample: () => void;
  /** A run of a title that is not a target role: make it one. */
  onAddRole: () => void;
  addedRole: boolean;
  /** Starting a run, or adding the role, is under way. */
  busy: boolean;
  error: string | null;
  /** The run is read again (an answer was given, or taken back). */
  onChanged: () => void;
  onSavedResume: (resume: Resume, version: number) => void;
}

/** A run of Find Gaps under the role's name: while it works, how many postings it has read; failed, why; done, what it read,
 * when, whether the resume has changed since (Check again), a fresh sample (New sample), and the themes most postings ask
 * first. The themes the owner set aside are counted in the foot ("2 hidden") and can be brought back. */
export default function GapRun({ run, clarifyActions, resume, resumeVersion, ready, why, whyId, going, onCheckAgain, onNewSample, onAddRole, addedRole, busy, error, onChanged, onSavedResume }: Props) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [showHidden, setShowHidden] = useState(false);
  const bringBack = useAction(
    async (answerId: string) => {
      await api.takeBackGapAnswer(answerId);
      onChanged();
    },
    { fallback: "Couldn't bring it back." },
  );
  const toggle = (id: string) =>
    setOpen((o) => {
      const next = new Set(o);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  const off = !ready || going || busy;
  const offTitle = !ready ? (why ?? undefined) : going ? "A Find Gaps run is already going." : undefined;
  const describedBy = !ready && why ? whyId : undefined;

  if (run.state === "waiting" || run.state === "working") {
    const share = run.total > 0 ? Math.min(run.read / run.total, 1) : 0;
    return (
      <MeSection id="gap-run" title={run.title} className="fi-me-gap-run-sec">
        <div className="fi-me-gap-run-bar">
          <div className="fi-me-gap-run-progress">
            <p className="fi-me-gap-run-line" role="status">
              {run.step}
            </p>
            {run.state === "working" && (
              <div className="fi-me-track" role="progressbar" aria-label="Postings read" aria-valuemin={0} aria-valuemax={run.total} aria-valuenow={run.read}>
                <div className="fi-me-fill" style={{ transform: `scaleX(${share})` }} />
              </div>
            )}
          </div>
        </div>
      </MeSection>
    );
  }

  if (run.state === "failed") {
    return (
      <MeSection id="gap-run" title={run.title} className="fi-me-gap-run-sec">
        <div className="fi-me-gap-run-bar">
          <MeAlert>{run.error || "The run stopped."}</MeAlert>
        </div>
      </MeSection>
    );
  }

  return (
    <MeSection id="gap-run" title={run.title} className="fi-me-gap-run-sec">
      <div className="fi-me-gap-run-bar">
        <div>
          <p className="fi-me-gap-run-line">{runLine(run)}</p>
          {run.note && <p className="fi-me-quiet">{run.note}</p>}
          {run.stale && <p className="fi-me-stale">Your resume changed since this check</p>}
        </div>
        <div className="fi-me-gap-run-actions">
          {run.can_repeat ? (
            <>
              <button type="button" className="fi-btn" disabled={off} title={offTitle} aria-describedby={describedBy} onClick={onCheckAgain}>
                Check again
              </button>
              <button type="button" className="fi-btn" disabled={off} title={offTitle} aria-describedby={describedBy} onClick={onNewSample}>
                New sample
              </button>
            </>
          ) : (
            !run.job && (
              <button type="button" className="fi-btn" disabled={busy || addedRole} onClick={onAddRole}>
                {addedRole ? "Added as a target role" : "Add as a target role"}
              </button>
            )
          )}
        </div>
      </div>
      {error && <MeAlert>{error}</MeAlert>}

      {run.themes.length > 0 && (
        <div className="fi-me-gap-listing fi-me-gap-themes">
          <div className="fi-me-gap-columns">
            <span>Theme</span>
            <span>Kind</span>
            <span>Asked by</span>
            <span>Your resume</span>
          </div>
          {run.themes.map((t) => (
            <GapTheme
              key={t.id}
              runId={run.id}
              theme={t}
              postings={run.postings}
              open={open.has(t.id)}
              onToggle={() => toggle(t.id)}
              resume={resume}
              resumeVersion={resumeVersion}
              ready={ready}
              why={why}
              whyId={whyId}
              onAnswered={onChanged}
              onSavedResume={onSavedResume}
            />
          ))}
        </div>
      )}
      {run.themes.length === 0 && run.hidden.length === 0 && <p className="fi-me-gap-none">No theme came out of these postings.</p>}
      {run.clarify && <GapClarify runId={run.id} actions={clarifyActions} clarify={run.clarify} ready={ready} why={why} whyId={whyId} onChanged={onChanged} onSavedResume={onSavedResume} />}

      {run.hidden.length > 0 && (
        <div className="fi-me-gap-foot">
          <button type="button" className="fi-link" aria-expanded={showHidden} onClick={() => setShowHidden((s) => !s)}>
            {run.hidden.length} hidden
          </button>
        </div>
      )}
      {showHidden && run.hidden.length > 0 && (
        <ul className="fi-me-gap-hidden">
          {run.hidden.map((h) => (
            <li key={h.id}>
              <span>
                {h.name} <span className="fi-badge fi-me-tag">{KIND_WORDS[h.kind as keyof typeof KIND_WORDS] ?? h.kind}</span>
              </span>
              <button type="button" className="fi-btn" disabled={bringBack.busy} onClick={() => void bringBack.run(h.id)}>
                Bring back
              </button>
            </li>
          ))}
        </ul>
      )}
      {bringBack.error && <MeAlert>{bringBack.error}</MeAlert>}
    </MeSection>
  );
}
