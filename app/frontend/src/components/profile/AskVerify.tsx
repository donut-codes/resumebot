import type { Ask } from "../../api/types/profile";
import GrowingText from "../primitives/GrowingText";

/** Whether the Verify box shows: only for the built-in ask or one that already holds Verify text (the owner can still clear it),
 * since a new ask the reader judges can no longer be saved. Whether Claude is the one who answers this ask from a posting: the app decides an ask from an employer fact, a posting's age,
 * a pay tier or the requirement classes, and nobody reads a posting for those, so a Verify line would be written for no one. */
export const readsThePosting = (ask: Ask): boolean =>
  (!!ask.derived || !!ask.verify?.trim()) &&
  ask.checked_by !== "app" && !ask.fact && !ask.from_classes && (ask.days === null || ask.days === undefined) && (ask.amount === null || ask.amount === undefined);

/** The Verify line of an ask or a statement, under its sentence: the owner's own words for exactly how to check it against a
 * posting, so the same posting gets the same answer every time (docs/PRD.md 30.9). It is worded as the Verify line at the end of
 * a rule on Hard pass is ("Verify: the posting's stated travel, at the top of any range, is 25% or less."): one box that grows
 * with its text, never cut off, with no limit on its length. Claude is sent it and applies it exactly; left empty, the ask is
 * read by its sentence alone. A draft until the page's one Save. */
export default function AskVerify({ ask, name, onChange }: { ask: Ask; name: string; onChange: (next: Ask) => void }) {
  if (!readsThePosting(ask)) return null;
  return (
    <div className="fi-ss-verify">
      <span className="fi-ss-verify-label" aria-hidden="true">
        Verify:
      </span>
      <GrowingText
        className="fi-input"
        aria-label={`Verify: ${name}`}
        placeholder="For example: the posting says the role manages a team of 5 or more people."
        oneLine
        value={ask.verify ?? ""}
        onText={(verify) => onChange({ ...ask, verify })}
      />
    </div>
  );
}
