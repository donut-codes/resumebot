import { useEffect, useId, useState } from "react";
import { api } from "../../api/client";
import type { HarnessFile, HarnessFileHistory, HarnessFileReply, HarnessFileText } from "../../api/types/agents";
import { dateTimeInWords } from "../../lib/dates";
import { useAction } from "../../lib/useAction";
import ErrorBanner from "../primitives/ErrorBanner";
import GrowingText from "../primitives/GrowingText";
import { formatCount } from "../../lib/format";

const chars = (n: number) => `${formatCount(n)} characters`;

/** Every file shown on the page that is the same file hears of a change, so the instructions of one step and the
 * copy of them under another never disagree after a save. */
type Listener = (reply: HarnessFileReply) => void;
const listeners = new Set<Listener>();
const publish = (reply: HarnessFileReply) => listeners.forEach((l) => l(reply));

type Mode = "read" | "edit" | "history";

/** A labelled box that grows with its words, with one plain line of hint tied to it. */
function FileField({ label, hint, value, onChange, rows = 1, oneLine = false, className = "" }: {
  label: string;
  hint: string;
  value: string;
  onChange: (value: string) => void;
  rows?: number;
  oneLine?: boolean;
  className?: string;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  return (
    <div className={`fi-field ${className}`.trim()}>
      <label htmlFor={id}>{label}</label>
      <p id={hintId} className="fi-field-hint">
        {hint}
      </p>
      <GrowingText id={id} className="fi-input" aria-describedby={hintId} minRows={rows} oneLine={oneLine} value={value} onText={onChange} />
    </div>
  );
}

/** How a version is named: "Version 2, your change" or "Version 1, as shipped". */
function versionWords(file: Pick<HarnessFile, "version" | "edited" | "from_app">): string {
  if (file.from_app) return `Version ${file.version}, shipped with the app`;
  return file.edited ? `Version ${file.version}, your change` : `Version ${file.version}, as shipped`;
}

/** One instruction or template file an agent is sent, a row of a ruled listing: what it is, its name, its size and
 * version, and behind its toggle the text verbatim. The owner edits it here (Save keeps the old text as a version, with
 * a one-line why), reads its history, and brings an earlier version back. */
export default function InstructionFile({ file: given }: { file: HarnessFile }) {
  const [file, setFile] = useState(given);
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<Mode>("read");
  const [draft, setDraft] = useState("");
  const [why, setWhy] = useState("");
  const [history, setHistory] = useState<HarnessFileHistory | null>(null);
  const [viewed, setViewed] = useState<HarnessFileText | null>(null);

  // A fresh page load brings the file as it is now.
  useEffect(() => setFile(given), [given]);
  // A change made to this file anywhere on the page.
  useEffect(() => {
    const hear: Listener = (reply) => {
      if (reply.file.kind !== file.kind || reply.file.name !== file.name) return;
      setFile((now) => ({ ...now, ...reply.file }));
      setHistory(reply.history);
      setViewed(null);
    };
    listeners.add(hear);
    return () => {
      listeners.delete(hear);
    };
  }, [file.kind, file.name]);

  const done = (reply: HarnessFileReply | undefined) => {
    if (!reply) return;
    publish(reply);
    setMode("read");
    setWhy("");
  };
  const save = useAction(
    async () => done(await api.saveHarnessFile(file.kind, file.name, { text: draft, why })),
    { fallback: "Couldn't save the file." },
  );
  const restore = useAction(
    async (version: number) => done(await api.restoreHarnessFile(file.kind, file.name, version)),
    { fallback: "Couldn't bring that version back." },
  );
  const loadHistory = useAction(
    async () => setHistory((await api.harnessFile(file.kind, file.name)).history),
    { fallback: "Couldn't load the history." },
  );
  const view = useAction(
    async (version: number) => setViewed(await api.harnessFileVersion(file.kind, file.name, version)),
    { fallback: "Couldn't load that version." },
  );

  const startEdit = () => {
    setDraft(file.text);
    setWhy("");
    save.setError(null);
    setMode("edit");
  };
  const showHistory = () => {
    setViewed(null);
    setMode("history");
    void loadHistory.run();
  };
  const busy = save.busy || restore.busy;
  const unchanged = draft.replace(/\r\n/g, "\n").trim() === file.text;
  const mustKeep = file.placeholders.map((p) => `{{${p}}}`);
  const failure = save.error ?? restore.error ?? loadHistory.error ?? view.error;

  return (
    <div className="how-file-item">
      <div className="how-file-row">
        <button type="button" className="how-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
          {open ? "Hide" : "Show"} {file.title.toLowerCase()}
        </button>
        <span className="how-file-name">{file.name}</span>
        <span className="fi-note how-file-size">
          {" "}
          · {chars(file.chars)} · {versionWords(file).toLowerCase()}
        </span>
      </div>
      {open && (
        <div className="how-file-body">
          {file.busy && <p className="how-file-busy">{file.busy}</p>}
          {mode === "read" && (
            <>
              <div className="how-file-actions">
                <button type="button" className="fi-link" onClick={startEdit} disabled={!!file.busy}>
                  Edit
                </button>
                <button type="button" className="fi-link" onClick={showHistory}>
                  History
                </button>
              </div>
              <pre className="how-file">{file.text}</pre>
            </>
          )}
          {mode === "edit" && (
            <form
              className="how-file-edit"
              onSubmit={(e) => {
                e.preventDefault();
                void save.run();
              }}
            >
              <FileField
                label={`Edit ${file.name}`}
                hint={
                  mustKeep.length > 0
                    ? `The app fills in ${mustKeep.join(", ")} for each task; leave them as they are.`
                    : "The agent is sent this text exactly as written."
                }
                value={draft}
                onChange={setDraft}
                rows={12}
                className="how-file-text"
              />
              <FileField
                label="Why are you changing it?"
                hint="One line. It is kept with this version."
                value={why}
                onChange={setWhy}
                oneLine
              />
              <div className="how-file-actions">
                <button type="submit" className="fi-btn fi-btn-go" disabled={busy || unchanged || !why.trim()}>
                  {save.busy ? "Saving…" : "Save as a new version"}
                </button>
                <button type="button" className="fi-link" onClick={() => setMode("read")} disabled={busy}>
                  Cancel
                </button>
              </div>
            </form>
          )}
          {mode === "history" && (
            <div>
              <div className="how-file-actions">
                <button type="button" className="fi-link" onClick={() => setMode("read")}>
                  Back to the text
                </button>
              </div>
              {!history && !loadHistory.error && <p className="fi-note">Loading the history&hellip;</p>}
              <ol className="how-versions" aria-label={`Versions of ${file.name}`}>
                {history?.versions.map((v) => (
                  <li key={v.version} className="how-version">
                    <div className="how-version-row">
                      <span className="how-version-num">
                        Version {v.version}
                        {v.current ? " (in force)" : ""}
                      </span>
                      <span className="fi-note">
                        {v.at ? dateTimeInWords(v.at) : "as shipped"} · {v.by} · {chars(v.chars)}
                      </span>
                      <span className="how-version-why">{v.why}</span>
                      <span className="how-version-actions">
                        <button
                          type="button"
                          className="fi-link"
                          aria-expanded={viewed?.version === v.version}
                          onClick={() => (viewed?.version === v.version ? setViewed(null) : void view.run(v.version))}
                        >
                          {viewed?.version === v.version ? "Hide" : "View"}
                          <span className="fi-vh"> version {v.version}</span>
                        </button>
                        {!v.current && (
                          <button
                            type="button"
                            className="fi-link"
                            onClick={() => void restore.run(v.version)}
                            disabled={!!file.busy || busy}
                          >
                            Restore this version
                            <span className="fi-vh"> (version {v.version})</span>
                          </button>
                        )}
                      </span>
                    </div>
                    {viewed?.version === v.version && <pre className="how-file">{viewed.text}</pre>}
                  </li>
                ))}
              </ol>
            </div>
          )}
          {failure && <ErrorBanner>{failure}</ErrorBanner>}
        </div>
      )}
    </div>
  );
}
