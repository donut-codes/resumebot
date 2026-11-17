import type { ProcessRun, ProcessRunDetail } from "../../api/types/agents";
import { dateTimeInWords } from "../../lib/dates";
import { Block, type BlockInk } from "../focus/parts";

/** The dot before a run's name: teal while it runs or when it finished, sun while it waits, red when it failed. */
export function runInk(run: ProcessRun): BlockInk {
  switch (run.status) {
    case "running":
    case "completed":
      return "green";
    case "waiting":
      return "saffron";
    case "failed":
      return "oxblood";
    default:
      return "graphite";
  }
}

/** One line of runs to open: the name, where it is, and whether it only checked alongside. */
function RunLinks({ label, runs, onOpen }: { label: string; runs: ProcessRun[]; onOpen: (id: string) => void }) {
  if (runs.length === 0) return null;
  return (
    <p className="how-run-line how-run-links">
      {label}{" "}
      {runs.map((r, i) => (
        <span key={r.id}>
          {i > 0 ? ", " : ""}
          <button type="button" className="fi-link" onClick={() => onOpen(r.id)}>
            {r.name}
          </button>
          {` (${r.alongside ? "checking alongside, " : ""}${r.status_words.toLowerCase()})`}
        </span>
      ))}
    </p>
  );
}

/** Where one run is, above the process it is a run of: what state it is in, what it waits for, and its other runs.
 * A shadow run says plainly that it only checks alongside what the app did. */
export function RunHeader({
  run,
  onOpenRun,
  onLeave,
}: {
  run: ProcessRunDetail;
  onOpenRun: (id: string) => void;
  onLeave: () => void;
}) {
  return (
    <Block
      id="how-run"
      ink={runInk(run)}
      title="Where this is in its process"
      aside={
        <button type="button" className="fi-btn" onClick={onLeave}>
          See the process on its own
        </button>
      }
    >
      <div className="how-run">
        {run.alongside && (
          <p>
            <span className="fi-badge fi-state" data-ink="saffron">
              Checking alongside
            </span>
          </p>
        )}
        <p className="how-run-state">
          {run.status_words}
          {run.step_title ? (run.ended ? `: ended at “${run.step_title}”` : `, at “${run.step_title}”`) : ""}
          <span className="how-run-when">
            {" "}
            &middot; started {dateTimeInWords(run.started_at)}
            {run.ended_at ? `, ended ${dateTimeInWords(run.ended_at)}` : ""}
          </span>
        </p>
        {run.version != null && (
          <p className="how-run-line">
            It ran on version {run.version} of this process
            {run.version_now != null && run.version_now !== run.version
              ? `; version ${run.version_now} is in use now.`
              : "."}
          </p>
        )}
        {run.alongside && (
          <p className="how-run-line">
            This one only watches. The app&rsquo;s own code did the work; this run made the same choices beside it, and
            acted on nothing.
          </p>
        )}
        {run.waiting_for.map((w) => (
          <p key={w.step} className="how-run-line">
            Waiting for {w.words}.
          </p>
        ))}
        <RunLinks label="Inside it:" runs={run.parts} onOpen={onOpenRun} />
        <RunLinks label="Other runs:" runs={run.others} onOpen={onOpenRun} />
      </div>
    </Block>
  );
}
