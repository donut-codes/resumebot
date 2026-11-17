import type { GateWay } from "../../api/types/profile";
import SemicolonInput from "../primitives/SemicolonInput";
import { EditField, Tick } from "./EditParts";

/** The ways of working, in the order the page lists them, with what each list of locations is called. */
export const WAYS = [
  { key: "remote", name: "Remote", field: "Where remote work must be open" },
  { key: "hybrid", name: "Hybrid", field: "Towns or areas, for hybrid" },
  { key: "in_person", name: "In person", field: "Towns or areas, for in person" },
] as const;

/** One way of working: a tick, and (while it is ticked) its one list of locations, names separated by semicolons. Empty
 * means anywhere. */
export function WayRow({ name, field, way, onChange }: { name: string; field: string; way: GateWay; onChange: (next: GateWay) => void }) {
  return (
    <div className="fi-ss-way">
      <Tick label={name} checked={way.on} onChange={(on) => onChange({ ...way, on })} text={name} />
      {way.on && (
        <EditField label={field}>
          {(id) => <SemicolonInput id={id} className="fi-input" items={way.locations} onChange={(locations) => onChange({ ...way, locations })} />}
        </EditField>
      )}
    </div>
  );
}
