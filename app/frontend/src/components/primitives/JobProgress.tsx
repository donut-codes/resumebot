import { forwardRef } from "react";
import { elapsedInWords } from "../../lib/text";

export interface JobStep {
  state: string;
  label: string;
  /** How far along the bar it is. */
  percent: number;
}

/** Before the first answer: sent, but nothing heard yet. */
const STARTING_PERCENT = 10;

interface Props {
  steps: JobStep[];
  /** Where the job is now (its state), or null before the first answer. */
  state: string | null;
  /** The server's own words for the step, when it has any. */
  step?: string | null;
  seconds: number;
  onCancel: () => void;
}

/** A reading's progress, in a popup: a bar, the server's words for the step, how long it has been, the list of steps with the
 * current one marked, and Cancel. Keyboard focus lands on it when it appears (it takes the ref). */
const JobProgress = forwardRef<HTMLDivElement, Props>(function JobProgress({ steps, state, step, seconds, onCancel }, ref) {
  const stepIndex = state ? Math.max(0, steps.findIndex((s) => s.state === state)) : 0;
  const percent = state ? steps[stepIndex].percent : STARTING_PERCENT;
  const stepText = step || (state ? steps[stepIndex].label : "Sending it");
  return (
    <div className="fi-progress" ref={ref} tabIndex={-1}>
      <div
        className="fi-progress-track"
        role="progressbar"
        aria-label="Progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        aria-valuetext={stepText}
      >
        <div className="fi-progress-fill" style={{ transform: `scaleX(${percent / 100})` }} />
      </div>
      <p className="fi-progress-line">
        <span role="status">{stepText}</span>
        <span className="fi-progress-time">{elapsedInWords(seconds)}</span>
      </p>
      <ol className="fi-progress-steps" aria-label="Steps">
        {steps.map((s, i) => (
          <li key={s.state} aria-current={state && i === stepIndex ? "step" : undefined} className={i === stepIndex ? "is-now" : undefined}>
            {s.label}
          </li>
        ))}
      </ol>
      <div className="fi-dialog-actions">
        <button type="button" className="fi-btn" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
});

export default JobProgress;
