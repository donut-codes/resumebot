import { useEffect, useId, useRef, useState } from "react";
import type { ParsedRole, ParseJob } from "../../api/types/profile";
import { useAction } from "../../lib/useAction";
import { useJobPoll } from "../../lib/useJobPoll";
import { useModalDialog } from "../../lib/useModalDialog";
import ErrorBanner from "../primitives/ErrorBanner";
import GrowingText from "../primitives/GrowingText";
import JobProgress, { type JobStep } from "../primitives/JobProgress";

/** The steps a reading goes through, and how far along the bar each one is. */
const STEPS: JobStep[] = [
  { state: "waiting", label: "Waiting for an agent", percent: 25 },
  { state: "reading", label: "Reading the posting", percent: 65 },
  { state: "done", label: "Done", percent: 100 },
];

interface Props {
  /** The role being filled, when the server knows it yet. */
  roleId?: string;
  /** The reading is done: what the posting said. The caller fills the role and closes the panel. */
  onFilled: (fields: Partial<ParsedRole>) => void;
  onClose: () => void;
  /** Starts reading the posting (POST /profile/parse), and reads how it is going: the screen's store hands them in. */
  start: (text: string, roleId?: string) => Promise<{ id: string }>;
  load: (id: string) => Promise<ParseJob<Partial<ParsedRole>>>;
}

/** "Paste a job posting", opened over the page: paste a job posting, Read it, and watch it go. Reading takes a while, so the
 * panel always shows where it is (a bar, the server's own words for the step, how long it has been) and never goes
 * quiet; a failure says so, with Try again. */
export default function ExamplePanel({ roleId, onFilled, onClose, start: begin, load }: Props) {
  const textId = useId();
  const headingId = useId();
  const dialog = useModalDialog(onClose);
  const [text, setText] = useState("");
  const progressRef = useRef<HTMLDivElement>(null);
  const poll = useJobPoll<ParseJob<Partial<ParsedRole>>>(
    load,
    { failed: "It couldn't be read.", lost: "This reading was lost on the way.", touch: "Lost touch with the reading." },
    (done) => void onFilled(done.fields ?? {}),
    "jobs",
  );
  const { working, failure } = poll;

  const start = useAction(
    async () => {
      const { id } = await begin(text.trim(), roleId);
      poll.begin(id);
    },
    { fallback: "Couldn't start reading it." },
  );

  // Keyboard focus follows the form into the progress, instead of dropping to the page.
  useEffect(() => {
    if (working) progressRef.current?.focus();
  }, [working]);

  return (
    <dialog {...dialog} className="fi-dialog" aria-labelledby={headingId}>
      <h2 id={headingId} className="fi-dialog-title">Paste a job posting</h2>
      {working ? (
        <JobProgress ref={progressRef} steps={STEPS} state={poll.status?.state ?? null} step={poll.status?.step} seconds={poll.seconds} onCancel={onClose} />
      ) : (
        <>
          <label className="fi-dialog-label" htmlFor={textId}>
            The job posting
          </label>
          <GrowingText id={textId} className="fi-input" minRows={12} value={text} onText={setText} />
          {failure && <ErrorBanner as="p">{failure}</ErrorBanner>}
          {start.error && <ErrorBanner as="p">{start.error}</ErrorBanner>}
          <div className="fi-dialog-actions">
            <button type="button" className="fi-btn fi-btn-go" disabled={!text.trim() || start.busy} onClick={() => start.run()}>
              {failure ? "Try again" : start.busy ? "Starting…" : "Read it"}
            </button>
            <button type="button" className="fi-btn" onClick={onClose}>
              Cancel
            </button>
          </div>
        </>
      )}
    </dialog>
  );
}
