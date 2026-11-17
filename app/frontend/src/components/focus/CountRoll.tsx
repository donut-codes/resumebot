import { useState } from "react";
import { formatCount } from "../../lib/format";

type Roll = { now: number; old: number | null; dir: "up" | "down" | null };

/** A count that rolls when it changes: the old figure slides out, the new one slides in, the way it went. */
export default function CountRoll({ value, className }: { value: number; className?: string }) {
  const [roll, setRoll] = useState<Roll>({ now: value, old: null, dir: null });
  if (roll.now !== value) setRoll({ now: value, old: roll.now, dir: value > roll.now ? "up" : "down" });
  const { old, dir } = roll;
  return (
    <span className={`fi-roll${className ? ` ${className}` : ""}`}>
      {old !== null && dir && (
        <span key={`old-${old}-${value}`} className={`fi-roll-old fi-roll-${dir}`} aria-hidden="true">
          {formatCount(old)}
        </span>
      )}
      <span key={`now-${value}`} className={`fi-roll-now${dir ? ` fi-roll-${dir}` : ""}`}>
        {formatCount(value)}
      </span>
    </span>
  );
}
