import type { ReactNode } from "react";

interface Props {
  /** What is loading, in words ("Loading your roles…"). */
  children?: ReactNode;
  as?: "div" | "p";
}

/** The one "still loading" line every screen uses: `.loading`. */
export default function Loading({ children = "Loading…", as: Tag = "div" }: Props) {
  return <Tag className="loading">{children}</Tag>;
}
