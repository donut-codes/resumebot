// Merging an employer into another and splitting roles out of one, by hand (docs/PRD.md 36.7), in the employer's pane:
// find or choose, see what will move (the server counts it; nothing changes yet), confirm. Everything goes through the
// employers store, which takes the rows the server says changed.

import { useEffect, useState } from "react";
import { errorMessage } from "../../api/client";
import type { Employer, EmployerMergePreview, EmployerSplitPreview } from "../../api/types/employers";
import { FIND_MS, useEmployersStore } from "../../state/employersStore";

const count = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** What a merge will do, in the owner's words (the server counted it: nothing has changed yet). */
export function mergeWords(p: EmployerMergePreview): string {
  const parts = [
    `${p.away.name} goes into ${p.keep.name}: ${count(p.roles, "role")}, ${count(p.postings, "posting")} and ${count(p.sites, "site")} move over.`,
    `It is called ${p.name}${p.names.length ? `, and also ${p.names.join("; ")}` : ""}.`,
  ];
  if (p.folded) parts.push(`${count(p.folded, "posting")} read under both ${p.folded === 1 ? "is" : "are"} counted once.`);
  if (p.hard_pass) {
    parts.push(
      `A hard pass holds on the one employer left${p.to_gated_out ? `: ${count(p.to_gated_out, "role")} not yet applied to will go to Gated out` : ""}.`,
    );
  }
  return parts.join(" ");
}

/** What a split will do, in the owner's words. */
export function splitWords(p: EmployerSplitPreview): string {
  const parts = [
    `${count(p.roles, "role")} and ${count(p.postings, "posting")} move from ${p.from.name} to ${p.to.exists ? "the employer called" : "a new employer called"} ${p.to.name}.`,
  ];
  if (p.names.length) parts.push(`The ${p.names.length === 1 ? "name" : "names"} ${p.names.join("; ")} ${p.names.length === 1 ? "goes" : "go"} with them.`);
  if (p.still_gated_out) {
    const one = p.still_gated_out === 1;
    parts.push(`${p.still_gated_out} of the roles ${one ? "is" : "are"} in Gated out for the hard pass on ${p.from.name} and ${one ? "stays" : "stay"} there until the gates are checked again, which returns ${one ? "it" : "them"} if nothing else stops ${one ? "it" : "them"}.`);
  }
  return parts.join(" ");
}

/** "Merge into another employer": find the one to keep by its name, see what will move, confirm. */
export function MergeForm({ employer, onMerged, onCancel }: { employer: Employer; onMerged: (kept: Employer) => void; onCancel: () => void }) {
  const store = useEmployersStore();
  const [typed, setTyped] = useState("");
  // What was found and what a merge would move, each kept with the question it answers, so a stale answer shows nothing.
  const [result, setResult] = useState<{ q: string; rows: Employer[] } | null>(null);
  const [into, setInto] = useState<string | null>(null);
  const [counted, setCounted] = useState<{ into: string; preview: EmployerMergePreview } | null>(null);
  const found = typed.trim() && result?.q === typed.trim() ? result.rows : null;
  const preview = into && counted?.into === into ? counted.preview : null;
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // The names found for what is typed; a newer search wins over an older answer.
  useEffect(() => {
    let live = true;
    const q = typed.trim();
    if (!q) return;
    // The search waits for the typing to pause, so a word is one ask.
    const wait = setTimeout(() => {
      store.search(q, employer.id).then(
        (rows) => live && setResult({ q, rows }),
        (err: unknown) => live && setError(errorMessage(err, "Couldn't look for the employers.")),
      );
    }, FIND_MS);
    return () => {
      live = false;
      clearTimeout(wait);
    };
  }, [typed, employer.id, store]);

  // What a merge into the chosen one would move.
  useEffect(() => {
    let live = true;
    if (!into) return;
    store.mergePreview(employer.id, into).then(
      (p) => live && setCounted({ into, preview: p }),
      (err: unknown) => live && setError(errorMessage(err, "Couldn't work out what a merge would move.")),
    );
    return () => {
      live = false;
    };
  }, [into, employer.id, store]);

  async function merge() {
    if (!into || !preview) return;
    setBusy(true);
    setError(null);
    try {
      onMerged(await store.merge(employer.id, into));
    } catch (err) {
      setError(errorMessage(err, "Couldn't merge them."));
      setBusy(false);
    }
  }

  return (
    <form
      className="fi-card fi-emp-move"
      aria-label="Merge this employer into another"
      onSubmit={(ev) => {
        ev.preventDefault();
        void merge();
      }}
    >
      <label className="fi-emp-label" htmlFor="merge-into-search">
        Merge {employer.name} into
      </label>
      <input
        id="merge-into-search"
        type="search"
        className="fi-input"
        placeholder="Find the employer to keep"
        autoComplete="off"
        value={typed}
        // What was chosen belongs to what was found: a new search starts the choice over. Enter only searches.
        onChange={(ev) => {
          setTyped(ev.target.value);
          setInto(null);
          setError(null);
        }}
        onKeyDown={(ev) => {
          if (ev.key === "Enter") ev.preventDefault();
        }}
      />
      {found !== null &&
        (found.length === 0 ? (
          <p className="fi-emp-quiet">None match.</p>
        ) : (
          <ul className="fi-emp-choices" role="radiogroup" aria-label="The employer to keep">
            {found.map((e) => (
              <li key={e.id}>
                <label>
                  <input type="radio" name="merge-into" checked={into === e.id} onChange={() => setInto(e.id)} /> {e.name}{" "}
                  <span className="fi-emp-quiet">{count(e.roles_total ?? 0, "role")}</span>
                </label>
              </li>
            ))}
          </ul>
        ))}
      {preview && <p className="fi-emp-words">{mergeWords(preview)}</p>}
      {error && (
        <p className="fi-alert" role="alert">
          {error}
        </p>
      )}
      <div className="fi-actions">
        <button type="submit" className="fi-btn fi-btn-go" disabled={!preview || busy}>
          Merge
        </button>
        <button type="button" className="fi-btn fi-btn-quiet" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

/** "Split roles out": choose roles, name the employer they belong to, see what will move, confirm. */
export function SplitForm({ employer, onSplit, onCancel }: { employer: Employer; onSplit: (to: Employer) => void; onCancel: () => void }) {
  const store = useEmployersStore();
  const roles = employer.roles ?? [];
  const [chosen, setChosen] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [review, setReview] = useState<EmployerSplitPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // A change to the choice is a new question: what was shown no longer fits it.
  const choose = (id: string, on: boolean) => {
    setReview(null);
    setChosen((was) => (on ? [...was, id] : was.filter((x) => x !== id)));
  };

  async function next() {
    setBusy(true);
    setError(null);
    try {
      if (review) onSplit(await store.split(employer.id, chosen, name.trim()));
      else setReview(await store.splitPreview(employer.id, chosen, name.trim()));
    } catch (err) {
      setError(errorMessage(err, review ? "Couldn't split them out." : "Couldn't work out what a split would move."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="fi-card fi-emp-move"
      aria-label="Split roles out of this employer"
      onSubmit={(ev) => {
        ev.preventDefault();
        if (review || (chosen.length && name.trim())) void next();
      }}
    >
      {roles.length === 0 ? (
        <p className="fi-emp-quiet">It has no roles to move.</p>
      ) : (
        <fieldset className="fi-emp-fieldset">
          <legend className="fi-emp-label">Roles to move</legend>
          <ul className="fi-emp-choices">
            {roles.map((r) => (
              <li key={r.id}>
                <label>
                  <input type="checkbox" checked={chosen.includes(r.id)} onChange={(ev) => choose(r.id, ev.target.checked)} /> {r.title}
                </label>
              </li>
            ))}
          </ul>
        </fieldset>
      )}
      <label className="fi-emp-label" htmlFor="split-to-name">
        Move them to the employer called
      </label>
      <input
        id="split-to-name"
        type="text"
        className="fi-input"
        value={name}
        onChange={(ev) => {
          setReview(null);
          setName(ev.target.value);
        }}
        // Enter asks what would move; once that is shown, only the button splits.
        onKeyDown={(ev) => {
          if (ev.key === "Enter" && review) ev.preventDefault();
        }}
      />
      {review && <p className="fi-emp-words">{splitWords(review)}</p>}
      {error && (
        <p className="fi-alert" role="alert">
          {error}
        </p>
      )}
      <div className="fi-actions">
        {review ? (
          <button type="submit" className="fi-btn fi-btn-go" disabled={busy}>
            Split them out
          </button>
        ) : (
          <button type="submit" className="fi-btn" disabled={!chosen.length || !name.trim() || busy}>
            See what moves
          </button>
        )}
        <button type="button" className="fi-btn fi-btn-quiet" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
