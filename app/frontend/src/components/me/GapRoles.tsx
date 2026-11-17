import { useId, useState } from "react";
import type { GapJobOption, GapJobRun, GapRole } from "../../api/types/me";
import { shortDateInWords } from "../../lib/dates";
import { plural } from "../../lib/text";
import { MeAlert, MeSection } from "./MeSection";
import { barWords, GRADES } from "./gaps";

interface Props {
  roles: GapRole[];
  /** The role whose run is shown below, if any. */
  shown: string | null;
  /** Show the role's latest run. */
  onOpen: (roleId: string) => void;
  /** Start a run for a target role, for a title typed, or from a job picked (docs/PRD.md 38.11). */
  onStart: (target: { role_id: string } | { title: string } | { opportunity_id: string }) => void;
  /** The roles the picker lists (null until it is first opened, when `onOpenPicker` asks for them), and the finished runs
   * started from a job, each to open again. */
  jobs: GapJobOption[] | null;
  /** The picker was opened (or focused): the page reads its list the first time. */
  onOpenPicker: () => void;
  jobRuns: GapJobRun[];
  /** The run started from a job that is shown below, if any. */
  shownRun: string | null;
  onOpenRun: (runId: string) => void;
  /** Whether an agent can take work now, and why not in words (peak hours...). */
  ready: boolean;
  why: string | null;
  whyId: string;
  /** Another run is going, so none can be started. */
  going: boolean;
  /** Starting a run is under way. */
  busy: boolean;
  /** Why a run could not start, in the server's words. */
  error: string | null;
}

/** The dashboard (docs/PRD.md 38.0): a row for each target role in the order of Search Settings' parts, with the researched roles
 * it rests on, how the analyst graded their required qualifications as a bar, the date of its latest check and a Find gaps
 * button; and under the rows a field for a role title that is not one of them, with its own button. */
export default function GapRoles({ roles, shown, onOpen, onStart, jobs, onOpenPicker, jobRuns, shownRun, onOpenRun, ready, why, whyId, going, busy, error }: Props) {
  const fieldId = useId();
  const jobFieldId = useId();
  const [title, setTitle] = useState("");
  const [job, setJob] = useState("");
  const off = !ready || going || busy;
  const offTitle = !ready ? (why ?? undefined) : going ? "A Find Gaps run is already going." : undefined;
  const describedBy = !ready && why ? whyId : undefined;
  return (
    <MeSection id="gap-roles" title="Target roles">
      <div className="fi-me-gap-listing fi-me-gap-roles">
        <div className="fi-me-gap-columns">
          <span>Role</span>
          <span>Researched</span>
          <span>Your resume against their postings</span>
          <span>Last check</span>
          <span />
        </div>
        {roles.map((r) => (
          <div key={r.id} className="fi-me-gap-row" data-open={shown === r.id}>
            <div className="fi-me-gap-role-name">
              {r.last_run || shown === r.id ? (
                <button type="button" className="fi-me-gap-role-open" aria-pressed={shown === r.id} onClick={() => onOpen(r.id)}>
                  {r.name || "A role with no name"}
                </button>
              ) : (
                r.name || "A role with no name"
              )}
            </div>
            <div className={r.researched ? undefined : "fi-me-quiet"}>{r.researched ? `from ${plural(r.researched, "role")}` : "No researched roles yet"}</div>
            <div>
              {r.percent ? (
                <>
                  <div className="fi-me-gap-bar" role="img" aria-label={barWords(r.percent)}>
                    {GRADES.map((g) => (
                      <span key={g.grade} data-grade={g.grade} style={{ width: `${r.percent?.[g.grade] ?? 0}%` }} />
                    ))}
                  </div>
                  <ul className="fi-me-gap-legend">
                    {GRADES.map((g) => (
                      <li key={g.grade} data-grade={g.grade}>
                        {g.words} <b>{r.percent?.[g.grade]}%</b>
                      </li>
                    ))}
                  </ul>
                </>
              ) : (
                <div className="fi-me-gap-bar" role="img" aria-label="No bar: no researched roles yet" />
              )}
            </div>
            <div>{r.last_run?.at ? `Checked ${shortDateInWords(r.last_run.at)}` : "Not checked"}</div>
            <div className="fi-me-gap-act">
              <button
                type="button"
                className="fi-btn"
                disabled={off}
                title={offTitle}
                aria-describedby={describedBy}
                aria-label={`Find gaps for ${r.name}`}
                onClick={() => onStart({ role_id: r.id })}
              >
                Find gaps
              </button>
            </div>
          </div>
        ))}
        {roles.length === 0 && <p className="fi-me-gap-none">You have no target role yet: add one in Search Settings, or try a role title below.</p>}
      </div>
      <div className="fi-me-gap-try">
        <label htmlFor={fieldId}>Try a new role</label>
        <div className="fi-me-gap-try-line">
          <input
            id={fieldId}
            className="fi-input"
            type="text"
            placeholder="Role title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && title.trim() && !off) onStart({ title: title.trim() });
            }}
          />
          <button
            type="button"
            className="fi-btn"
            disabled={off || !title.trim()}
            title={offTitle}
            aria-describedby={describedBy}
            aria-label="Find gaps for a new role"
            onClick={() => onStart({ title: title.trim() })}
          >
            Find gaps
          </button>
        </div>
      </div>
      <div className="fi-me-gap-try">
        <label htmlFor={jobFieldId}>Start from a job</label>
        <div className="fi-me-gap-try-line">
          <select
            id={jobFieldId}
            className="fi-input"
            value={job}
            onFocus={onOpenPicker}
            onMouseDown={onOpenPicker}
            onChange={(e) => setJob(e.target.value)}
          >
            <option value="">Pick one of your roles</option>
            {(jobs ?? []).map((j) => (
              <option key={j.id} value={j.id}>
                {[j.company, j.title].filter(Boolean).join(", ")}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="fi-btn"
            disabled={off || !job}
            title={offTitle}
            aria-describedby={describedBy}
            aria-label="Find gaps from a job"
            onClick={() => onStart({ opportunity_id: job })}
          >
            Find gaps
          </button>
        </div>
        {jobRuns.length > 0 && (
          <ul className="fi-me-gap-hidden">
            {jobRuns.map((r) => (
              <li key={r.id}>
                <button type="button" className="fi-me-gap-role-open" aria-pressed={shownRun === r.id} onClick={() => onOpenRun(r.id)}>
                  {[r.company, r.title].filter(Boolean).join(", ")}
                </button>
                <span className="fi-me-quiet">{r.at ? `Checked ${shortDateInWords(r.at)}` : "Not checked"}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      {error && <MeAlert>{error}</MeAlert>}
    </MeSection>
  );
}
