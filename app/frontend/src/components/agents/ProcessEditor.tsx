import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../../api/client";
import type { ProcessHistory } from "../../api/types/agents";
import { BpmnProcessView } from "process-view";
// The package's editor and step-detail styles: its barrel imports them, but a build that uses only part of the package
// drops that import, so this view brings them itself.
import "../../../../packages/process-view/src/editor.css";
import { useAction } from "../../lib/useAction";
import { useResource } from "../../lib/useResource";
import ErrorBanner from "../primitives/ErrorBanner";
import Loading from "../primitives/Loading";

/** Edit mode for one process (docs/PRD.md 16.1): the process view package's editor on the file that runs, a check by the
 * engine's own loader as the owner types, and a save that is the process's next version. There is no approval step:
 * the next run uses what is saved, and a run going now finishes as it started. */
export default function ProcessEditor({
  processKey,
  onSaved,
  onClose,
}: {
  processKey: string;
  /** The new history after a save. */
  onSaved: (history: ProcessHistory) => void;
  onClose: () => void;
}) {
  const { data: file, error: fileError, reload: reloadFile } = useResource(
    () => api.processBpmn(processKey),
    [processKey],
    { fallback: "Couldn't load this process's file.", reset: true },
  );
  const [draft, setDraft] = useState<string | null>(null);
  // The version the edit was started on, kept while it is edited even if a newer one arrives meanwhile: it is sent with
  // the save, so the server can refuse a save made on a version that is no longer the one in use.
  const [basedOn, setBasedOn] = useState<number | null>(null);
  const [valid, setValid] = useState<boolean | null>(null);
  const [why, setWhy] = useState("");
  // Only the latest check's answer counts: one that comes back after a newer check was asked for is ignored.
  const latestCheck = useRef(0);
  const root = useRef<HTMLDivElement>(null);
  const started = useRef(false);

  // The owner pressed Edit to get here: the view's own Edit toggle is pressed for them, so the editor is open at once.
  useEffect(() => {
    if (!file || started.current) return;
    started.current = true;
    root.current?.querySelector<HTMLButtonElement>(".pv-edit-toggle")?.click();
  }, [file]);

  const save = useAction(
    async () => {
      if (!draft || !file) return;
      const history = await api.saveProcessEdit(processKey, draft, why.trim(), basedOn ?? file.number);
      latestCheck.current++;
      onSaved(history);
    },
    {
      fallback: "Couldn't save your edit.",
      // The process moved on while it was being edited: read the newer version now, so the editor can say it changed.
      onError: (_message, err) => {
        if (err instanceof ApiError && err.status === 409) reloadFile();
      },
    },
  );

  const changed = !!draft && !!file && draft !== file.xml;
  const ready = changed && valid === true && why.trim() !== "";
  const blocker = !changed
    ? "Change a step first"
    : valid === false
      ? "It can't run as written yet"
      : valid === null
        ? "Checking it can run"
        : why.trim() === ""
          ? "Say why in a line first"
          : undefined;

  return (
    <div ref={root}>
      {fileError && <ErrorBanner as="p">{fileError}</ErrorBanner>}
      {!file && !fileError && <Loading>Loading&hellip;</Loading>}
      {file && (
        <>
          <div className="how-edit-bar">
            <p className="how-edit-says">
              You are editing version {file.number}. Saving makes your edit the next version: the next run uses it, and a
              run going now finishes as it started. Any earlier version can be brought back.
            </p>
            {file.note && <p className="how-edit-says">{file.note}</p>}
            <div className="how-edit-row">
              <div className="fi-field how-edit-why">
                <label htmlFor="how-edit-why">Why are you changing it?</label>
                <input
                  id="how-edit-why"
                  className="fi-input"
                  type="text"
                  value={why}
                  placeholder="One line, for the history"
                  onChange={(e) => setWhy(e.target.value)}
                />
              </div>
              <div className="how-edit-buttons">
                <button
                  type="button"
                  className="fi-btn fi-btn-go"
                  disabled={!ready || save.busy}
                  title={blocker}
                  onClick={() => save.run()}
                >
                  {save.busy ? "Saving…" : "Save as the next version"}
                </button>
                <button type="button" className="fi-btn fi-btn-quiet" onClick={onClose}>
                  {changed ? "Discard and close" : "Close"}
                </button>
              </div>
            </div>
            {save.error && <ErrorBanner as="p">{save.error}</ErrorBanner>}
          </div>
          <BpmnProcessView
            bpmn={file.xml}
            editable
            onChange={(xml) => {
              if (xml === file.xml) {
                // Back to the version in use: nothing to save, and no answer still on its way is of any use.
                latestCheck.current++;
                setDraft(null);
                setBasedOn(null);
                setValid(null);
                return;
              }
              setDraft(xml);
              // The check of the text before this edit says nothing of this one; its answer is on its way.
              setValid(null);
              setBasedOn((n) => n ?? file.number);
            }}
            onValidate={async (xml) => {
              const mine = ++latestCheck.current;
              const checked = await api.validateProcessEdit(processKey, xml);
              if (mine === latestCheck.current) setValid(checked.ok);
              return { ok: checked.ok, problems: checked.problems };
            }}
            performerOnRows
          />
        </>
      )}
    </div>
  );
}
