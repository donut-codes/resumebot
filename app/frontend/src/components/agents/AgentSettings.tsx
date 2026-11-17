import type { AgentDetail } from "../../api/types/agents";
import { dateTimeInWords } from "../../lib/dates";
import { rolesWords } from "./agentWords";

/** The agent's Settings tab: what it is and what it may do, as plain facts. Nothing here is typed in; the agent
 * says what it is, and Pause and Resume (at the top of the page) is the one control. */
export default function AgentSettings({ agent }: { agent: AgentDetail }) {
  const rules = agent.may_do.rules ?? [];
  return (
    <div className="agent-manage fi-block-body is-flush">
      <dl className="agent-facts">
        <dt>Name</dt>
        <dd>{agent.name}</dd>
        <dt>What it's for</dt>
        <dd>{agent.description || "No purpose written yet."}</dd>
        <dt>Role</dt>
        <dd>{rolesWords(agent)}</dd>
        {agent.model_words && (
          <>
            <dt>Model</dt>
            <dd>{agent.model_words}</dd>
          </>
        )}
        {agent.declared_at && (
          <>
            <dt>First checked in</dt>
            <dd>{dateTimeInWords(agent.declared_at)}</dd>
          </>
        )}
        <dt>What it may do</dt>
        <dd>
          <p>{agent.may_do.summary}</p>
          {rules.length > 0 && (
            <ul className="agent-rules">
              {rules.map((r) => (
                <li key={r.kind}>
                  <strong>{r.title}</strong>
                  <span className="fi-note"> &middot; {r.rule}</span>
                </li>
              ))}
            </ul>
          )}
        </dd>
      </dl>
    </div>
  );
}
