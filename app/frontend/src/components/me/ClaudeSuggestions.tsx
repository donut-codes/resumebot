import { useRef, useState } from "react";
import type { GapProposal, Resume } from "../../api/types/me";
import { useRefreshKey } from "../../lib/appContext";
import { useResource } from "../../lib/useResource";
import { Proposal, type ClarifyActions } from "./GapClarify";

/** The calls the block makes; the page that owns the API supplies them (as FindGaps does for the Coach's proposals). */
export interface SuggestionCalls {
  open: () => Promise<{ version: number; proposals: GapProposal[] }>;
  accept: (id: string, text: string | undefined, baseVersion: number) => Promise<{ version: number; resume: Resume }>;
  reject: (id: string) => Promise<unknown>;
}

interface Props {
  calls: SuggestionCalls;
  /** The master version this page loaded: an accept names it, and a newer master refuses the accept in words. */
  baseVersion: number;
  /** Whether the page holds unsaved edits of the master: an accept then does not replace them. */
  dirty: boolean;
  /** A suggestion was accepted: the master as saved and its version. Not called while there are unsaved edits. */
  onSavedResume: (resume: Resume, version: number) => void;
}

/** The lines the owner's own Claude suggested for the master resume (docs/PRD.md 50.15), waiting for the owner to accept, edit
 * or reject each. Shown only when there are some; nothing is in the master until the owner accepts. It reads the list again
 * when Find Gaps' area changes (a suggestion arrives while the page is open) and after each decision. */
export default function ClaudeSuggestions({ calls, baseVersion, dirty, onSavedResume }: Props) {
  const refreshKey = useRefreshKey();
  const open = useResource(() => calls.open(), [refreshKey], { liveArea: "gaps" });
  const [kept, setKept] = useState(false);
  const { reload } = open;
  // The newest master version an accept here answered with: the page's own base stays old while it holds unsaved edits.
  const savedVersion = useRef(0);
  const actions: Pick<ClarifyActions, "acceptProposal" | "rejectProposal"> = {
    acceptProposal: async (id, text) => {
      try {
        const saved = await calls.accept(id, text, Math.max(baseVersion, savedVersion.current));
        savedVersion.current = Math.max(savedVersion.current, saved.version);
        return saved;
      } catch (err) {
        reload(); // a newer master (or a decided suggestion): the list is read again, and the words say why
        throw err;
      }
    },
    rejectProposal: (id) => calls.reject(id),
  };
  const rows = open.data?.proposals ?? [];
  if (rows.length === 0 && !kept) return null;
  return (
    <section className="fi-me-sec fi-me-claude" aria-labelledby="me-claude-head">
      <h2 id="me-claude-head">Suggested by your Claude</h2>
      <p className="fi-me-hint">Lines your Claude suggested from what you told it. Nothing is in your master resume until you accept it.</p>
      {kept && (
        <p className="fi-me-filled" role="status">
          Saved. Your unsaved edits are kept; reload to see the new line.
        </p>
      )}
      <div className="fi-me-gap-proposals">
        {rows.map((p) => (
          <Proposal
            key={p.id}
            proposal={p}
            actions={actions as ClarifyActions}
            onChanged={reload}
            onSavedResume={(resume, version) => {
              if (dirty) setKept(true);
              else onSavedResume(resume, version);
            }}
          />
        ))}
      </div>
    </section>
  );
}
