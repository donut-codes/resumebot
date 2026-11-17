import { useId } from "react";
import type { GapPosting, GapTheme as Theme, Resume } from "../../api/types/me";
import { safeHref } from "../../lib/links";
import { routeFor, serializeRoute } from "../../lib/route";
import { ChevronIcon } from "../focus/icons";
import GapFeedback from "./GapFeedback";
import { GRADES, KIND_WORDS } from "./gaps";

interface Props {
  runId: string;
  theme: Theme;
  /** The postings of the run, to list the ones that asked this theme. */
  postings: GapPosting[];
  open: boolean;
  onToggle: () => void;
  resume: Resume;
  resumeVersion: number;
  ready: boolean;
  why: string | null;
  whyId: string;
  onAnswered: () => void;
  onSavedResume: (resume: Resume, version: number) => void;
}

const MASTER = serializeRoute(routeFor("me"));

/** One theme of a run: its name, the master line that backs its grade (linked to the master), its kind, how many of the
 * postings ask it and its grade. Opened, it lists the postings that asked it, and, for a Transferable or Unproven one, what
 * the owner can say of it (`GapFeedback`). */
export default function GapTheme({ runId, theme, postings, open, onToggle, resume, resumeVersion, ready, why, whyId, onAnswered, onSavedResume }: Props) {
  const bodyId = useId();
  const grade = GRADES.find((g) => g.grade === theme.grade) ?? GRADES[2];
  const asked = postings.filter((p) => theme.postings.includes(p.ref));
  const [first, ...rest] = theme.lines;
  return (
    <div className="fi-me-gap-theme" data-open={open}>
      <div className="fi-me-gap-row">
        <div className="fi-me-gap-theme-name">
          <button type="button" className="fi-me-gap-theme-btn" aria-expanded={open} aria-controls={bodyId} onClick={onToggle}>
            <span className="fi-me-chevron">
              <ChevronIcon />
            </span>
            <span>{theme.name}</span>
          </button>
          {first && (
            <p className="fi-me-gap-quote fi-me-gap-theme-sub">
              <a href={MASTER}>&ldquo;{first.text}&rdquo;</a>
            </p>
          )}
          {theme.answer === "not_done" && (
            <div className="fi-me-gap-theme-sub">
              <p className="fi-me-gap-real">A real gap: you said you haven&rsquo;t done this</p>
            </div>
          )}
        </div>
        <div>
          <span className="fi-badge fi-me-tag">{KIND_WORDS[theme.kind]}</span>
        </div>
        <div className="fi-me-gap-asked">
          {theme.asked} of {theme.of}
        </div>
        <div>
          <span className="fi-badge fi-me-tag" data-grade={grade.grade}>
            {grade.words}
          </span>
        </div>
      </div>
      {open && (
        <div className="fi-me-gap-theme-body" id={bodyId}>
          {theme.why && <p className="fi-me-gap-why">{theme.why}</p>}
          {rest.length > 0 && (
            <ul className="fi-me-gap-lines">
              {theme.lines.map((l) => (
                <li key={l.id}>
                  <a href={MASTER}>{l.text}</a>
                </li>
              ))}
            </ul>
          )}
          <table className="fi-me-gap-postings">
            <thead>
              <tr>
                <th>Employer</th>
                <th>Posting</th>
              </tr>
            </thead>
            <tbody>
              {asked.map((p) => (
                <tr key={p.ref}>
                  <td>{p.employer}</td>
                  <td>
                    {safeHref(p.url) ? (
                      <a href={safeHref(p.url)} target="_blank" rel="noreferrer noopener">
                        {p.title || p.employer}
                      </a>
                    ) : (
                      p.title
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {theme.grade !== "P" && (
            <GapFeedback
              runId={runId}
              theme={theme}
              resume={resume}
              resumeVersion={resumeVersion}
              ready={ready}
              why={why}
              whyId={whyId}
              onAnswered={onAnswered}
              onSavedResume={onSavedResume}
            />
          )}
        </div>
      )}
    </div>
  );
}
