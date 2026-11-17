import type { Gate } from "../../api/types/profile";
import GrowingText from "../primitives/GrowingText";
import { FactFields } from "./EditParts";
import { CLASS_NAMES, factValues } from "./profileSpec";

/** A line in a section. A requirement (a sentence, usually ending in a Verify line) is the reader's, who answers it pass, fail
 * or unknown; an employer fact and a posting's age are checked by the app, and so is a requirement saved with a class and a
 * share (`from_classes`, `share`), which the app decides from the analyst's classes of the required items: the page keeps
 * those two as they came and says what they mean in plain words under the sentence, as it does for a posting's age. A line
 * has no switch of its own: its section has one. */
export function GateLine({
  gate, says, focus, onChange, onRemove,
}: {
  gate: Gate;
  says: string;
  /** Takes keyboard focus as it appears (the sentence just added). */
  focus: boolean;
  /** The id of its Section box, so focus can go back to it after the line moves to another section. */
  onChange: (next: Gate) => void;
  onRemove: () => void;
}) {
  const label = gate.kind === "rule" ? gate.text || "a new sentence" : says;
  return (
    <li>
      <div className="fi-ss-rule">
        {gate.kind === "rule" && (
          <GrowingText
            className="fi-input"
            aria-label="A requirement"
            placeholder="A requirement a posting must meet"
            autoFocus={focus}
            oneLine
            value={gate.text}
            onText={(text) => onChange({ ...gate, text })}
          />
        )}
        {gate.kind === "rule" && gate.from_classes && gate.share ? (
          <p className="fi-ss-sentence">
            {gate.from_classes === "PT"
              ? `Fails when fewer than ${gate.share}% of the required items in the posting are ${CLASS_NAMES.PT}.`
              : `Fails when ${gate.share}% or more of the required items in the posting are ${CLASS_NAMES[gate.from_classes] ?? gate.from_classes}.`}
          </p>
        ) : null}
        {gate.kind === "employer_fact" && (
          <FactFields fact={gate.fact} op={gate.op} value={gate.value} onChange={(change) => onChange({ ...gate, ...change })} />
        )}
        {gate.kind === "posting_age" && <p className="fi-ss-sentence">{says}</p>}
        {gate.kind === "employer_fact" && (
          <p className="fi-ss-note">Checked automatically from what the app knows about the employer.</p>
        )}
        {gate.kind === "employer_fact" && factValues(gate.fact) && <p className="fi-ss-note">The value is one of: {factValues(gate.fact)}.</p>}
        <div className="fi-ss-foot">
          <button type="button" className="fi-link is-danger" onClick={onRemove} aria-label={`Remove ${label}`}>
            Remove
          </button>
        </div>
      </div>
    </li>
  );
}
