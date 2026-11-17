import type { ReactNode } from "react";
import type { FileVersion } from "../../api/types/agents";
import { dateTimeInWords } from "../../lib/dates";

/** The versions of a process file or of an instruction or template file, newest first (docs/PRD.md 16.2): the number,
 * when, who and why; an older one can be looked at, and the owner can restore it, which saves it as a new version. */
export default function VersionList({
  versions,
  busy,
  viewing,
  onView,
  onRestore,
  viewed,
  label,
}: {
  versions: FileVersion[];
  busy: boolean;
  /** The version being looked at, and what to show under it. */
  viewing: number | null;
  viewed: ReactNode;
  onView: (number: number) => void;
  onRestore: (number: number) => void;
  /** What the list is of, for its accessible name ("Versions of Search for roles"). */
  label: string;
}) {
  return (
    <ol className="how-version-list" aria-label={label}>
      {versions.map((v) => (
        <li key={v.number} className={v.current ? "how-version current" : "how-version"}>
          <div className="how-version-head">
            <strong>Version {v.number}</strong>
            {v.current && (
              <span className="fi-badge fi-state" data-ink="green">
                In use
              </span>
            )}
            {v.loads === false && (
              <span className="fi-badge fi-state" data-ink="oxblood">
                Not working
              </span>
            )}
            <span className="fi-note">
              {v.by ?? "resumebot"}
              {v.at ? `, ${dateTimeInWords(v.at)}` : ""}
            </span>
          </div>
          {v.why && <p className="how-version-why">{v.why}</p>}
          {v.loads === false && v.problem && <p className="how-version-problem">{v.problem}</p>}
          {v.summary && v.summary.length > 0 && (
            <ul className="how-version-changes fi-note">
              {v.summary.map((line, i) => (
                <li key={i}>{line}</li>
              ))}
            </ul>
          )}
          <div className="how-version-actions">
            <button
              type="button"
              className="how-toggle"
              aria-expanded={viewing === v.number}
              onClick={() => onView(v.number)}
            >
              {viewing === v.number ? "Hide this version" : "View this version"}
            </button>
            {v.can_restore && !v.current && v.loads !== false && (
              <button type="button" className="fi-btn" disabled={busy} onClick={() => onRestore(v.number)}>
                Restore this version
              </button>
            )}
          </div>
          {viewing === v.number && <div className="how-version-viewed">{viewed}</div>}
        </li>
      ))}
    </ol>
  );
}
