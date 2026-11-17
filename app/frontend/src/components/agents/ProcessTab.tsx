import { useMemo, useState } from "react";
import type { HarnessProcess, HarnessStep, ProcessRunDetail } from "../../api/types/agents";
import { ProcessView, START_ID, type ProcessStep } from "process-view";
import { Block } from "../focus/parts";
import { DataModelBlock, DecisionWays, visibleFacts } from "./DecisionWays";
import { harnessToProcess } from "./harnessProcess";
import ProcessEditor from "./ProcessEditor";
import ProcessVersions from "./ProcessVersions";
import { RunHeader } from "./RunHeader";
import { RunSent, SentBlock } from "./SentBlock";
import StepSpec from "./StepSpec";

export function ProcessTab({
  process,
  run,
  onOpenRun,
  onLeaveRun,
  childProcesses,
  calledFrom,
  onOpen,
  sectioned,
  onChanged,
}: {
  process: HarnessProcess;
  /** The process was changed (saved or restored): the page reads it again. */
  onChanged: () => void;
  /** On the page (true) the process and its parts are sections under bands; alone (false) they are plain. */
  sectioned: boolean;
  /** A running instance of this process: its current step and the steps it passed are marked. */
  run?: ProcessRunDetail | null;
  onOpenRun?: (id: string) => void;
  onLeaveRun?: () => void;
  /** The sub-processes this one calls, which a step can open. */
  childProcesses: HarnessProcess[];
  /** The processes that call this one, each with the way to its tab. */
  calledFrom: { process: HarnessProcess; open: () => void }[];
  onOpen: (p: HarnessProcess) => void;
}) {
  const graph = useMemo(() => harnessToProcess(process), [process]);
  // Edit mode (docs/PRD.md 16.1): the owner, on the page, and not while a run is open. A run going elsewhere finishes on
  // the version it started with, so a save never changes it.
  const canEdit = sectioned && !run;
  const [editing, setEditing] = useState(false);
  const [saved, setSaved] = useState<number | null>(null);
  const byId = useMemo(() => new Map(process.steps.map((s) => [s.id, s])), [process]);
  // Where the run is: the step it is at, and the steps it passed.
  const marks = useMemo(() => {
    if (!run) return undefined;
    const out: Record<string, "current" | "passed"> = {};
    for (const p of run.passed) out[p.step] = "passed";
    if (run.current) out[run.current] = "current";
    return out;
  }, [run]);
  const renderDetail = (step: ProcessStep) => {
    if (step.id === START_ID) return null;
    const s: HarnessStep | undefined = byId.get(step.id);
    if (!s) return null;
    const called = s.calls ? childProcesses.find((c) => c.id === s.calls) : undefined;
    return (
      <>
        {called && (
          <p className="how-open-sub">
            <button type="button" className="fi-link" onClick={() => onOpen(called)}>
              Open {called.name}
            </button>
          </p>
        )}
        <StepSpec step={s} />
        {s.ways && s.ways.length > 0 && <DecisionWays step={s} stepTitle={(id) => byId.get(id)?.title ?? id} />}
        {s.who_about && s.type === "task" && <p className="fi-note">{s.who_about}</p>}
        {run && marks?.[s.id] && (run.sent[s.id]?.length ?? 0) > 0 && (
          <RunSent items={run.sent[s.id]} />
        )}
        {run && marks?.[s.id] && s.sent && !run.sent[s.id] && (
          <p className="fi-note">No copy of what was sent at this step was kept for this run.</p>
        )}
        {s.sent ? (
          <SentBlock stepId={s.harness_id ?? s.id} sent={s.sent} />
        ) : s.type === "task" ? (
          <p className="fi-note">Nothing is sent to an agent at this step.</p>
        ) : null}
      </>
    );
  };
  const facts = process.data_model && visibleFacts(process.data_model).length > 0 ? process.data_model : null;
  const calledFromLine = calledFrom.length > 0 && (
    <p className="how-called-from">
      Called from{" "}
      {calledFrom.map(({ process: parent, open }, i) => (
        <span key={parent.id}>
          {i > 0 ? ", " : ""}
          <button type="button" className="fi-link" onClick={open}>
            {parent.name}
          </button>
        </span>
      ))}
      . It is documented once, here.
    </p>
  );
  const view = editing ? (
    <ProcessEditor
      processKey={process.id}
      onClose={() => setEditing(false)}
      onSaved={(history) => {
        setEditing(false);
        setSaved(history.running.number);
        onChanged();
      }}
    />
  ) : (
    <ProcessView
      steps={graph.steps}
      edges={graph.edges}
      modeControl={
        canEdit ? (
          <button
            type="button"
            className="mode-toggle pv-edit-toggle"
            onClick={() => {
              setSaved(null);
              setEditing(true);
            }}
          >
            Edit
          </button>
        ) : undefined
      }
      renderDetail={renderDetail}
      performerOnRows
      marks={marks}
      // A run is drawn settled, so the graph is fitted to its box once and stays put.
      live={!run}
      currentWord={run?.ended ? "Ended here" : undefined}
    />
  );
  const runHeader = run && onOpenRun && onLeaveRun && <RunHeader run={run} onOpenRun={onOpenRun} onLeave={onLeaveRun} />;

  // One process alone (a mission's playbook): no page around it, so no bands.
  if (!sectioned) {
    return (
      <div>
        {runHeader}
        <p className="fi-note how-about">{process.about}</p>
        {calledFromLine}
        {view}
        {facts && <DataModelBlock fields={facts} />}
      </div>
    );
  }
  // On the page: the run (if one is open), the process under a band of its own name, and the facts a rule can see.
  return (
    <>
      {runHeader}
      <Block id="how-process" title={process.name} hint={process.about} flush className="how-process">
        {calledFromLine}
        {saved != null && !editing && (
          <p className="how-saved" role="status">
            Saved. Version {saved} is in use: the next run follows it, and a run going now finishes as it started.
          </p>
        )}
        {view}
      </Block>
      {!run && (
        <ProcessVersions process={process} onChanged={onChanged} />
      )}
      {facts && (
        <Block id="how-facts" title="What a rule can look at" flush>
          <DataModelBlock fields={facts} />
        </Block>
      )}
    </>
  );
}
