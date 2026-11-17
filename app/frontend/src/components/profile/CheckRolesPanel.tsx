import { useId, useState } from "react";
import type { AgentReady, CheckBucket, CheckJob, CheckListResult, CheckPostingResult, CheckScope } from "../../api/types/profile";
import { useAction } from "../../lib/useAction";
import { useJobPoll } from "../../lib/useJobPoll";
import { useModalDialog } from "../../lib/useModalDialog";
import ErrorBanner from "../primitives/ErrorBanner";
import GrowingText from "../primitives/GrowingText";
import { elapsedInWords } from "../../lib/text";

/** What can be checked, in the words the panel shows. */
const SCOPES: { scope: CheckScope; label: string }[] = [
  { scope: "pursued", label: "The roles I pursued" },
  { scope: "gated", label: "Recent postings stopped for not matching your roles" },
  { scope: "posting", label: "One job posting" },
];

interface Props {
  /** Whether an agent is free to take the check, or why not; null until known. */
  ready: AgentReady | null;
  onClose: () => void;
  /** Starts a check (POST /profile/check), and reads how it is going: the screen's store hands them in. */
  begin: (scope: CheckScope, text?: string) => Promise<{ id: string }>;
  load: (id: string) => Promise<CheckJob>;
}

/** "Check my roles", opened over the page (docs/PRD.md 31.2): do the role tabs catch what the owner wants? Choose what
 * to check (the roles they pursued, a sample of stopped postings, or one pasted posting), Check, and watch it go. It is
 * a dry run: nothing is changed. The answer is lists: for the roles and the stopped postings, what the
 * tabs would pass, what they would stop and what fits none of them; for a posting, the tab it matches or none. */
export default function CheckRolesPanel({ ready, onClose, begin, load }: Props) {
  const textId = useId();
  const headingId = useId();
  const dialog = useModalDialog(onClose);
  const [scope, setScope] = useState<CheckScope>("pursued");
  const [text, setText] = useState("");
  const poll = useJobPoll<CheckJob>(
    load,
    { failed: "It couldn't be checked.", lost: "This check was lost on the way.", touch: "Lost touch with the check." },
    undefined,
    "jobs",
  );
  const { status, failure } = poll;

  const start = useAction(
    async () => {
      const { id } = await begin(scope, scope === "posting" ? text.trim() : undefined);
      poll.begin(id);
    },
    { fallback: "Couldn't start the check." },
  );

  const done = status?.state === "done";
  const working = poll.working && !done;

  const again = () => {
    poll.reset();
    start.setError(null);
  };

  const notReady = ready !== null && !ready.ready;
  const canStart = !start.busy && !notReady && (scope !== "posting" || text.trim() !== "");

  return (
    <dialog {...dialog} className="fi-dialog" aria-labelledby={headingId}>
      <h2 id={headingId} className="fi-dialog-title">Check my roles</h2>
      {done && status?.result ? (
        <>
          <CheckResult result={status.result} />
          <div className="fi-dialog-actions">
            <button type="button" className="fi-btn" onClick={again}>
              Check again
            </button>
            <button type="button" className="fi-btn" onClick={onClose}>
              Close
            </button>
          </div>
        </>
      ) : working ? (
        <div className="fi-progress" tabIndex={-1}>
          <p className="fi-progress-line">
            <span role="status">{status?.step || "Sending it"}</span>
            <span className="fi-progress-time">{elapsedInWords(poll.seconds)}</span>
          </p>
          <div className="fi-dialog-actions">
            <button type="button" className="fi-btn" onClick={onClose}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <>
          <p className="fi-dialog-hint">
            A dry run: it shows whether your target roles would let through the jobs you want. Nothing is changed.
          </p>
          <fieldset className="fi-dialog-choices">
            <legend>What to check</legend>
            {SCOPES.map((s) => (
              <label key={s.scope} className="fi-dialog-choice">
                <input type="radio" name="check-scope" checked={scope === s.scope} onChange={() => setScope(s.scope)} />
                <span>{s.label}</span>
              </label>
            ))}
          </fieldset>
          {scope === "posting" && (
            <>
              <label className="fi-dialog-label" htmlFor={textId}>
                The job posting
              </label>
              <GrowingText id={textId} className="fi-input" minRows={10} value={text} onText={setText} />
            </>
          )}
          {notReady && <p className="fi-dialog-hint">{ready?.why}</p>}
          {failure && <ErrorBanner as="p">{failure}</ErrorBanner>}
          {start.error && <ErrorBanner as="p">{start.error}</ErrorBanner>}
          <div className="fi-dialog-actions">
            <button type="button" className="fi-btn fi-btn-go" disabled={!canStart} onClick={() => start.run()}>
              {failure ? "Try again" : start.busy ? "Starting…" : "Check"}
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

/** What a check found, as lists. */
function CheckResult({ result }: { result: CheckListResult | CheckPostingResult }) {
  if (result.scope === "posting") {
    const sentence = result.tab
      ? `It matches ${result.tab.name}: ${result.why}`
      : result.outcome === "stopped"
        ? `Your roles would stop it: ${result.why}`
        : `It matches none of your roles: ${result.why}`;
    return <p className="fi-dialog-answer" role="status">{sentence}</p>;
  }
  const noun = result.scope === "pursued" ? "roles" : "postings";
  return (
    <div className="fi-dialog-result">
      <p className="fi-dialog-answer" role="status">
        Checked {result.checked}
        {result.of > result.checked ? ` of ${result.of}` : ""} {noun}: {result.passed.count} would pass, {result.stopped.count} would be
        stopped, {result.fits_none.count} fit none of your roles.
      </p>
      <ul>
        {result.tabs.map((t) => (
          <li key={t.id}>
            {t.name}: {t.passed} would pass, {t.stopped} would be stopped
          </li>
        ))}
      </ul>
      <Bucket title="Would be stopped" bucket={result.stopped} open={result.scope === "pursued"} />
      <Bucket title="Fit none of your roles" bucket={result.fits_none} open={result.scope === "pursued"} />
      <Bucket title="Would pass" bucket={result.passed} open={result.scope === "gated"} />
    </div>
  );
}

/** One list of a result: a title with its count, and each item with the company, the tab and the line why. */
function Bucket({ title, bucket, open }: { title: string; bucket: CheckBucket; open: boolean }) {
  if (bucket.count === 0) return null;
  return (
    <details className="fi-dialog-bucket" open={open}>
      <summary>
        {title} ({bucket.count})
      </summary>
      <ul>
        {bucket.items.map((row, i) => (
          <li key={row.id ?? i}>
            <strong>{row.title}</strong>
            {row.company ? `, ${row.company}` : ""}
            {row.tab_name ? ` (${row.tab_name})` : ""}: {row.why}
          </li>
        ))}
      </ul>
      {bucket.count > bucket.items.length && <p className="fi-dialog-hint">Showing the first {bucket.items.length}.</p>}
    </details>
  );
}
