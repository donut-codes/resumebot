import { useCallback, useState, type ReactNode } from "react";
import { ChevronIcon } from "../focus/icons";

/** Which parts are open. A part that was never opened or folded by hand is open or folded as `defaults` say (decided
 * once, when the page opens, so a part never folds under the cursor as its text grows); a part left out of `defaults` is
 * open, or folded when `otherwise` is false (a part that may come in later, like a target role's). */
export function useFolds(defaults: () => Record<string, boolean>, otherwise = true) {
  const [start] = useState(defaults);
  const [chosen, setChosen] = useState<Record<string, boolean>>({});
  const isOpen = useCallback((id: string) => chosen[id] ?? start[id] ?? otherwise, [chosen, start, otherwise]);
  const toggle = useCallback((id: string) => setChosen((c) => ({ ...c, [id]: !(c[id] ?? start[id] ?? otherwise) })), [start, otherwise]);
  const open = useCallback((id: string) => setChosen((c) => ({ ...c, [id]: true })), []);
  return { isOpen, toggle, open };
}

/** The id of a part's heading button. */
const headId = (id: string) => `me-${id}-toggle`;

interface Props {
  id: string;
  title: string;
  /** A part of the page is a second-level heading over a white sheet; a role inside Experience is a third-level one, a row that
   * opens in place. */
  level?: 2 | 3;
  open: boolean;
  onToggle: () => void;
  /** What is in it, in a few words ("22 phrases"), beside the heading. */
  count?: string;
  /** While it is folded, one line of what is in it. */
  summary?: string;
  /** The one plain line under an open part's heading. */
  hint?: string;
  /** Whether this part has changes that are not saved: the sun dot after its name. */
  dirty?: boolean;
  /** Something small after the name (a tag). */
  tag?: ReactNode;
  children: ReactNode;
}

/** One part of a Me page: a white sheet whose heading is a button that folds it (the whole head is its target), or, for a
 * role, a row that opens in place. Open, it shows its one plain line and its fields; folded, it shows what is in it in a
 * line, so a long page reads as a list of its parts. A folded part does not draw its fields at all. A count that starts
 * with 0 says the part is empty and waits for the owner (the sun's words, never its fill). */
export default function FoldPart({ id, title, level = 2, open, onToggle, count, summary, hint, dirty = false, tag, children }: Props) {
  const toggle = (
    <button type="button" id={headId(id)} aria-expanded={open} aria-controls={`me-${id}-body`} onClick={onToggle}>
      <span className="fi-me-chevron">
        <ChevronIcon />
      </span>
      <span className="fi-me-part-title">{title}</span>
      {dirty && (
        <>
          <span className="fi-me-dirty" aria-hidden="true" />
          <span className="fi-vh">, unsaved changes</span>
        </>
      )}
    </button>
  );
  const folded =
    !open && summary ? (
      <div className="fi-me-part-fold">
        <p className="fi-me-part-summary">{summary}</p>
      </div>
    ) : null;
  const body = (
    <div id={`me-${id}-body`} className="fi-me-part-body" hidden={!open}>
      {open && (
        <>
          {hint && <p className="fi-me-hint">{hint}</p>}
          {children}
        </>
      )}
    </div>
  );

  const waiting = count !== undefined && /^0\s/.test(count);
  const counted = count ? <span className={`fi-me-count${waiting ? " is-waiting" : ""}`}>{count}</span> : null;

  if (level === 3) {
    return (
      <div className="fi-me-part-sub" id={`me-${id}`} data-open={open}>
        <div className="fi-me-part-head">
          <h3>{toggle}</h3>
          {tag}
          {counted}
        </div>
        {folded}
        {body}
      </div>
    );
  }
  return (
    <section className="fi-me-part-main fi-sheet" id={`me-${id}`} aria-labelledby={`me-${id}-head`} data-open={open}>
      <div className="fi-me-part-head">
        <h2 id={`me-${id}-head`}>{toggle}</h2>
        {counted}
      </div>
      {folded}
      {body}
    </section>
  );
}
