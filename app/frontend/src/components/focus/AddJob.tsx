import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import type { RoleAdded } from "../../api/types/pipeline";
import { useAction } from "../../lib/useAction";

/** The box that adds a job by its link (docs/PRD.md 24.10): the link and Add, and nothing else. The server checks the
 * link and answers with where the job stands; the screen says it and opens the role when there is one. */
export default function AddJob({ id, add, onAdded, onClose }: { id: string; add: (url: string) => Promise<RoleAdded>; onAdded: (added: RoleAdded) => void; onClose: () => void }) {
  const fieldId = useId();
  const field = useRef<HTMLInputElement>(null);
  const [link, setLink] = useState("");
  const send = useAction(async (url: string) => {
    onAdded(await add(url));
  }, { fallback: "Couldn't add that job." });
  // The box opens because the owner asked for it: the link goes straight into it.
  useEffect(() => {
    field.current?.focus();
  }, []);

  return (
    <form
      id={id}
      className="fi-add"
      aria-label="Add a job"
      onSubmit={(ev) => {
        ev.preventDefault();
        if (link.trim() && !send.busy) send.run(link.trim());
      }}
    >
      <label className="fi-add-label" htmlFor={fieldId}>Job link</label>
      <p id={`${fieldId}-hint`} className="fi-add-hint">
        Paste the link to the job&rsquo;s page. It goes through the same steps as any posting the scout finds.
      </p>
      <input
        ref={field}
        id={fieldId}
        className="fi-add-input"
        type="text"
        inputMode="url"
        placeholder="https://"
        aria-describedby={`${fieldId}-hint`}
        value={link}
        onChange={(e) => setLink(e.target.value)}
        onKeyDown={(ev: KeyboardEvent) => {
          if (ev.key === "Escape") onClose();
        }}
      />
      {send.error && <p className="fi-alert" role="alert">{send.error}</p>}
      <div className="fi-add-actions">
        <button type="submit" className="fi-btn fi-btn-go" disabled={send.busy || !link.trim()} title={link.trim() ? undefined : "Paste the link first"}>
          {send.busy ? "Adding…" : "Add"}
        </button>
        <button type="button" className="fi-btn" onClick={onClose}>Cancel</button>
      </div>
    </form>
  );
}
