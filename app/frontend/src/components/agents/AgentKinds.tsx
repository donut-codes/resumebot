import { useState } from "react";
import { api } from "../../api/client";
import { useRefreshKey } from "../../lib/appContext";
import { routeFor, serializeRoute } from "../../lib/route";
import { useAction } from "../../lib/useAction";
import { useResource } from "../../lib/useResource";
import { Block } from "../focus/parts";
import ErrorBanner from "../primitives/ErrorBanner";
import StateWords from "../primitives/StateWords";

/** The app's own browser worker: listed only while it holds an item, with no pause, Run now or queue. */
export const CODE_CHECKER = "code_checker";

const HOW_THEY_WORK = serializeRoute(routeFor("agents", { agentsHow: true }));

function waitingWords(n: number): string {
  return n === 0 ? "Nothing waiting" : `${n} waiting`;
}

/** The agents by kind, as the pool lists them (the page's order, each with its name and what it is for): Run all and Pause
 * all over one row per kind, each with what it is doing, what waits for it, its Run now and Pause buttons and a link to
 * How they work, where its instructions are read and edited. */
export default function AgentKinds() {
  const refreshKey = useRefreshKey();
  const { data: kinds, reload } = useResource(() => api.listAgentKinds(), [refreshKey], {
    liveArea: "agents",
    fallback: "Couldn't load the agents.",
  });
  /** Why Run now queued nothing, by the kind it was pressed for ("all" for Run all); a kind that queued is not here. */
  const [held, setHeld] = useState<Record<string, string>>({});
  const { run, busy, error } = useAction(
    async (kind: string, what: "run" | "pause" | "resume") => {
      if (what === "run") {
        const out = kind === "all" ? await api.runAllAgentKinds() : await api.runAgentKind(kind);
        setHeld((h) => {
          const rest = { ...h };
          delete rest[kind];
          return out.held ? { ...rest, [kind]: out.held } : rest;
        });
      } else {
        await api.agentKindAction(kind, what);
      }
      reload();
    },
    { fallback: "That didn't go through." },
  );
  const { data: sessions, reload: reloadSessions } = useResource(() => api.agentSessions(), [refreshKey], {
    fallback: "Couldn't load how many sessions may work.",
  });
  const setSessions = useAction(
    async (change: { count?: number; budget?: number | null }) => {
      await api.setAgentSessions(change);
      reloadSessions();
    },
    { fallback: "That didn't go through." },
  );
  if (!kinds || kinds.length === 0) return null;
  const allPaused = kinds.filter((k) => k.kind !== CODE_CHECKER).every((k) => k.paused);
  return (
    <Block id="agents-kinds" title="Agents" count={kinds.length} flush>
      <div className="agent-kinds-all">
        <button type="button" className="fi-btn fi-btn-go" disabled={busy} onClick={() => run("all", "run")}>
          Run all
        </button>
        <button type="button" className="fi-btn" disabled={busy} onClick={() => run("all", allPaused ? "resume" : "pause")}>
          {allPaused ? "Resume all" : "Pause all"}
        </button>
        {sessions && (
          <label className="agent-sessions-limit">
            <span className="fi-label">Sessions at once (daytime)</span>
            <select
              className="fi-input"
              value={sessions.count}
              disabled={setSessions.busy}
              onChange={(e) => setSessions.run({ count: Number(e.target.value) })}
            >
              {Array.from({ length: sessions.max }, (_, i) => i + 1).map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
            <span className="fi-note">Your off hours use all {sessions.max}.</span>
          </label>
        )}
        {sessions && sessions.budget !== undefined && (
          <label className="agent-sessions-limit">
            <span className="fi-label">Nightly budget ($, at API prices)</span>
            <input
              className="fi-input"
              type="number"
              min={0}
              step={1}
              key={sessions.budget}
              defaultValue={sessions.budget ?? ""}
              disabled={setSessions.busy}
              aria-label="Nightly budget"
              onBlur={(e) => {
                const v = e.target.value.trim() === "" ? null : Number(e.target.value);
                if (v === null ? sessions.budget !== null : v >= 0 && v !== sessions.budget) setSessions.run({ budget: v });
              }}
            />
          </label>
        )}
      </div>
      {setSessions.error && <ErrorBanner as="p">{setSessions.error}</ErrorBanner>}
      {sessions?.budget_line && <p className="agent-kind-held fi-note">{sessions.budget_line}</p>}
      {held.all && <p className="agent-kind-held fi-note">Held: {held.all}</p>}
      {error && <ErrorBanner as="p">{error}</ErrorBanner>}
      <ul className="agent-kind-rows" aria-label="Agents by kind">
        {kinds.map((k) => (
          <li key={k.kind} className="agent-kind-row" data-kind={k.kind}>
            <div className="agent-kind-head">
              <span className="agent-kind-name">{k.label}</span>
              {k.paused && <StateWords words="Paused" />}
            </div>
            {k.purpose && <p className="agent-kind-purpose">{k.purpose}</p>}
            <div className="agent-kind-doing">
              {k.doing.length === 0 ? (
                <span className="fi-note">Idle</span>
              ) : (
                <ul className="agent-kind-tasks" aria-label={`${k.label} at work`}>
                  {k.doing.map((d) => (
                    <li key={d.session}>{d.task}</li>
                  ))}
                </ul>
              )}
              {k.kind !== CODE_CHECKER && <span className="fi-note">{waitingWords(k.waiting)}</span>}
            </div>
            {k.kind !== CODE_CHECKER && (
              <div className="agent-kind-buttons">
                <button type="button" className="fi-btn" disabled={busy} onClick={() => run(k.kind, "run")}>
                  Run now
                </button>
                <button type="button" className="fi-btn" disabled={busy} onClick={() => run(k.kind, k.paused ? "resume" : "pause")}>
                  {k.paused ? "Resume" : "Pause"}
                </button>
              </div>
            )}
            {k.health && <p className="agent-kind-health fi-note">{k.health}</p>}
            {held[k.kind] && <p className="agent-kind-held fi-note">Held: {held[k.kind]}</p>}
            {k.instructions.length > 0 && (
              <ul className="agent-kind-files" aria-label={`${k.label} instructions`}>
                <li><a href={HOW_THEY_WORK}>How the agents work</a></li>
              </ul>
            )}
          </li>
        ))}
      </ul>
    </Block>
  );
}
