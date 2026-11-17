import { Fragment, lazy, Suspense, type ReactNode } from "react";
import { api } from "../../api/client";
import Trail from "../trail/Trail";
import type { AgentDetail, AgentRunner, AgentSummary } from "../../api/types/agents";
import { useRefreshKey, useRoute } from "../../lib/appContext";
import { byNewest, timeAgoInWords } from "../../lib/dates";
import { BOARD_HREF, openPipeline, openRole, routeFor, serializeRoute, type AgentTabKey, type Route } from "../../lib/route";
import { useAction } from "../../lib/useAction";
import { useResource } from "../../lib/useResource";
import { Block, Rail, RailEntry, RailGroup } from "../focus/parts";
import EmptyState from "../primitives/EmptyState";
import ErrorBanner from "../primitives/ErrorBanner";
import Loading from "../primitives/Loading";
import StateWords from "../primitives/StateWords";
import AgentSettings from "./AgentSettings";
import MailLogin from "./MailLogin";
import CostNights from "./CostNights";
import HowTheyWork from "./HowTheyWork";
import { lastSeenWords, nowInk, ribbonRole, rolesWords, slotsWords, standing, statusLine, stuckWords } from "./agentWords";
import AgentCrew from "../primitives/AgentCrew";
import AgentKinds from "./AgentKinds";

// A mission draws its playbook with the process view, so it loads the way How they work does. It is a part of this screen.
const MissionsScreen = lazy(() => import("../missions/MissionsScreen"));

/** Which agent opens by default: the one working right now, else whoever checked in most recently -- never just
 * the roster's own first row. */
function defaultAgentId(list: AgentSummary[]): string | null {
  if (list.length === 0) return null;
  const working = list.find((a) => a.status === "working");
  if (working) return working.connection_id;
  const byRecency = [...list].sort((a, b) => byNewest(a.last_seen_at, b.last_seen_at));
  return byRecency[0].connection_id;
}

/** An agent that runs several runners at once (an install with lanes) lists them: one line each, which runner, what it is
 * doing and since when. An agent with one runner has none. */
function sessionsWords(runners: AgentRunner[]): string {
  return `${runners.filter((r) => r.state === "working").length} of ${runners.length} sessions working`;
}

function hasRunners(agent: AgentSummary): boolean {
  return agent.runners.length > 1;
}

/** One agent in the roster: its crew of robots, where it stands, how many slots are busy and when it last called. The
 * whole row opens the agent. Under it, each runner it has. */
function RosterRow({ agent, selected, onSelect }: { agent: AgentSummary; selected: boolean; onSelect: () => void }) {
  const stand = standing(agent);
  const slots = slotsWords(agent);
  return (
    <li className="agent-roster-item" data-role={ribbonRole(agent)}>
      <button type="button" className={`agent-roster-row${selected ? " is-current" : ""}`} aria-current={selected ? "true" : undefined} onClick={onSelect}>
        <span className="agent-roster-agent">
          <AgentCrew agent={agent} size={32} className="agent-avatar" />
          <span className="agent-roster-info">
            <span className="agent-roster-name">{agent.name}</span>
            <span className="agent-roster-kind">
              <StateWords words={rolesWords(agent)} />
              {agent.model_words && <span className="fi-note agent-roster-model">{agent.model_words}</span>}
            </span>
          </span>
        </span>
        <span className="agent-roster-status">
          <StateWords words={stand.word} ink={stand.ink} />
          {agent.current && <span className="agent-roster-doing">{agent.current.project_title}</span>}
        </span>
        <span className="agent-roster-slots">{slots}</span>
        <span className="agent-roster-seen">{lastSeenWords(agent)}</span>
      </button>
      {hasRunners(agent) && <p className="agent-roster-sessions fi-note">{sessionsWords(agent.runners)}</p>}
    </li>
  );
}

/** The agents, one row each. */
function Roster({ agents, selected, onSelect }: { agents: AgentSummary[]; selected: string | null; onSelect: (id: string) => void }) {
  return (
    <Block id="agents-roster" title="Roster" count={agents.length} flush>
      {agents.length === 0 ? (
        <EmptyState>No agents yet.</EmptyState>
      ) : (
        <ul className="agent-roster" aria-label="Agents">
          <li className="agent-roster-cols" aria-hidden="true">
            <span>Agent</span>
            <span>Status</span>
            <span>Slots busy</span>
            <span>Last seen</span>
          </li>
          {agents.map((a) => (
            <RosterRow key={a.connection_id} agent={a} selected={a.connection_id === selected} onSelect={() => onSelect(a.connection_id)} />
          ))}
        </ul>
      )}
    </Block>
  );
}

/** What the agent is doing now: its task and how long, where it stands and how busy it is. The dot before its name says it
 * at a glance: teal when it works, sun paused, red stuck or gone quiet, grey asleep. An agent with several runners lists each
 * one's session instead of the one task; a task held with no sign of life is still told first. */
function DoingNow({ agent }: { agent: AgentDetail }) {
  const status = statusLine(agent);
  const capacity = agent.capacity;
  const several = hasRunners(agent);
  const board = (
    <a className="fi-btn" href={BOARD_HREF}>
      Open the board
    </a>
  );
  return (
    <Block id="agent-now" level={3} ink={nowInk(agent)} title="Doing now" flush>
      <div>
        {agent.current && (!several || agent.current.stuck_since) ? (
          <div className="agent-now agent-now-row">
            <div className="agent-now-body">
              <div className="agent-now-title">{agent.current.task_title}</div>
              <div className="agent-now-when">
                {agent.current.project_title} ·{" "}
                {agent.current.stuck_since
                  ? stuckWords(agent.current.stuck_since)
                  : `started ${timeAgoInWords(agent.current.started_at)}`}
              </div>
            </div>
            {board}
          </div>
        ) : !several ? (
          <p className="agent-now-row agent-idle">Nothing right now.</p>
        ) : null}
        {several && (
          <div className="agent-now agent-now-row">
            <div className="agent-now-body">
              <div className="agent-now-title">{sessionsWords(agent.runners)}</div>
            </div>
            {agent.current && !agent.current.stuck_since && board}
          </div>
        )}
        <div className="agent-now-row agent-state">
          <span className={status.problem ? "agent-status problem" : "agent-status"}>{status.text}</span>
          {capacity && (
            <span>
              <span className={capacity.free ? "agent-capacity-free" : "agent-capacity-busy"}>{capacity.words}</span>
            </span>
          )}
        </div>
      </div>
    </Block>
  );
}

type Tab = AgentTabKey;

function AgentPanel({
  agent,
  onOpenSent,
  onChanged,
  tab,
}: {
  agent: AgentDetail;
  /** "What was sent" on a line of the activity: opens that search's payload on How they work. */
  onOpenSent?: (runId: string) => void;
  onChanged: () => void;
  tab: Tab;
}) {
  const { run: setPaused, busy, error } = useAction(
    async (paused: boolean) => {
      await api.editAgent(agent.connection_id, { paused });
      onChanged();
    },
    { fallback: "That didn't go through." },
  );

  // An agent that does several kinds of work: each kind under its own short label, said once; what none of them
  // does is the one line under them. One kind needs no label.
  const kinds = (agent.may_do.rules ?? []).length > 1 ? agent.may_do.rules : [];
  return (
    <div className="agent-panel" data-role={ribbonRole(agent)}>
      <div className="agent-panel-head">
        <AgentCrew agent={agent} size={48} className="agent-avatar" />
        <div className="agent-panel-head-info">
          <h2 className="agent-panel-name">{agent.name}</h2>
          <p className="agent-for">{agent.description || "No purpose written yet."}</p>
          {kinds.length > 0 && (
            <dl className="agent-kinds" aria-label="Its kinds of work">
              {kinds.map((k) => (
                <Fragment key={k.kind}>
                  <dt>{k.title}</dt>
                  <dd>{k.rule}</dd>
                </Fragment>
              ))}
            </dl>
          )}
          <p className="agent-may-do">{agent.may_do.summary}</p>
        </div>
        <button type="button" className={agent.paused ? "fi-btn fi-btn-go" : "fi-btn"} disabled={busy} onClick={() => setPaused(!agent.paused)}>
          {agent.paused ? "Resume" : "Pause"}
        </button>
      </div>
      {agent.paused && <p className="agent-paused-note">Paused: it won't be handed any work until you resume it.</p>}
      {error && <ErrorBanner as="p">{error}</ErrorBanner>}

      <DoingNow agent={agent} />

      {/* The rail names the part, so the part has no heading of its own: one heading layer, not two. */}
      {tab === "activity" && (
        <section id="agent-activity" className="fi-block" aria-label="Activity">
          <Trail
            load={(opts) => api.agentTrail(agent.connection_id, opts)}
            loadKey={`${agent.connection_id}-${agent.activity.length}-${agent.activity[0]?.at ?? ""}`}
            filters
            filterLabel="Filter what this agent did"
            empty="Nothing yet."
            onOpenSearch={openPipeline}
            onOpenRole={openRole}
            onOpenSent={onOpenSent}
          />
        </section>
      )}

      {tab === "settings" && (
        <section id="agent-settings" className="fi-block" aria-label="Settings">
          <AgentSettings agent={agent} />
        </section>
      )}
    </div>
  );
}

/** What the first two parts say they are, under their name. How they work and Missions say theirs themselves. */
const AGENTS_ABOUT = "The agents working on your job search, and what they're doing.";

/** The Agent Settings screen: the agents working on the job search, and for each what it's for, when it last checked in,
 * what it may do and a Pause button, under the rail's parts: Activity and Settings. Beside them, "How they work"
 * shows the steps the agents follow and exactly what each is sent, and "Missions" holds the work that runs by itself.
 * A Focus screen: the rail lists the four parts and the pane holds the one open. Which agent, which of its parts and which
 * of these pages is open is the address's. */
export default function AgentsScreen() {
  const refreshKey = useRefreshKey();
  const { route, go } = useRoute();
  const missionsOpen = route.tab === "missions";
  const how = route.agentsHow;

  const {
    data: agents,
    error,
    reload,
  } = useResource(() => api.listAgents(), [refreshKey], {
    enabled: !missionsOpen, // the Missions part does not need the roster
    liveArea: "agents",
    fallback: "Couldn't load your agents.",
  });

  // The address says which agent is open; one that is gone falls back to the default.
  const selected: string | null =
    route.agentId && agents?.some((a) => a.connection_id === route.agentId) ? route.agentId : agents ? defaultAgentId(agents) : null;
  const tab: Tab = route.agentTab ?? "activity";

  // Best effort: a failed load leaves the panel on its last known state. No agent open: nothing to show.
  const { data: detail, reload: reloadDetail } = useResource(
    () => (selected ? api.getAgent(selected) : Promise.resolve(null)),
    [selected, refreshKey],
    { enabled: !missionsOpen },
  );

  // The rail's four parts: the open agent's Activity and Settings, How they work and Missions.
  const agentRoute = (agentTab: Tab | null) => routeFor("agents", { agentId: selected, agentTab });
  const agentsOpen = !how && !missionsOpen;
  const parts: { key: string; label: string; to: Route; current: boolean }[] = [
    { key: "activity", label: "Activity", to: agentRoute("activity"), current: agentsOpen && tab === "activity" },
    { key: "settings", label: "Settings", to: agentRoute("settings"), current: agentsOpen && tab === "settings" },
    { key: "how", label: "How they work", to: routeFor("agents", { agentsHow: true }), current: how && !missionsOpen },
    { key: "missions", label: "Missions", to: routeFor("missions"), current: missionsOpen },
  ];
  const current = parts.find((p) => p.current) ?? parts[0];
  const frame = (body: ReactNode, about?: string) => (
    <div className="fi fi-ag">
      <Rail label="Agent Settings">
        <RailGroup>
          {parts.map((p) => (
            <RailEntry key={p.key} label={p.label} current={p.current} href={serializeRoute(p.to)} onPick={() => go(p.to)} />
          ))}
        </RailGroup>
      </Rail>
      <div className="fi-pane">
        <div className="fi-pane-in" key={agentsOpen ? "agents" : current.key}>
          <h1 className="fi-vh">Agent Settings</h1>
          <p className="fi-ag-title">{current.label}</p>
          {about && <p className="fi-head-meta fi-ag-about">{about}</p>}
          {body}
        </div>
      </div>
    </div>
  );

  // The Missions part: its own page, as it was, under this screen's rail.
  if (missionsOpen) {
    return frame(
      <Suspense fallback={<Loading>Loading&hellip;</Loading>}>
        <MissionsScreen />
      </Suspense>,
    );
  }

  if (error) return frame(<ErrorBanner>{error}</ErrorBanner>);
  if (!agents) return frame(<Loading>Loading agents&hellip;</Loading>);

  const open = detail && detail.connection_id === selected ? detail : null;

  return frame(
    how ? (
      <HowTheyWork />
    ) : (
      <>
        <AgentKinds />
        <Roster
          agents={agents}
          selected={selected}
          onSelect={(id) => go(routeFor("agents", { agentId: id, agentTab: tab === "settings" ? "settings" : null }))}
        />

        {open ? (
          <AgentPanel
            key={open.connection_id}
            agent={open}
            onOpenSent={(runId) => go(routeFor("agents", { agentsHow: true, sentTaskId: runId }))}
            tab={tab}
            onChanged={() => {
              reload();
              reloadDetail();
            }}
          />
        ) : (
          <p className="agent-panel-wait fi-note">{agents.length === 0 ? "Select an agent." : "Loading…"}</p>
        )}

        <CostNights />
        <MailLogin />
      </>
    ),
    how ? undefined : AGENTS_ABOUT,
  );
}
