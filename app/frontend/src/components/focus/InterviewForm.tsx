import { useId, useState } from "react";
import { errorMessage } from "../../api/client";
import type { Interview, InterviewInput, RoundKind } from "../../api/types/pipeline";
import { fromOwnerLocalValue, ownerZoneLabel, toOwnerLocalValue } from "../../lib/dates";

export const ROUNDS: { kind: RoundKind; label: string }[] = [
  { kind: "recruiter_screen", label: "Recruiter screen" },
  { kind: "hiring_manager", label: "Hiring manager" },
  { kind: "panel", label: "Panel" },
  { kind: "executive", label: "Executive" },
  { kind: "other", label: "Other" },
];


interface Props {
  /** The interview being changed; none for a new one. */
  interview?: Interview;
  onSave: (body: InterviewInput) => Promise<void>;
  onCancel: () => void;
}

/** The form that adds an interview or changes one: when, the round and what the employer calls it, who is in it, notes. */
export default function InterviewForm({ interview, onSave, onCancel }: Props) {
  const id = useId();
  const [when, setWhen] = useState(interview ? toOwnerLocalValue(interview.when_at) : "");
  const [round, setRound] = useState<RoundKind>(interview?.round_kind ?? "hiring_manager");
  const [label, setLabel] = useState(interview?.label ?? "");
  const [people, setPeople] = useState<{ name: string; title: string }[]>(interview?.people.map((p) => ({ name: p.name, title: p.title })) ?? []);
  const [notes, setNotes] = useState(interview?.notes ?? "");
  const [problem, setProblem] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const setPerson = (i: number, change: Partial<{ name: string; title: string }>) =>
    setPeople((list) => list.map((p, j) => (j === i ? { ...p, ...change } : p)));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const at = when ? fromOwnerLocalValue(when) : null;
    if (!at) {
      setProblem("Pick the date and time of the interview.");
      return;
    }
    setProblem(null);
    setSaving(true);
    try {
      await onSave({
        when: at,
        round_kind: round,
        label: label.trim(),
        people: people.filter((p) => p.name.trim()).map((p) => ({ name: p.name.trim(), title: p.title.trim() })),
        notes: notes.trim(),
      });
    } catch (err) {
      setProblem(errorMessage(err, err instanceof Error && err.message ? err.message : "Couldn't save the interview."));
      setSaving(false);
    }
  }

  return (
    <form className="fi-iv-form fi-card" onSubmit={submit} aria-label={interview ? "Change the interview" : "Add an interview"}>
      <div className="fi-iv-fields">
        <div className="fi-iv-field">
          <label htmlFor={`${id}-when`}>When ({ownerZoneLabel()})</label>
          <input id={`${id}-when`} type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
        </div>
        <div className="fi-iv-field">
          <label htmlFor={`${id}-round`}>Round</label>
          <select id={`${id}-round`} value={round} onChange={(e) => setRound(e.target.value as RoundKind)}>
            {ROUNDS.map((r) => (
              <option key={r.kind} value={r.kind}>{r.label}</option>
            ))}
          </select>
        </div>
        <div className="fi-iv-field">
          <label htmlFor={`${id}-label`}>What the employer calls it</label>
          <input id={`${id}-label`} type="text" value={label} onChange={(e) => setLabel(e.target.value)} />
        </div>
      </div>

      <fieldset className="fi-iv-people">
        <legend>Who will be there</legend>
        {people.map((p, i) => (
          <div className="fi-iv-person" key={i}>
            <div className="fi-iv-field">
              <label htmlFor={`${id}-n${i}`}>Name</label>
              <input id={`${id}-n${i}`} type="text" value={p.name} onChange={(e) => setPerson(i, { name: e.target.value })} />
            </div>
            <div className="fi-iv-field">
              <label htmlFor={`${id}-t${i}`}>Title</label>
              <input id={`${id}-t${i}`} type="text" value={p.title} onChange={(e) => setPerson(i, { title: e.target.value })} />
            </div>
            <button type="button" className="fi-btn" onClick={() => setPeople((list) => list.filter((_, j) => j !== i))}>
              Remove person
            </button>
          </div>
        ))}
        <button type="button" className="fi-btn" onClick={() => setPeople((list) => [...list, { name: "", title: "" }])}>
          Add a person
        </button>
      </fieldset>

      <div className="fi-iv-field">
        <label htmlFor={`${id}-notes`}>Notes</label>
        <textarea id={`${id}-notes`} rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} />
      </div>

      {problem && <p className="fi-alert" role="alert">{problem}</p>}
      <div className="fi-iv-actions">
        <button type="submit" className="fi-btn fi-btn-go" disabled={saving}>
          {interview ? "Save changes" : "Add interview"}
        </button>
        <button type="button" className="fi-btn" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
      </div>
    </form>
  );
}
