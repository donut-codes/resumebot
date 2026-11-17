import { useState } from "react";
import { api } from "../../api/client";
import type { HarnessProcess } from "../../api/types/agents";
import { useRoute } from "../../lib/appContext";
import { routeFor, serializeRoute, type Route } from "../../lib/route";
import { useResource } from "../../lib/useResource";
import { PillRow } from "../focus/parts";
import ErrorBanner from "../primitives/ErrorBanner";
import Loading from "../primitives/Loading";
import { ProcessTab } from "./ProcessTab";
import { chainFor } from "./processChain";
import { SentForTask } from "./SentBlock";

interface Props {
  /** Show this one process alone (a mission's playbook): no page intro and no tabs for the other processes, and the
   * process tab open is the component's own, not the address's. */
  only?: string;
}

/** One row of tabs: the top-level processes, or the sub-processes under the one open. On the page each is a link to its own
 * address; alone (a mission's playbook) they are buttons that swap the process in place. */
function TabRow({
  label,
  items,
  activeId,
  sub = false,
  toOf,
  onPick,
}: {
  label: string;
  items: HarnessProcess[];
  activeId: string | undefined;
  sub?: boolean;
  /** Where picking a process goes, on the page. */
  toOf?: (p: HarnessProcess) => Route;
  onPick: (p: HarnessProcess) => void;
}) {
  const { go } = useRoute();
  return (
    <PillRow
      label={label}
      sub={sub}
      tabs={items.map((p) => ({
        key: p.id,
        label: p.name,
        current: p.id === activeId,
        href: toOf ? serializeRoute(toOf(p)) : undefined,
        onPick: () => (toOf ? go(toOf(p)) : onPick(p)),
      }))}
    />
  );
}

/** The Agents page's "How they work" tab: the top-level processes (a search for roles) are the tabs; the
 * one open lists the sub-processes it calls beneath it as child tabs; each is drawn as every process is, and at
 * each step what the agent is sent. */
export default function HowTheyWork({ only }: Props) {
  const { route, go: goRoute } = useRoute();
  // On the Agent Settings page the address holds what is open: the process tab, one search's payload, a running process.
  const routed = !only;
  const sentTaskId = routed ? route.sentTaskId : null;
  const liveRunId = routed ? route.liveRunId : null;
  /** The address of the process tabs from the top down to these (and the search whose payload is open, still open). */
  const pathAddress = (to: string[]) => routeFor("agents", { agentsHow: true, howPath: to, sentTaskId: route.sentTaskId });
  const [localPath, setLocalPath] = useState<string[]>([]);
  const { data, error, reload } = useResource(() => api.harness(), [], {
    fallback: "Couldn't load how the agents work.",
  });
  // A running process opened from the Pipeline: read again when the server says the harness changed.
  const { data: run, error: runError } = useResource(
    () => api.processRun(liveRunId ?? ""),
    [liveRunId],
    { fallback: "Couldn't load where this is in its process.", enabled: !!liveRunId, liveArea: "harness", reset: true },
  );
  if (error) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <Loading>Loading&hellip;</Loading>;

  const all = data.processes;
  const byId = new Map(all.map((p) => [p.id, p]));
  const childrenOf = (p: HarnessProcess) =>
    p.children.map((id) => byId.get(id)).filter((c): c is HarnessProcess => !!c && c.id !== p.id);
  const tops = only ? all.filter((p) => p.slug === only) : all.filter((p) => p.top_level);
  const slugs = (to: HarnessProcess[]) => to.map((p) => p.slug);
  const go = (to: HarnessProcess[]) => (routed ? goRoute(pathAddress(slugs(to))) : setLocalPath(slugs(to)));
  // The tabs from the top down to a process (the first way found), for a link to a process from elsewhere.
  const pathTo = (target: HarnessProcess, from: HarnessProcess[] = tops, seen: string[] = []): HarnessProcess[] | null => {
    for (const p of from) {
      if (p.id === target.id) return [p];
      if (seen.includes(p.id)) continue;
      const below = pathTo(target, childrenOf(p), [...seen, p.id]);
      if (below) return [p, ...below];
    }
    return null;
  };
  // A run opens on the tabs of its own process, whatever the address says after it.
  const runProcess = liveRunId && run ? byId.get(run.process) : undefined;
  const runTabs = runProcess ? pathTo(runProcess) : null;
  const asked = routed ? route.howPath : localPath;
  const chain = runTabs ?? chainFor(all, only && asked[0] !== only ? [only] : asked);
  const process = chain[chain.length - 1];
  const calledFrom = process
    ? all
        .filter((p) => p.children.includes(process.id))
        .map((parent) => ({ process: parent, open: () => go([...(pathTo(parent) ?? [parent]), process]) }))
    : [];
  // A process alone (a mission's playbook) has no page around it; a run waiting to load shows no process yet.
  const showing = !!process && !(liveRunId && !run && !runError);
  const openRun = runTabs && process && run?.process === process.id ? run : null;
  return (
    <div className="how-they-work">
      {!only && (
        <p className="fi-head-meta fi-ag-about">
          The steps your agents follow, and exactly what each agent is sent at each one. It is read from the same files
          the agents get, so it is never out of date.
        </p>
      )}
      {sentTaskId && (
        <SentForTask taskId={sentTaskId} overview={data} onSeeAll={() => goRoute(routeFor("agents", { agentsHow: true }))} />
      )}
      {liveRunId && runError && <ErrorBanner>{runError}</ErrorBanner>}
      {liveRunId && !run && !runError && <Loading>Loading where this is&hellip;</Loading>}
      {!only && (
        <TabRow label="Which process" items={tops} activeId={chain[0]?.id} toOf={(p) => pathAddress([p.slug])} onPick={(p) => go([p])} />
      )}
      {chain.map(
        (node, i) =>
          node.children.length > 0 && (
            <TabRow
              key={node.id}
              sub
              label={`Sub-processes of ${node.name}`}
              items={childrenOf(node)}
              activeId={chain[i + 1]?.id}
              // Picking the open sub-process again closes it, back to the process it is under.
              toOf={routed ? (p) => pathAddress(slugs(p.id === chain[i + 1]?.id ? chain.slice(0, i + 1) : [...chain.slice(0, i + 1), p])) : undefined}
              onPick={(p) => go(p.id === chain[i + 1]?.id ? chain.slice(0, i + 1) : [...chain.slice(0, i + 1), p])}
            />
          ),
      )}
      {process && showing && (
        <ProcessTab
          key={process.id}
          process={process}
          sectioned={!only}
          onChanged={reload}
          run={openRun}
          onOpenRun={(id) => goRoute(routeFor("agents", { agentsHow: true, liveRunId: id }))}
          onLeaveRun={() => goRoute(pathAddress(slugs(chain)))}
          childProcesses={childrenOf(process)}
          calledFrom={calledFrom}
          onOpen={(p) => go([...chain, p])}
        />
      )}
    </div>
  );
}
