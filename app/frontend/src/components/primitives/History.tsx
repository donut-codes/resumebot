import { api } from "../../api/client";
import type { DocumentPart } from "../../api/types/me";
import type { ProfilePart, ProfileVersion } from "../../api/types/profile";
import { dateTimeInWords } from "../../lib/dates";
import { useResource } from "../../lib/useResource";
import { useRefreshKey } from "../../lib/appContext";

type Kind = ProfilePart | DocumentPart;

function savedBy(who: string): string {
  if (who === "owner") return "You";
  if (who === "import") return "Imported";
  return who.charAt(0).toUpperCase() + who.slice(1);
}

/** Every saved version of one document, newest first: who saved it, when, why, and what changed. */
export function History({ kind, saves = 0 }: { kind: Kind; saves?: number }) {
  const refreshKey = useRefreshKey();
  const { data, error } = useResource(() => api.profileHistory(kind), [kind, refreshKey, saves], {
    fallback: "Couldn't load the history.",
    reset: true,
  });
  if (error)
    return (
      <p className="fi-alert" role="alert">
        {error}
      </p>
    );
  if (!data) return <p className="fi-me-loading">Loading the history&hellip;</p>;
  if (data.versions.length === 0) return <p className="fi-me-empty">Never saved.</p>;
  return (
    <ul className="fi-me-history">
      {data.versions.map((v: ProfileVersion) => (
        <li key={v.version}>
          <div className="fi-me-history-head">
            <strong>Version {v.version}</strong>
            <span>
              {savedBy(v.saved_by)}
              {v.saved_at && `, ${dateTimeInWords(v.saved_at)}`}
            </span>
            {/* The master resume's versions each export as their own JSON Resume file (the writing rules' do not). */}
            {kind === "resume" && (
              <a
                className="fi-link"
                href={`/api/v1/documents/resume/versions/${v.version}/export.json`}
                download="resume.json"
                aria-label={`Export version ${v.version} (JSON Resume)`}
              >
                Export
              </a>
            )}
          </div>
          {v.why && <p className="fi-me-history-why">{v.why}</p>}
          {v.changed.length > 0 && <p className="fi-me-quiet">{v.changed.join("; ")}</p>}
        </li>
      ))}
    </ul>
  );
}
