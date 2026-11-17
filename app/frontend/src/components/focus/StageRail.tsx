import { RAIL } from "./model";
import { Rail, RailEntry, RailGroup } from "./parts";

interface Props {
  current: string;
  counts: Record<string, number>;
  /** While a search is on: how many of its matches are in each entry (an entry with none dims). */
  hits: Record<string, number> | null;
  labels: Record<string, string>;
  onPick: (key: string) => void;
}

/** The stages, with Favorites first: each with its live count. */
export default function StageRail({ current, counts, hits, labels, onPick }: Props) {
  return (
    <Rail label="Stages">
      {RAIL.map((group, gi) => (
        <RailGroup key={gi} label={group.label ?? undefined}>
          {group.entries.map((e) => {
            const hit = hits ? (hits[e.key] ?? 0) : null;
            return (
              <RailEntry
                key={e.key}
                label={labels[e.key] ?? e.label}
                note={hit ? <span className="fi-stage-hit"> · {hit}</span> : null}
                count={counts[e.key] ?? 0}
                current={!hits && current === e.key}
                onPick={() => onPick(e.key)}
                yours={e.yours}
                small={e.small}
                nested={e.nested}
                dim={hit === 0}
              />
            );
          })}
        </RailGroup>
      ))}
    </Rail>
  );
}
