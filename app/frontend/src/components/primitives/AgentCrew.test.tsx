import { render } from "@testing-library/react";
import type { AgentSummary } from "../../api/types/agents";
import AgentCrew from "./AgentCrew";

const RUNNER: AgentSummary = {
  connection_id: "a1", name: "Runner", description: null, status: "working", paused: false, roles: ["scout", "writer", "analyst"], // as stored: drawn in the order the work goes
  roles_words: "Scout, Writer, Analyst", availability: "On a task", session_state: "live", checked_in_at: null, last_seen_at: null, declared_at: null,
  model_words: null, capacity: { free: false, busy: 1, slots: 1, words: "Busy: 1 of 1 slots", since: null }, runners: [],
  current: { task_title: "Researching a role", project_title: "Job search", started_at: "x", role: "analyst" },
};

describe("AgentCrew", () => {
  it("draws resumebot in each disguise the agent holds, and lights only the one whose turn it is", () => {
    const { container } = render(<AgentCrew agent={RUNNER} />);
    const bots = [...container.querySelectorAll("svg")];
    expect(bots).toHaveLength(3);
    expect(bots.map((b) => b.classList.contains("is-active"))).toEqual([false, true, false]);
    expect(bots.filter((b) => b.classList.contains("bot-asleep"))).toHaveLength(2);
  });

  it("an agent with none of the known roles is the plain robot", () => {
    const { container } = render(<AgentCrew agent={{ ...RUNNER, roles: ["planner"], status: "idle", current: null }} />);
    expect(container.querySelectorAll("svg")).toHaveLength(1);
  });
});
