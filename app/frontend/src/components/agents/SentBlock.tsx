import { useState } from "react";
import { api } from "../../api/client";
import type { HarnessExample, HarnessOverview, HarnessSent, ProcessRunSent } from "../../api/types/agents";
import { dateTimeInWords } from "../../lib/dates";
import { formatCount } from "../../lib/format";
import { useAction } from "../../lib/useAction";
import { useResource } from "../../lib/useResource";
import { Block } from "../focus/parts";
import ErrorBanner from "../primitives/ErrorBanner";
import Loading from "../primitives/Loading";
import InstructionFile from "./InstructionFile";

const chars = (n: number) => `${formatCount(n)} characters`;


/** Whether an example is a copy of what an agent was sent (and when), or rebuilt from today's files; and which of
 * the files it was sent with are different now. */
function ExampleLabel({ example }: { example: HarnessExample }) {
  const changed = example.changed_files ?? [];
  const label = example.sent_at ? `${example.label} at ${dateTimeInWords(example.sent_at)}` : example.label;
  // The files it was sent with, and the version of each (a copy kept before versions says only the name).
  const sentWith = (example.files ?? []).map((f) => (f.version ? `${f.name} (version ${f.version})` : f.name));
  return (
    <p className="fi-note">
      {label.charAt(0).toUpperCase() + label.slice(1)}.
      {sentWith.length > 0 && <> Sent with: {sentWith.join(", ")}.</>}
      {changed.length > 0 && (
        <>
          {" "}
          Changed since: {changed.map((f) => `${f.name}${f.now === "gone" ? " (gone)" : ""}`).join(", ")}.
        </>
      )}
    </p>
  );
}

/** What the agent is sent at one step: the files as they are on disk, the parts added with the task, and a real
 * example. `fixed` is one search's own example (opened from an agent's Activity). */
export function SentBlock({
  stepId,
  sent,
  fixed,
  startOpen = false,
}: {
  stepId: string;
  sent: HarnessSent;
  fixed?: HarnessExample | null;
  startOpen?: boolean;
}) {
  const [open, setOpen] = useState(startOpen);
  const [example, setExample] = useState<HarnessExample | null | undefined>(fixed ?? undefined);
  const [showExample, setShowExample] = useState(!!fixed);
  const { run: loadExample, busy, error } = useAction(
    async () => {
      const got = await api.harnessExample(stepId);
      setExample(got.example);
      setShowExample(true);
    },
    { fallback: "Couldn't load an example." },
  );
  const sizes = new Map((example?.parts ?? []).map((p) => [p.key, p.chars]));
  const budget = example?.budget_chars ?? sent.budget_chars;
  const hasFields = sent.fields.length > 0;

  return (
    <div className="how-sent" onClick={(e) => e.stopPropagation()}>
      <button type="button" className="fi-btn how-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        {open ? "Hide what the agent is sent" : "What the agent is sent"}
      </button>
      {open && (
        <div>
          <div className="how-part">
            <h4 className="how-part-title">Given once, before any task</h4>
            <div className="how-files">
              {sent.role_files.map((f) => (
                <InstructionFile key={f.name} file={f} />
              ))}
            </div>
          </div>
          {sent.instruction && (
            <div className="how-part">
              <h4 className="how-part-title">Sent with this step</h4>
              <div className="how-files">
                <InstructionFile file={sent.instruction} />
              </div>
            </div>
          )}
          {sent.template && (
            <div className="how-part">
              <h4 className="how-part-title">The shape of its answer</h4>
              <div className="how-files">
                <InstructionFile file={sent.template} />
              </div>
            </div>
          )}
          {hasFields && (
            <div className="how-part">
              <h4 className="how-part-title">Added with the task, field by field</h4>
              <table className="how-fields">
                <thead>
                  <tr>
                    <th>Field</th>
                    <th>What it is</th>
                    <th className="num">{example ? "Size" : ""}</th>
                  </tr>
                </thead>
                <tbody>
                  {sent.fields.map((f) => (
                    <tr key={`${f.group}-${f.key}`}>
                      <td>
                        <code>{f.key}</code>
                      </td>
                      <td>{f.means}</td>
                      <td className="num">{sizes.has(f.key) ? chars(sizes.get(f.key)!) : ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="fi-note">
                Nothing sent to an agent is cut. A reply over {chars(sent.budget_chars)} is refused, with the reason.
              </p>
            </div>
          )}
          {sent.has_example && (
            <div className="how-part">
              {!showExample && (
                <button type="button" className="fi-btn how-toggle" disabled={busy} onClick={() => loadExample()}>
                  {busy ? "Loading…" : "Show a real example"}
                </button>
              )}
              {error && <ErrorBanner as="p">{error}</ErrorBanner>}
              {showExample && example === null && <p className="fi-note">No task like this has been sent yet.</p>}
              {showExample && example && (
                <div>
                  <h4 className="how-part-title">
                    A real one: &ldquo;{example.title}&rdquo; · {chars(example.chars)} of {chars(budget)}
                  </h4>
                  <ExampleLabel example={example} />
                  <div className="how-meter" aria-hidden="true">
                    <span style={{ width: `${Math.min(100, Math.round((example.chars / budget) * 100))}%` }} />
                  </div>
                  <pre className="how-file">{JSON.stringify(example.payload, null, 2)}</pre>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** One kept copy of what an agent was sent, opened where the run passed the step. */
function SentOne({ taskId }: { taskId: string }) {
  const { data, error } = useResource(() => api.harnessTask(taskId), [taskId], {
    fallback: "Couldn't load what was sent.",
  });
  if (error) return <ErrorBanner as="p">{error}</ErrorBanner>;
  if (!data) return <Loading>Loading what was sent&hellip;</Loading>;
  if (!data.example) return <p className="fi-note">That task can't be shown now.</p>;
  return (
    <div>
      <ExampleLabel example={data.example} />
      <pre className="how-file">{JSON.stringify(data.example.payload, null, 2)}</pre>
    </div>
  );
}

/** "What was sent" at an agent step a run reached: each copy kept, opened on request. */
export function RunSent({ items }: { items: ProcessRunSent[] }) {
  const [openId, setOpenId] = useState<string | null>(null);
  return (
    <div className="how-part" onClick={(e) => e.stopPropagation()}>
      <h4 className="how-part-title">What was sent</h4>
      <ul className="how-run-sent-list">
        {items.map((it) => {
          const open = openId === it.task_id;
          return (
            <li key={it.task_id}>
              <button
                type="button"
                className="how-toggle"
                aria-expanded={open}
                onClick={() => setOpenId(open ? null : it.task_id)}
              >
                {open ? "Hide" : "Show"} what was sent{it.agent ? ` to ${it.agent}` : ""}, {dateTimeInWords(it.sent_at)}
              </button>
              {it.title && <span className="fi-note"> &ldquo;{it.title}&rdquo;</span>}
              {open && <SentOne taskId={it.task_id} />}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** What was sent for one search: its step with the files, and its own payload. */
export function SentForTask({
  taskId,
  overview,
  onSeeAll,
}: {
  taskId: string;
  overview: HarnessOverview;
  onSeeAll?: () => void;
}) {
  const { data, error } = useResource(() => api.harnessTask(taskId), [taskId], {
    fallback: "Couldn't load what was sent.",
  });
  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <Loading>Loading what was sent&hellip;</Loading>;
  const process = overview.processes.find((p) => p.id === data.process);
  const step = data.step ?? process?.steps.find((s) => s.id === data.step?.id) ?? null;
  return (
    <Block id="how-sent" title="What was sent for this search">
      <div className="how-sent-task">
        {data.example && <p className="how-sent-for">&ldquo;{data.example.title}&rdquo;</p>}
        <p className="how-sent-says">
          {step ? `Step: ${step.title}. ` : ""}
          {data.example
            ? data.example.source === "sent"
              ? "This is the task as the agent was handed it."
              : "This task has no copy kept, so this is it as it would be handed to the agent now."
            : "The agent can't be sent this task now."}
        </p>
        {step?.sent ? (
          <SentBlock
            stepId={step.id}
            sent={step.sent}
            fixed={data.example}
            startOpen
          />
        ) : (
          <p className="fi-note">Nothing is sent to an agent at this step.</p>
        )}
      </div>
      {onSeeAll && (
        <div className="how-sent-foot">
          <button type="button" className="fi-btn" onClick={onSeeAll}>
            See how all the steps work
          </button>
        </div>
      )}
    </Block>
  );
}
