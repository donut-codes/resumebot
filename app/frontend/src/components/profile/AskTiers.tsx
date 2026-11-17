import { useState } from "react";
import { percentOf } from "../../lib/percent";
import { byLeast, readTiers, type Tier, writeTiers } from "./askMethodNumbers";
import { InlineNumber } from "./EditParts";

/** The coverage tiers of "How we work out what to ask for", step 3, as a small table: for each tier, how much of the job you
 * can show and how far up the range the ask starts, as percents, highest first. The bottom row is everything below the
 * tier above it, so it has no figure of its own and cannot be removed (the method needs a tier that reaches 0). A change
 * goes up as the same text the stored setting is (askMethodNumbers.ts), the rows kept here as the owner edits them, so a row does
 * not jump to its place while its number is being typed: it moves when its box is left. */
export default function AskTiers({ value, onChange }: { value: string; onChange: (next: string) => void }) {
  const [rows, setRows] = useState<Tier[]>(() => readTiers(value));
  // Changed from outside (put back by Undo, or saved elsewhere): read it again, unless these rows already say it.
  const [seen, setSeen] = useState(value);
  if (value !== seen) {
    setSeen(value);
    if (writeTiers(rows) !== value) setRows(readTiers(value));
  }
  const last = rows.length - 1;
  const change = (next: Tier[]) => {
    setRows(next);
    onChange(writeTiers(next));
  };
  const set = (at: number, patch: Partial<Tier>) => change(rows.map((r, i) => (i === at ? { ...r, ...patch } : r)));
  /** A new tier just above the bottom one: half the coverage of the tier over it, and an ask between the two. */
  const add = () => {
    const above = rows[last - 1] ?? { least: 1, ask: 1 };
    const half = Math.round(percentOf(above.least) / 2) / 100;
    const between = Math.round((percentOf(above.ask) + percentOf(rows[last].ask)) / 2) / 100;
    change([...rows.slice(0, last), { least: half, ask: between }, rows[last]]);
  };
  const tidy = () => setRows((r) => [...r.slice(0, -1).sort(byLeast), r[r.length - 1]]);

  return (
    <div className="fi-ss-tiers-wrap">
      <table className="fi-ss-tiers">
        <caption className="fi-vh">Where in the range your ask starts, by how much of the job you can show</caption>
        <thead>
          <tr>
            <th scope="col">You can show</th>
            <th scope="col">Ask this far up the range</th>
            <th scope="col"><span className="fi-vh">Remove a row</span></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              <td>
                {i < last ? (
                  <>
                    at least{" "}
                    <span className="fi-ss-numbox">
                      <InlineNumber percent min={0} max={100} value={row.least} label={`Row ${i + 1}, you can show at least (percent)`}
                        onChange={(least) => least !== null && set(i, { least })} onLeave={tidy} />
                      %
                    </span>
                  </>
                ) : (
                  "anything less"
                )}
              </td>
              <td>
                <span className="fi-ss-numbox">
                  <InlineNumber percent min={0} max={100} value={row.ask} label={`Row ${i + 1}, ask this far up the range (percent)`}
                    onChange={(ask) => ask !== null && set(i, { ask })} />
                  %
                </span>
              </td>
              <td>
                {i < last && (
                  <button type="button" className="fi-link is-danger" aria-label={`Remove row ${i + 1}`} onClick={() => change(rows.filter((_, j) => j !== i))}>
                    Remove
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button type="button" className="fi-link" onClick={add}>
        Add a row
      </button>
    </div>
  );
}
