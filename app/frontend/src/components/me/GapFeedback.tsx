import { useId, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { GapDraft, GapTheme, Resume } from "../../api/types/me";
import { useAction } from "../../lib/useAction";
import { SAVED_TOAST_MS } from "../../lib/useDraftPage";
import { useJobPoll } from "../../lib/useJobPoll";
import { useToast } from "../toast/ToastProvider";
import { MeAlert } from "./MeSection";
import { ADDED_WHY, addToResume, jobName } from "./gaps";

/** The fewest characters the owner's words may have before a line is drafted from them (the server's own limit). */
const MIN_WORDS = 10;

type Choice = "done" | "not_done" | "not_relevant";

interface Props {
  runId: string;
  theme: GapTheme;
  /** The master resume as it was loaded, and the version of it, so the save names the version it was made from. */
  resume: Resume;
  resumeVersion: number;
  /** Whether an agent can take the work now, and why not in words (peak hours...). */
  ready: boolean;
  why: string | null;
  whyId: string;
  /** An answer was given or taken back: the run is read again. */
  onAnswered: () => void;
  /** The line was added: the master resume as saved, and its new version. */
  onSavedResume: (resume: Resume, version: number) => void;
}

/** What the owner says of a theme graded Transferable or Unproven (docs/PRD.md 38.5): "I've done this" (their own words, a
 * line the agent drafts from them in a box they can rewrite, the job it belongs to, Evidence or Bullet, and Add to resume,
 * the resume's own save with the version it was loaded at), "I haven't" (a real gap) or "Not relevant" (hidden for the role).
 * Only Add to resume writes the resume; the other answers are Find Gaps' own. */
export default function GapFeedback({ runId, theme, resume, resumeVersion, ready, why, whyId, onAnswered, onSavedResume }: Props) {
  const uid = useId();
  const { showToast } = useToast();
  const [choice, setChoice] = useState<Choice>(theme.answer === "not_done" ? "not_done" : "done");
  const [words, setWords] = useState("");
  const [draft, setDraft] = useState<string | null>(null);
  const [jobId, setJobId] = useState("");
  const [as, setAs] = useState<"evidence" | "bullet">("evidence");
  const [added, setAdded] = useState(false);

  const jobs = resume.roles;
  const job = jobs.some((r) => r.id === jobId) ? jobId : (jobs[0]?.id ?? "");

  const poll = useJobPoll<GapDraft>(
    (id) => api.gapDraft(id),
    { failed: "That didn't finish.", lost: "That didn't finish.", touch: "That didn't finish." },
    (done) => {
      if (done.draft) setDraft(done.draft);
      else return "That didn't finish.";
    },
    "gaps",
  );
  const drafting = poll.working && draft === null;

  const answer = useAction(
    async (next: Choice) => {
      if (next === "done") {
        if (theme.answer_id) await api.takeBackGapAnswer(theme.answer_id);
      } else if (next === "not_done") {
        if (theme.answer !== "not_done") await api.answerGapTheme(runId, theme.id, "not_done");
      } else {
        await api.answerGapTheme(runId, theme.id, "not_relevant");
      }
      setChoice(next);
      onAnswered();
    },
    { fallback: "Couldn't save that answer." },
  );

  const ask = useAction(
    async () => {
      setAdded(false);
      const { id } = await api.draftGapLine(runId, theme.id, words.trim());
      poll.begin(id);
    },
    { fallback: "Couldn't start drafting." },
  );

  const add = useAction(
    async () => {
      const next = addToResume(resume, job, as, theme.name, draft ?? "");
      if (!next) throw new ApiError("Choose the job it belongs to.", 0);
      const saved = await api.saveProfile("resume", next, ADDED_WHY, resumeVersion);
      onSavedResume(next, saved.version ?? resumeVersion + 1);
      showToast("Saved", SAVED_TOAST_MS);
      setWords("");
      setDraft(null);
      poll.reset();
      setAdded(true);
    },
    { fallback: "Couldn't add it to your resume." },
  );

  const tooShort = words.trim().length < MIN_WORDS;

  return (
    <div className="fi-me-gap-feedback">
      <fieldset>
        <legend>Have you done this?</legend>
        <div className="fi-me-gap-choices">
          {(
            [
              ["done", "I've done this"],
              ["not_done", "I haven't"],
              ["not_relevant", "Not relevant"],
            ] as const
          ).map(([key, label]) => (
            <label key={key} className="fi-me-gap-choice">
              <input type="radio" name={`${uid}-choice`} checked={choice === key} disabled={answer.busy} onChange={() => void answer.run(key)} /> {label}
            </label>
          ))}
        </div>
        {answer.error && <MeAlert>{answer.error}</MeAlert>}
      </fieldset>

      {choice === "done" && (
        <>
          <div className="fi-me-gap-box">
            <label className="fi-me-gap-label" htmlFor={`${uid}-words`}>
              What you did, in your own words
            </label>
            <textarea id={`${uid}-words`} className="fi-input" value={words} onChange={(e) => setWords(e.target.value)} />
          </div>
          <div className="fi-me-gap-ask">
            <button
              type="button"
              className="fi-btn"
              disabled={tooShort || !ready || ask.busy || drafting}
              aria-describedby={!ready && why ? whyId : undefined}
              title={!ready ? (why ?? undefined) : undefined}
              onClick={() => void ask.run()}
            >
              {draft !== null ? "Draft it again" : "Draft the line"}
            </button>
            {drafting && (
              <span className="fi-me-quiet" role="status">
                {poll.status?.step ?? "Sending it"}
              </span>
            )}
          </div>
          {ask.error && <MeAlert>{ask.error}</MeAlert>}
          {poll.failure && <MeAlert>{poll.failure}</MeAlert>}
          {added && (
            <p className="fi-me-added" role="status">
              Added to your resume
            </p>
          )}

          {draft !== null && (
            <>
              <div className="fi-me-gap-box">
                <label className="fi-me-gap-label" htmlFor={`${uid}-draft`}>
                  The draft line, to edit as you like
                </label>
                <textarea id={`${uid}-draft`} className="fi-input" value={draft} onChange={(e) => setDraft(e.target.value)} />
              </div>
              <div className="fi-me-gap-add">
                <div className="fi-me-gap-box">
                  <label className="fi-me-gap-label" htmlFor={`${uid}-job`}>
                    Which job it belongs to
                  </label>
                  <select id={`${uid}-job`} className="fi-input" value={job} onChange={(e) => setJobId(e.target.value)}>
                    {jobs.length === 0 && <option value="">No job in your master resume yet</option>}
                    {jobs.map((r) => (
                      <option key={r.id} value={r.id}>
                        {jobName(r)}
                      </option>
                    ))}
                  </select>
                </div>
                <fieldset className="fi-me-gap-type">
                  <legend>Add it as</legend>
                  <div className="fi-me-gap-choices">
                    <label className="fi-me-gap-choice">
                      <input type="radio" name={`${uid}-as`} checked={as === "evidence"} onChange={() => setAs("evidence")} /> Evidence <small>never printed</small>
                    </label>
                    <label className="fi-me-gap-choice">
                      <input type="radio" name={`${uid}-as`} checked={as === "bullet"} onChange={() => setAs("bullet")} /> Bullet <small>printed on resumes</small>
                    </label>
                  </div>
                </fieldset>
                <button type="button" className="fi-btn fi-btn-go" disabled={!draft.trim() || !job || add.busy} onClick={() => void add.run()}>
                  {add.busy ? "Adding…" : "Add to resume"}
                </button>
              </div>
              {add.error && <MeAlert>{add.error}</MeAlert>}
            </>
          )}
        </>
      )}
    </div>
  );
}
