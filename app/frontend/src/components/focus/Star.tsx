import { useState } from "react";
import { StarIcon } from "./icons";

interface Props {
  on: boolean;
  title: string;
  busy?: boolean;
  large?: boolean;
  onToggle: () => void;
}

/** The favorite star: a starred role is fast-tracked and listed in Favorites. It pops when it changes. */
export default function Star({ on, title, busy, large, onToggle }: Props) {
  const [pop, setPop] = useState(0);
  return (
    <button
      type="button"
      className={`fi-star${large ? " is-large" : ""}`}
      aria-pressed={on}
      aria-label={on ? `Unstar ${title}` : `Star ${title}`}
      disabled={busy}
      onClick={(e) => {
        e.stopPropagation();
        setPop((n) => n + 1);
        onToggle();
      }}
    >
      <StarIcon key={pop} className={pop ? "fi-pop" : undefined} />
    </button>
  );
}
