import { useEffect, useRef, useState } from "react";
import type { ParsedResume } from "../../api/types/profile";
import ImportPanel from "./ImportPanel";

interface Props {
  /** Whether an agent can take the work now. */
  ready: boolean;
  /** Why not, in words, while it can't (no agent has checked in, Claude's peak hours); the page shows it under the buttons. */
  why: string | null;
  /** The id of the line that says why, which the button is described by. */
  whyId: string;
  /** A resume was read: what it said, to put into the master resume's parts as unsaved changes. */
  onImported: (fields: ParsedResume) => void;
  /** A reading started for the owner, not from this page (their Claude, through the owner door): the dialog opens on it and
   * asks after it, as it does for a reading it started itself. */
  job?: string | null;
  /** The dialog opened for `job` has closed, read or not: the address no longer needs to name it. */
  onJobClosed?: () => void;
}

/** "Import your resume", on the line under Me's tabs on the master resume: a button that opens the dialog to choose or
 * paste a resume. It is off while no agent can take the work, with the reason beside it (the page shows the line). A resume
 * that was read is handed to the page with `onImported`. A reading that was started for the owner (`job`, from the address) opens
 * the dialog on it, the same way. */
export default function ImportResume({ ready, why, whyId, onImported, job, onJobClosed }: Props) {
  const [chosen, setChosen] = useState(false);
  // The reading named in the address that was closed (read or not): the dialog stays shut for it until the address changes.
  const [closedFor, setClosedFor] = useState<string | null>(null);
  if (!job && closedFor) setClosedFor(null);
  const open = chosen || (Boolean(job) && job !== closedFor);
  const opener = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);

  // When the dialog closes, keyboard focus goes back to the button that opened it.
  useEffect(() => {
    if (wasOpen.current && !open) opener.current?.focus();
    wasOpen.current = open;
  }, [open]);

  const close = () => {
    setChosen(false);
    if (job) {
      setClosedFor(job);
      onJobClosed?.();
    }
  };

  return (
    <>
      <button
        ref={opener}
        type="button"
        className="fi-btn"
        aria-expanded={open}
        aria-describedby={why ? whyId : undefined}
        disabled={!ready || open}
        title={why ?? undefined}
        onClick={() => setChosen(true)}
      >
        Import your resume
      </button>
      {open && (
        <ImportPanel
          job={job}
          onFilled={(fields) => {
            close();
            onImported(fields);
          }}
          onClose={close}
        />
      )}
    </>
  );
}
