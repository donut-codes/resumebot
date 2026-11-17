import { useEffect, useId, useRef, useState } from "react";
import { api } from "../../api/client";
import type { ParsedResume, ParseJob } from "../../api/types/profile";
import { useAction } from "../../lib/useAction";
import { useJobPoll } from "../../lib/useJobPoll";
import { useModalDialog } from "../../lib/useModalDialog";
import GrowingText from "../primitives/GrowingText";
import JobProgress, { type JobStep } from "../primitives/JobProgress";
import { MeAlert } from "./MeSection";

/** The steps reading a resume goes through, and how far along the bar each one is. */
const STEPS: JobStep[] = [
  { state: "waiting", label: "Waiting for an agent", percent: 25 },
  { state: "reading", label: "Reading your resume", percent: 65 },
  { state: "done", label: "Done", percent: 100 },
];

interface Props {
  /** A reading that was started for the owner, not from this panel (their Claude, through the owner door): the panel asks
   * after it from the start instead of showing the form. */
  job?: string | null;
  /** The reading is done: what the resume said. The caller fills the master resume and closes the panel. */
  onFilled: (fields: ParsedResume) => void;
  onClose: () => void;
}

/** "Import your resume", opened over the page like "Paste a job posting": choose a resume file (.docx, .pdf or .txt) or
 * paste its text, Read it, and watch it go. Reading takes a while, so the panel always shows where it is (a bar, the
 * server's own words for the step, how long it has been) and never goes quiet; a failure says so, with Try again. A file
 * is read on the server, and one that can't be read says why. */
export default function ImportPanel({ job, onFilled, onClose }: Props) {
  const fileId = useId();
  const textId = useId();
  const headingId = useId();
  const dialog = useModalDialog(onClose);
  const fileInput = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [text, setText] = useState("");
  const progressRef = useRef<HTMLDivElement>(null);
  const poll = useJobPoll<ParseJob<ParsedResume>>(
    (id) => api.parseJob<ParsedResume>(id),
    { failed: "That didn't finish.", lost: "That didn't finish.", touch: "That didn't finish." },
    (done) => {
      if (done.fields) onFilled(done.fields);
      else return "That didn't finish.";
    },
    "jobs",
  );
  const { working, failure } = poll;
  const { begin } = poll;

  // A reading started for the owner is asked after from the first moment.
  useEffect(() => {
    if (job) begin(job);
  }, [job, begin]);

  const start = useAction(
    async () => {
      const { id } = file ? await api.parseResumeFile(file) : await api.parseResume(text.trim());
      poll.begin(id);
    },
    { fallback: "Couldn't start reading it." },
  );

  // Keyboard focus follows the form into the progress, instead of dropping to the page.
  useEffect(() => {
    if (working) progressRef.current?.focus();
  }, [working]);

  // One or the other: the last one the person touched is the one that is read.
  const chooseFile = (chosen: File | null) => {
    setFile(chosen);
    if (chosen) setText("");
  };
  const typeText = (typed: string) => {
    setText(typed);
    if (typed && file) {
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  return (
    <dialog {...dialog} className="fi-dialog" aria-labelledby={headingId}>
      <h2 id={headingId} className="fi-dialog-title">Import your resume</h2>
      {/* The form stays in the panel, hidden, while it works, so the file that was chosen is still chosen if the reading fails. */}
      <div className="fi-dialog-form" hidden={working}>
        <p className="fi-dialog-hint">An agent will sort your resume into the parts of your master resume, in your own words. It is added to what you have: nothing you wrote is removed or reworded, and you see what will be added before anything goes in.</p>
        <label className="fi-dialog-label" htmlFor={fileId}>
          Your resume file (.docx, .pdf or .txt)
        </label>
        <input
          id={fileId}
          ref={fileInput}
          type="file"
          className="fi-dialog-file"
          accept=".docx,.pdf,.txt"
          onChange={(e) => chooseFile(e.target.files?.[0] ?? null)}
        />
        <label className="fi-dialog-label" htmlFor={textId}>
          Or paste its text
        </label>
        <GrowingText id={textId} className="fi-input" minRows={8} value={text} onText={typeText} />
        {failure && <MeAlert>{failure}</MeAlert>}
        {start.error && <MeAlert>{start.error}</MeAlert>}
        <div className="fi-dialog-actions">
          <button type="button" className="fi-btn fi-btn-go" disabled={(!file && !text.trim()) || start.busy} onClick={() => start.run()}>
            {failure ? "Try again" : start.busy ? "Starting…" : "Read it"}
          </button>
          <button type="button" className="fi-btn" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
      {working && <JobProgress ref={progressRef} steps={STEPS} state={poll.status?.state ?? null} step={poll.status?.step} seconds={poll.seconds} onCancel={onClose} />}
    </dialog>
  );
}
