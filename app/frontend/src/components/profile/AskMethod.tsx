import { useEffect, useId, useRef, useState } from "react";
import type { CompExample, CompMethod } from "../../api/types/profile";
import GrowingText from "../primitives/GrowingText";
import AskTiers from "./AskTiers";
import { exampleLine, problemsOf, rangeOf, type RangedKey } from "./askMethodNumbers";
import { InlineNumber } from "./EditParts";

/** What works the example out: the server's `compute` (GET /profile/comp-example), handed in by the screen's store. */
export type Worker = (comp: CompMethod) => Promise<CompExample>;

/** How long the example waits after the last edit before it asks the server again. */
const EXAMPLE_WAIT_MS = 250;

/** The method's numbers, in the order the method runs them, and nothing else: what the example is worked from. */
const numbersOf = (c: CompMethod) =>
  [c.comp_tiers, c.comp_transferable_weight, c.comp_stance_clamp, c.comp_stance_min_siblings, c.comp_likely_low, c.comp_likely_high, c.comp_round_to];

/** The server's own worked example (GET /profile/comp-example) for the numbers as they stand on the page, saved or not.
 * Nothing is worked out here. `for` is the numbers it was worked for: the page shows it as out of date until the server has
 * answered for the numbers now on the page. Numbers outside their range are not sent (`held`): the last example stays, as
 * out of date, until they are back in range. A failed call shows no example. */
function useWorkedExample(comp: CompMethod, held: boolean, work: Worker): { example: CompExample; stale: boolean } | null {
  const [got, setGot] = useState<{ for: string; example: CompExample } | null>(null);
  const now = JSON.stringify(numbersOf(comp));
  const first = useRef(true);
  useEffect(() => {
    if (held) return undefined;
    let current = true;
    // The first one at once, so it is there when the section opens; each edit after it waits for the typing to pause.
    const wait = first.current ? 0 : EXAMPLE_WAIT_MS;
    first.current = false;
    const timer = setTimeout(() => {
      work(comp).then(
        (example) => current && setGot({ for: now, example }),
        () => current && setGot(null),
      );
    }, wait);
    return () => {
      current = false;
      clearTimeout(timer);
    };
    // `now` stands for the numbers `comp` holds.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [now, held]);
  return got && { example: got.example, stale: got.for !== now };
}

/** How we work out what to ask for (docs/PRD.md 26.6): the estimated ask's method as numbered steps, in the order the math
 * runs, each number a box inside its sentence, then what the number means and a worked example that follows the numbers as
 * they are typed, before they are saved. Every number is kept as the setting it has always been (a share as a fraction, the
 * tiers as their text), and a box that is a percent is converted by the decimal point (lib/percent.ts). The example is the
 * server's own `compute` on a made-up range, never a copy of its math.
 *
 * A number is judged against its range when its box is left, never while it is being typed: "85" passes through "8" on the
 * way, and that is not a mistake to be told of. While a box is being typed in and is out of range, the last example stays,
 * as out of date. Save judges every number again, whatever has been left (GatesView.tsx). */
export default function AskMethod({ value, onChange, example }: { value: CompMethod; onChange: (next: CompMethod) => void; example: Worker }) {
  const uid = useId();
  const problems = problemsOf(value);
  const held = Object.keys(problems).length > 0;
  const worked = useWorkedExample(value, held, example);
  /** The boxes left since they were last typed in: the only ones a problem is told of. */
  const [left, setLeft] = useState<ReadonlySet<RangedKey>>(new Set());
  const told = Object.fromEntries(Object.entries(problems).filter(([key]) => left.has(key as RangedKey))) as Partial<Record<RangedKey, string>>;
  const isTold = Object.keys(told).length > 0;
  const mark = (key: RangedKey, on: boolean) =>
    setLeft((was) => {
      if (was.has(key) === on) return was;
      const next = new Set(was);
      if (on) next.add(key);
      else next.delete(key);
      return next;
    });

  const set = (key: keyof CompMethod, next: unknown) => onChange({ ...value, [key]: next });
  /** A number's box, for the range it has (askMethodNumbers.ts), held together with its unit so a line never breaks between them: a
   * percent has "%" after it unless the sentence says its own word (`unit` "" for points), the rounding step a "$" before it.
   * A required number can't be emptied: an emptied box keeps the number it had until it is left. */
  const box = (key: RangedKey, label: string, unit?: { before?: string; after?: string }) => {
    const r = rangeOf(key);
    const { before, after } = unit ?? (r.percent ? { after: "%" } : {});
    return (
      <span className="fi-ss-numbox">
        {before}
        <InlineNumber
          percent={r.percent}
          min={r.min}
          max={r.max}
          value={value[key]}
          label={label}
          allowEmpty={r.blank}
          invalid={key in told}
          onChange={(next) => {
            if (next === null && !r.blank) return;
            mark(key, false);
            set(key, next);
          }}
          onLeave={() => mark(key, true)}
        />
        {after}
      </span>
    );
  };

  return (
    <div className="fi-ss-method">
      <ol className="fi-ss-steps">
        <li>
          <span className="fi-ss-lead">Start from the pay range.</span>
          <span className="fi-ss-detail">The posted range, or when none is posted, the market range for that title, level and place.</span>
        </li>
        <li>
          <span className="fi-ss-lead">Count how much of the job you can show.</span>
          <span className="fi-ss-detail">
            A Proven requirement counts fully; a Transferable one counts as{" "}
            {box("comp_transferable_weight", "A Transferable requirement counts as this share of a Proven one (percent)")} of one.
          </span>
        </li>
        <li>
          <span className="fi-ss-lead">Place your ask in the range by that share.</span>
          <AskTiers value={value.comp_tiers} onChange={(next) => set("comp_tiers", next)} />
        </li>
        <li>
          <span className="fi-ss-lead">Adjust for how the employer pays.</span>
          <span className="fi-ss-detail">
            If its other posted ranges run above or below market (needs at least{" "}
            {box("comp_stance_min_siblings", "Other posted ranges needed to judge how the employer pays")} of them), move the ask up or
            down by at most {box("comp_stance_clamp", "The most the ask moves for how the employer pays (percentage points)", {})} points.
          </span>
        </li>
        <li>
          <span className="fi-ss-lead">Stay near the likely offer.</span>
          <span className="fi-ss-detail">
            New hires usually land between{" "}
            {box("comp_likely_low", "Likely offer when you can show none of the job, share of the middle of the range (percent)")} and{" "}
            {box("comp_likely_high", "Likely offer when you can show all of the job, share of the middle of the range (percent)")} of the
            middle of the range; the more you can show, the nearer the top figure. Your ask never goes above that.
          </span>
        </li>
        <li>
          <span className="fi-ss-lead">
            Round to the nearest{" "}
            {box("comp_round_to", "Round to the nearest (dollars, blank to round in proportion to the pay)", { before: "$" })}.
          </span>
          <span className="fi-ss-detail">Leave it blank to round in proportion to the pay.</span>
        </li>
      </ol>

      <div className="fi-ss-means">
        <div className="fi-ss-means-part">
          <label htmlFor={`${uid}-basis`} className="fi-ss-means-label">
            What the number means:
          </label>
          <select
            id={`${uid}-basis`}
            className="fi-input fi-ss-kind"
            value={value.comp_basis}
            onChange={(e) => set("comp_basis", e.target.value)}
          >
            <option value="base">Base salary</option>
            <option value="total">Total compensation</option>
          </select>
        </div>
        <div className="fi-ss-means-part is-assumes">
          <span className="fi-ss-means-label" aria-hidden="true">
            assuming
          </span>
          <GrowingText
            className="fi-input"
            aria-label="What the number assumes (benefits, bonus)"
            placeholder="benefits and bonus"
            value={value.comp_assumptions}
            onText={(text) => set("comp_assumptions", text)}
          />
        </div>
      </div>

      <p className={`fi-ss-example${isTold ? " is-problem" : ""}`} role="status" data-stale={!isTold && worked?.stale ? "true" : undefined}>
        {isTold ? Object.values(told).join(" ") : worked ? exampleLine(worked.example) : null}
      </p>
    </div>
  );
}
