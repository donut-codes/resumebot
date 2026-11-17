import { forwardRef, useId } from "react";

interface Props {
  /** Who has changes, in words ("Company and Builders"). */
  what: string;
  why: string;
  onWhy: (why: string) => void;
  busy: boolean;
  /** Why the last save didn't go through, in the server's words. */
  error: string | null;
  /** Whether that failure was the server saying the page is out of date: the bar then offers Reload, and what was typed
   * stays on the page until the person reloads. */
  stale?: boolean;
  /** Puts every changed part back to its last saved text. */
  onUndo: () => void;
  onSave: () => void;
}

/** The one Save of a My Resume page: a white bar, lifted, stuck to the bottom of the pane that is there only while something
 * on the page is unsaved. It says what changed (the sun "Unsaved changes" badge, then the names), takes an optional one-line
 * reason for the history, and offers Undo and the one sun button, Save (also Ctrl or Cmd with S).
 * A save refused because the page is out of date says so and offers Reload; the changes stay where they are. */
const SaveBar = forwardRef<HTMLFormElement, Props>(function SaveBar({ what, why, onWhy, busy, error, stale = false, onUndo, onSave }, ref) {
  const whyId = useId();
  return (
    <form
      ref={ref}
      className="fi-me-bar"
      aria-label="Unsaved changes"
      onSubmit={(e) => {
        e.preventDefault();
        if (!busy) onSave();
      }}
    >
      {error && (
        <p className="fi-alert" role="alert">
          {error}
          {stale && (
            <>
              {" "}
              {/* The browser asks before it throws the typed changes away (the unsaved guard). */}
              <button type="button" className="fi-link" onClick={() => window.location.reload()}>
                Reload
              </button>
            </>
          )}
        </p>
      )}
      <div className="fi-me-bar-row">
        <p className="fi-me-bar-what">
          <span className="fi-badge fi-me-unsaved">Unsaved changes</span> to <strong>{what}</strong>
        </p>
        <label className="fi-vh" htmlFor={whyId}>
          Why (optional, one line)
        </label>
        <input
          id={whyId}
          type="text"
          className="fi-input fi-me-bar-why"
          placeholder="Why (optional, one line)"
          value={why}
          onChange={(e) => onWhy(e.target.value)}
        />
        <div className="fi-me-bar-actions">
          <button type="button" className="fi-btn" disabled={busy} onClick={onUndo}>
            Undo
          </button>
          <button type="submit" className="fi-btn fi-btn-go" disabled={busy}>
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </form>
  );
});

export default SaveBar;
