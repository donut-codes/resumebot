import type { ReactNode } from "react";

/** One sheet of a My Resume page with its name over it: the crib, the document as built, the
 * history, Find Gaps' target roles and its run. A second-level heading names the section and the section is its landmark. */
export function MeSection({ id, title, hint, className = "", children }: { id: string; title: ReactNode; hint?: ReactNode; className?: string; children: ReactNode }) {
  return (
    <section className={`fi-me-sec ${className}`.trim()} id={id} aria-labelledby={`${id}-head`}>
      <h2 id={`${id}-head`}>{title}</h2>
      {hint && <p className="fi-me-hint">{hint}</p>}
      <div className="fi-sheet">{children}</div>
    </section>
  );
}

/** The one error line of a My Resume page, announced as an alert. */
export function MeAlert({ children }: { children: ReactNode }) {
  return (
    <p className="fi-alert" role="alert">
      {children}
    </p>
  );
}

/** The one "still loading" line of a My Resume page. */
export function MeLoading({ children = "Loading…" }: { children?: ReactNode }) {
  return <p className="fi-me-loading">{children}</p>;
}

/** The one "nothing here yet" line of a My Resume page. */
export function MeEmpty({ children }: { children: ReactNode }) {
  return <p className="fi-me-empty">{children}</p>;
}
