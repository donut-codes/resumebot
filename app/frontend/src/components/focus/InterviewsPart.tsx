import { useState } from "react";
import { errorMessage } from "../../api/client";
import type { Interview, InterviewInput, PrepState } from "../../api/types/pipeline";
import { dateTimeInWords } from "../../lib/dates";
import { useLiveChange } from "../../lib/useLiveChange";
import InterviewForm from "./InterviewForm";
import InterviewReport from "./InterviewReport";

const OPEN: PrepState[] = ["waiting", "writing", "review_waiting", "reviewing"];
const REDO: PrepState[] = ["failed", "lapsed"];

interface Props {
  /** The role's status: only a role in Active Discussion takes a new interview. */
  stage: string;
  interviews: Interview[];
  onAdd: (body: InterviewInput) => Promise<void>;
  onEdit: (id: string, body: InterviewInput) => Promise<void>;
  onRemove: (id: string) => Promise<void>;
  onPrepare: (id: string) => Promise<void>;
  /** Reads the role again (while a prep is open). */
  onRefresh: () => void;
}

function Card({ iv, onEdit, onRemove, onPrepare }: { iv: Interview } & Pick<Props, "onEdit" | "onRemove" | "onPrepare">) {
  const [editing, setEditing] = useState(false);
  const [asking, setAsking] = useState(false);
  const [showing, setShowing] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const prep = iv.prep;
  const changed = prep?.changed ?? [];
  const again = iv.can_prepare_again && (changed.length > 0 || (!!prep && REDO.includes(prep.state)));

  async function act(run: () => Promise<void>, fallback: string) {
    setProblem(null);
    try {
      await run();
    } catch (err) {
      setProblem(errorMessage(err, err instanceof Error && err.message ? err.message : fallback));
    }
  }

  if (editing) {
    return (
      <InterviewForm
        interview={iv}
        onCancel={() => setEditing(false)}
        onSave={async (body) => {
          await onEdit(iv.id, body);
          setEditing(false);
        }}
      />
    );
  }

  return (
    <article className="fi-iv-card fi-card" aria-label={`${iv.round_label} interview`}>
      <header className="fi-iv-head">
        <div>
          <h4>{iv.round_label}</h4>
          {iv.label && <p className="fi-iv-label">{iv.label}</p>}
        </div>
        <p className="fi-iv-when">{dateTimeInWords(iv.when_at)}</p>
      </header>
      {iv.people.length > 0 && (
        <ul className="fi-iv-people-list">
          {iv.people.map((p) => (
            <li key={p.id}>{p.title ? `${p.name}, ${p.title}` : p.name}</li>
          ))}
        </ul>
      )}
      {iv.notes && <p className="fi-iv-notes">{iv.notes}</p>}

      {prep ? (
        <p className={prep.state === "failed" ? "fi-iv-status is-bad" : "fi-iv-status"}>
          {iv.versions.length > 1 && <span className="fi-rep-quiet">Version {prep.version}: </span>}
          {prep.step}
        </p>
      ) : (
        iv.past && <p className="fi-iv-status">The time has passed, so no report is made.</p>
      )}
      {prep?.error && prep.state === "failed" && <p className="fi-rep-quiet">{prep.error}</p>}
      {changed.length > 0 && <p className="fi-iv-changed">You changed this interview after the report was made: {changed.join("; ")}.</p>}
      {problem && <p className="fi-alert" role="alert">{problem}</p>}

      {asking ? (
        <div className="fi-iv-confirm" role="group" aria-label="Remove this interview">
          <p>Remove this interview and its report? This can&rsquo;t be undone.</p>
          <button type="button" className="fi-btn" onClick={() => act(() => onRemove(iv.id), "Couldn't remove the interview.")}>Remove it</button>
          <button type="button" className="fi-btn" onClick={() => setAsking(false)}>Keep it</button>
        </div>
      ) : (
        <div className="fi-iv-actions">
          {prep?.report && (
            <button type="button" className="fi-btn fi-btn-go" aria-expanded={showing} onClick={() => setShowing((s) => !s)}>
              {showing ? "Hide the report" : "Open the report"}
            </button>
          )}
          {again && (
            <button type="button" className="fi-btn" onClick={() => act(() => onPrepare(iv.id), "Couldn't start the report.")}>Prepare again</button>
          )}
          <button type="button" className="fi-btn" onClick={() => setEditing(true)}>Edit</button>
          <button type="button" className="fi-btn" onClick={() => setAsking(true)}>Remove</button>
        </div>
      )}
      {showing && prep?.report && <InterviewReport report={prep.report} review={prep.review} />}
    </article>
  );
}

/** The role's interviews (docs/PRD.md 15.2): the form that adds one (a role in Active Discussion only), a card for each,
 * where its prep is, and the report when it is ready. */
export default function InterviewsPart({ stage, interviews, onAdd, onEdit, onRemove, onPrepare, onRefresh }: Props) {
  const [adding, setAdding] = useState(false);
  const canAdd = stage === "active_discussion";
  const working = interviews.some((i) => i.prep && OPEN.includes(i.prep.state));

  // A report being made shows its progress: while one is open, the role is read again when the server says a prep changed.
  useLiveChange("prep", onRefresh, { enabled: working });

  if (!canAdd && interviews.length === 0) return null;

  return (
    <section className="fi-sec" aria-label="Interviews">
      <h3>Interviews</h3>
      {interviews.length === 0 && !adding && <p className="fi-pane-note">None yet.</p>}
      <div className="fi-iv-list">
        {interviews.map((iv) => (
          <Card key={iv.id} iv={iv} onEdit={onEdit} onRemove={onRemove} onPrepare={onPrepare} />
        ))}
      </div>
      {adding ? (
        <InterviewForm
          onCancel={() => setAdding(false)}
          onSave={async (body) => {
            await onAdd(body);
            setAdding(false);
          }}
        />
      ) : (
        canAdd && (
          <button type="button" className="fi-btn" onClick={() => setAdding(true)}>
            Add an interview
          </button>
        )
      )}
    </section>
  );
}
