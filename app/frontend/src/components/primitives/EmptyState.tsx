import type { ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** The smaller line used inside a panel or drawer (`.empty-state-small`). */
  small?: boolean;
}

/** The one "nothing here yet" line every screen uses: `.empty-state`. */
export default function EmptyState({ children, small = false }: Props) {
  return <p className={small ? "empty-state-small" : "empty-state"}>{children}</p>;
}
