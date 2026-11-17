import { useRef } from "react";
import RoleRow from "./RoleRow";
import { useFlip, useLeaving } from "./motion";
import type { RowView } from "./model";

interface Props {
  rows: RowView[];
  /** Another stage or search: the list draws fresh, with no motion. */
  listKey: string;
  currentId: string | null;
  busy: Record<string, true>;
  mark: string;
  empty: string;
  onOpen: (id: string) => void;
  onStar: (id: string) => void;
}

/** The roles of the stage or the search: rows glide to their new places and a row that leaves folds out. */
export default function RoleList({ rows, listKey, currentId, busy, mark, empty, onOpen, onStar }: Props) {
  const ref = useRef<HTMLUListElement>(null);
  const shown = useLeaving(rows, listKey);
  useFlip(ref, listKey);
  return (
    <ul className="fi-rows" ref={ref} aria-label="Roles">
      {shown.length === 0 && <li className="fi-empty">{empty}</li>}
      {shown.map(({ item, leaving }) => (
        <RoleRow
          key={item.id}
          row={item}
          current={item.id === currentId}
          leaving={leaving}
          busy={!!busy[item.id]}
          mark={mark}
          onOpen={onOpen}
          onStar={onStar}
        />
      ))}
    </ul>
  );
}
