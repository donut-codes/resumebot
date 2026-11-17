// What every part of Search Settings shares: the Saved line in the pane's head (Undo, and a reason said after the save),
// the history of a part as a sheet at the foot of its pane, the scope control of Hard pass and Prioritization, and the
// draft an editor is typed into before it is saved. Each reads the search store or draws from props; none calls the API.

import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import type { ProfileVersion } from "../../api/types/profile";
import { dateTimeInWords } from "../../lib/dates";
import { useSearch, useSearchStore, type Part } from "../../state/searchStore";
import { SavedLine, Sheet } from "../focus/parts";
import { ALL_TAB, roleLabel } from "../profile/scope";
import { plainRefusal } from "../profile/targetRoles";

/** The Saved line for the part shown: Saving, Saved with Undo and "Add a reason", or why it did not go (with what was
 * typed, so it can be put in again). Nothing for a save of another part. */
export function SaveHead({ parts }: { parts: Part[] }) {
  const store = useSearchStore();
  const saved = useSearch((s) => s.saved);
  const [asking, setAsking] = useState(false);
  const [why, setWhy] = useState("");
  const id = useId();
  const version = saved.version;
  const [seenVersion, setSeenVersion] = useState(version);
  if (version !== seenVersion) {
    setSeenVersion(version);
    setAsking(false);
    setWhy("");
  }
  if (!saved.part || !parts.includes(saved.part) || saved.state === "idle") return null;
  const keep = async () => {
    if (why.trim() && (await store.addWhy(why))) setAsking(false);
  };
  return (
    <div className="fi-ss-saved">
      <SavedLine state={saved.state} error={plainRefusal(saved.error)} onUndo={saved.canUndo ? () => void store.undo() : undefined}>
        {saved.why ? (
          <span className="fi-ss-why">{saved.why}</span>
        ) : (
          !asking && (
            <button type="button" className="fi-link" onClick={() => setAsking(true)}>
              Add a reason
            </button>
          )
        )}
      </SavedLine>
      {saved.state === "saved" && asking && !saved.why && (
        <form
          className="fi-ss-reason"
          onSubmit={(e) => {
            e.preventDefault();
            void keep();
          }}
        >
          <label className="fi-vh" htmlFor={id}>
            Why you changed it
          </label>
          <input id={id} className="fi-input" autoComplete="off" placeholder="Why you changed it" value={why} onChange={(e) => setWhy(e.target.value)} />
          <button type="submit" className="fi-btn" disabled={!why.trim()}>
            Keep
          </button>
        </form>
      )}
      {saved.state === "saved" && saved.error && <p className="fi-saved is-failed">{saved.error}</p>}
      {saved.state === "failed" && saved.typed && <p className="fi-ss-typed">You had written: {saved.typed}</p>}
    </div>
  );
}

function savedBy(who: string): string {
  if (who === "owner") return "You";
  if (who === "import") return "Imported";
  return who.charAt(0).toUpperCase() + who.slice(1);
}

/** Every saved version of a part, newest first (who, when, why, what changed): a sheet at the foot of the pane. It is
 * read again when the part's version moves. */
export function HistorySheet({ part }: { part: Part }) {
  const store = useSearchStore();
  const version = useSearch((s) => s.profile?.versions[part] ?? 0);
  const [versions, setVersions] = useState<ProfileVersion[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    store.history(part).then(
      (h) => live && (setVersions(h.versions), setFailed(false)),
      () => live && setFailed(true),
    );
    return () => {
      live = false;
    };
  }, [store, part, version]);
  return (
    <Sheet title="History" count={version ? `${version} ${version === 1 ? "version" : "versions"}` : undefined}>
      {failed ? (
        <p className="fi-alert">Couldn&rsquo;t load the history.</p>
      ) : !versions ? (
        <p className="fi-ss-quiet">Loading…</p>
      ) : versions.length === 0 ? (
        <p className="fi-ss-quiet">Never saved.</p>
      ) : (
        <ol className="fi-ss-history">
          {versions.map((v) => (
            <li key={v.version}>
              <div className="fi-ss-history-head">
                <strong>Version {v.version}</strong>
                <span>
                  {savedBy(v.saved_by)}
                  {v.saved_at && `, ${dateTimeInWords(v.saved_at)}`}
                </span>
              </div>
              {v.why && <p className="fi-ss-history-why">{v.why}</p>}
              {v.changed.length > 0 && <p className="fi-ss-quiet">{v.changed.join("; ")}</p>}
            </li>
          ))}
        </ol>
      )}
    </Sheet>
  );
}

/** All, or one target role: the scope of Hard pass and Prioritization, as the Pipeline's segmented control. */
export function ScopeControl({ roles, scope, onScope }: { roles: { id: string; name: string }[]; scope: string; onScope: (scope: string) => void }) {
  const box = useRef<HTMLDivElement>(null);
  const [thumb, setThumb] = useState<{ left: number; top: number; width: number; height: number } | null>(null);
  useLayoutEffect(() => {
    const place = () => {
      const on = box.current?.querySelector<HTMLElement>('[aria-pressed="true"]');
      setThumb(on ? { left: on.offsetLeft, top: on.offsetTop, width: on.offsetWidth, height: on.offsetHeight } : null);
    };
    place();
    // Placed again once the face has loaded and whenever the control changes size, so the marker fits its words.
    void document.fonts?.ready.then(place);
    const watch = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(place);
    box.current?.querySelectorAll("button").forEach((b) => watch?.observe(b));
    window.addEventListener("resize", place);
    return () => {
      watch?.disconnect();
      window.removeEventListener("resize", place);
    };
  }, [scope, roles.length]);
  const tabs = [{ id: ALL_TAB, name: "All roles" }, ...roles.map((r) => ({ id: r.id, name: roleLabel(r.name) }))];
  return (
    <div className="fi-seg fi-ss-scope" role="group" aria-label="For which roles" ref={box}>
      <span className="fi-seg-thumb" style={thumb ? { insetInlineStart: thumb.left, insetBlockStart: thumb.top, inlineSize: thumb.width, blockSize: thumb.height } : { inlineSize: 0, opacity: 0 }} />
      {tabs.map((t) => (
        <button key={t.id} type="button" aria-pressed={t.id === scope} onClick={() => t.id !== scope && onScope(t.id)}>
          {t.name}
        </button>
      ))}
    </div>
  );
}

const TYPED = new Set(["text", "number", "search", "url", "email", "tel", ""]);
/** Whether a change on this element is typing (saved when the field is left) rather than a click (saved at once). */
function isTyping(el: EventTarget | null): boolean {
  if (el instanceof HTMLTextAreaElement) return true;
  return el instanceof HTMLInputElement && TYPED.has(el.type);
}

/** An item's edits as they are typed, handed on to be saved when a field is left, or at once when a box is ticked, a
 * choice is made or a button pressed. It takes a new value from outside while nothing is typed and unsaved in it. */
export function Draft<T>({ value, onSave, children }: { value: T; onSave: (next: T) => void; children: (draft: T, change: (next: T) => void) => ReactNode }) {
  const [edit, setEdit] = useState<{ draft: T; dirty: boolean }>({ draft: value, dirty: false });
  // How the last change came: typed (saved when the field is left) or clicked (saved at once).
  const [by, setBy] = useState<"type" | "click">("click");
  const [seen, setSeen] = useState(value);
  if (value !== seen) {
    setSeen(value);
    if (!edit.dirty) setEdit({ draft: value, dirty: false });
  }
  const commit = () => {
    if (!edit.dirty) return;
    setEdit({ draft: edit.draft, dirty: false });
    onSave(edit.draft);
  };
  // A click's change is saved once it is drawn.
  useEffect(() => {
    if (by !== "click" || !edit.dirty) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- one render after a click's change: it marks it handed on, once
    setEdit({ draft: edit.draft, dirty: false });
    onSave(edit.draft);
  }, [by, edit, onSave]);
  const change = (next: T) => setEdit({ draft: next, dirty: true });
  const note = (target: EventTarget | null) => setBy(isTyping(target) ? "type" : "click");
  return (
    <div
      className="fi-ss-draft"
      onChangeCapture={(e) => note(e.target)}
      onClickCapture={(e) => note(e.target)}
      onKeyDownCapture={(e) => note(e.target)}
      onBlur={(e) => {
        if (isTyping(e.target)) commit();
      }}
    >
      {children(edit.draft, change)}
    </div>
  );
}
