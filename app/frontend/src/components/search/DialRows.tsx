// The measurable dials in Search Settings (docs/PRD.md path 46): one row for each, with its choice and its band or
// pick list. A Must is a line of Hard pass and a Prefer a line of Prioritization; the app checks each against a
// stored fact, so no reader judges a dial. A row keeps its place across its own saves (its key is the dial), so a field is not
// replaced while it is typed into; a number is saved when its field is left. A dial has no default: every row starts at Don't care. A row that is not complete
// (no value, or no points for a Prefer) is only what is typed; it is saved the moment it says enough.

import { useId, useState } from "react";
import type { Profile } from "../../api/types/profile";
import { useSearch, useSearchStore } from "../../state/searchStore";
import { applyDial, CHOICE_WORDS, complete, type Choice, type DialState, readDials } from "./dials";

/** A number field that holds what is typed and hands it over when the field is left (or Enter is pressed), so a number of
 * more than one digit is one save, not one per digit, and the field is never replaced while it is typed into. */
function NumberBox({ value, onCommit, ...props }: {
  value: string | number;
  onCommit: (text: string) => void;
  id: string;
  min: number;
  step?: number;
  className: string;
  "aria-label": string;
}) {
  const [text, setText] = useState(String(value));
  const [seen, setSeen] = useState(value);
  if (value !== seen) {
    // The row holds a different value now (saved, undone, or changed elsewhere): show it.
    setSeen(value);
    setText(String(value));
  }
  const commit = () => {
    if (text !== String(value)) onCommit(text);
  };
  return (
    <input
      type="number"
      {...props}
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
      }}
    />
  );
}

function DialRow({ state }: { state: DialState }) {
  const store = useSearchStore();
  const profile = useSearch((s) => s.profile) as Profile;
  const { dial } = state;
  const id = useId();
  const [draft, setDraft] = useState<{ choice: Choice; value: string | number; points: number | ""; except: string } | null>(null);
  const choice = draft?.choice ?? state.choice;
  const value = draft?.value ?? state.value;
  const points = draft?.points ?? (state.points || "");
  const except = draft?.except ?? state.except;

  const change = (next: { choice?: Choice; value?: string | number; points?: number | ""; except?: string }) => {
    const to = { choice: next.choice ?? choice, value: next.value ?? value, points: next.points ?? points, except: next.except ?? except };
    if (to.choice === "dont_care") {
      to.value = "";
      to.except = "";
    }
    else if (dial.kind === "flag") to.value = "yes"; // nothing to set: choosing Must is the setting
    const done = complete(dial, to.choice, to.value, Number(to.points));
    if (!done) {
      setDraft(to);
      return;
    }
    setDraft(null);
    const saved = applyDial(profile, state, to.choice, to.value, Number(to.points), to.except);
    if (saved.gates !== profile.gates) void store.edit("gates", saved.gates);
    if (saved.scoring !== profile.scoring) void store.edit("scoring", saved.scoring);
  };

  // Title words is typed text: it is saved when the owner leaves the box, not on every keystroke.
  const [typed, setTyped] = useState<string | null>(null);
  const leaveWords = () => {
    if (typed === null) return;
    setTyped(null);
    const cleaned = typed.split(",").map((w) => w.trim()).filter(Boolean).join(", ");
    if (cleaned !== String(value)) change({ value: cleaned });
  };

  // Its exceptions are saved the same way: when the owner leaves the box.
  const [typedExcept, setTypedExcept] = useState<string | null>(null);
  const leaveExcept = () => {
    if (typedExcept === null) return;
    setTypedExcept(null);
    const cleaned = typedExcept.split(",").map((w) => w.trim()).filter(Boolean).join(", ");
    if (cleaned !== except) change({ except: cleaned });
  };

  const picked = new Set(String(value).split(",").map((v) => v.trim()).filter(Boolean));
  const togglePick = (v: string, on: boolean) => {
    const next = dial.options?.map((o) => o.value).filter((o) => (o === v ? on : picked.has(o))) ?? [];
    change({ value: next.join(",") });
  };

  return (
    <li className="fi-ss-dial" data-dial={dial.id}>
      <div className="fi-ss-dial-head">
        <span className="fi-ss-dial-name">{dial.label}</span>
        <select
          className="fi-input fi-ss-dial-choice"
          aria-label={`${dial.label}: how much it matters`}
          value={choice}
          onChange={(e) => change({ choice: e.target.value as Choice })}
        >
          <option value="dont_care">{CHOICE_WORDS.dont_care}</option>
          {dial.choices.map((c) => (
            <option key={c} value={c}>
              {CHOICE_WORDS[c]}
            </option>
          ))}
        </select>
      </div>
      {choice !== "dont_care" && dial.kind === "pick" && (
        <fieldset className="fi-ss-dial-picks">
          <legend className="fi-vh">{dial.label}: the values</legend>
          {dial.options?.map((o) => (
            <label key={o.value} className="fi-ss-dial-pick">
              <input type="checkbox" checked={picked.has(o.value)} onChange={(e) => togglePick(o.value, e.target.checked)} />
              {o.label}
            </label>
          ))}
        </fieldset>
      )}
      {choice !== "dont_care" && dial.kind === "words" && (
        <label className="fi-ss-dial-words">
          {dial.caption}
          <input
            type="text"
            className="fi-input"
            aria-label={`${dial.label}: the words and phrases`}
            value={typed ?? String(value)}
            onChange={(e) => setTyped(e.target.value)}
            onBlur={leaveWords}
            onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
          />
        </label>
      )}
      {choice !== "dont_care" && dial.kind === "words" && dial.takesExcept && (
        <label className="fi-ss-dial-words">
          Except in these phrases, which a listed word may sit inside. Separate them with commas.
          <input
            type="text"
            className="fi-input"
            aria-label={`${dial.label}: except in these phrases`}
            value={typedExcept ?? except}
            onChange={(e) => setTypedExcept(e.target.value)}
            onBlur={leaveExcept}
            onKeyDown={(e) => e.key === "Enter" && e.currentTarget.blur()}
          />
        </label>
      )}
      {choice !== "dont_care" && dial.kind === "flag" && (
        <p className="fi-ss-dial-words">A posting that does not name its employer is screened out.</p>
      )}
      {choice !== "dont_care" && dial.kind === "number" && (
        <label className="fi-ss-dial-number" htmlFor={`${id}-n`}>
          {dial.lead ?? (dial.id === "pay" ? "At least" : "Within")}
          <NumberBox
            id={`${id}-n`}
            min={0}
            className="fi-input fi-ss-num"
            aria-label={`${dial.label}: the number of ${dial.unit === "%" ? "percent" : dial.unit}`}
            value={value}
            onCommit={(text) => change({ value: text })}
          />
          {dial.unit}
        </label>
      )}
      {choice === "prefer" && (
        <label className="fi-ss-dial-points" htmlFor={`${id}-p`}>
          Adds
          <NumberBox
            id={`${id}-p`}
            min={1}
            step={1}
            className="fi-input fi-ss-pts"
            aria-label={`${dial.label}: points`}
            value={points}
            onCommit={(text) => change({ points: text === "" ? "" : Math.trunc(Number(text)) })}
          />
          points
        </label>
      )}
    </li>
  );
}

/** The part of Search Settings the rows sit in. A dial is shown only where one of its choices lives: Hard pass holds the
 * Must, Prioritization the Prefer (docs/PRD.md 46.4). A dial with both choices is in both, its control unchanged. */
export type Part = "hard_pass" | "prioritization";
const CHOICE_OF: Record<Part, Exclude<Choice, "dont_care">> = { hard_pass: "must", prioritization: "prefer" };

/** The dials, one row each, as a part of a list of rules. `part` limits the rows to the dials that take its choice; `side`
 * limits them further to the employer's or the job's. */
export default function DialRows({ part, side }: { part: Part; side?: "employer" | "job" }) {
  const profile = useSearch((s) => s.profile) as Profile;
  const states = readDials(profile).filter(
    (s) => s.dial.choices.includes(CHOICE_OF[part]) && (side === undefined || s.dial.side === side),
  );
  return (
    <ul className="fi-ss-sub" aria-label="Measured dials">
      {states.map((s) => (
        <DialRow key={s.dial.id} state={s} />
      ))}
    </ul>
  );
}
