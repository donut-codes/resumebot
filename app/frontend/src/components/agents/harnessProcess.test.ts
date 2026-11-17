import type { HarnessProcess, HarnessStep } from "../../api/types/agents";
import { harnessToProcess, START_ID } from "./harnessProcess";

function step(id: string, overrides: Partial<HarnessStep> = {}): HarnessStep {
  return {
    id, harness_id: null, title: id, type: "task", who: "Specialist", who_about: "", role: null, actor: "", trigger: "",
    fired_by: "", enters_at: "", preconditions: "", inputs: "", rules: "", outputs: "", exceptions: "",
    owner_sees: "", source: "", criteria: null, trigger_kind: null, trigger_detail: null,
    trigger_subtype: null, calls: null, next: [], sent: null, ...overrides,
  };
}

const ROUND: HarnessProcess = {
  id: "jobsearch_round",
  slug: "jobsearch_round",
  top_level: true,
  children: [],
  name: "Search for roles",
  about: "",
  steps: [
    step("a", { next: [{ to: "q", label: "" }] }),
    step("q", { type: "decision", criteria: "Finish when nothing is off plan.", next: [{ to: "a", label: "Not yet: again" }, { to: "done", label: "All good" }, { to: "q", label: "" }] }),
    step("done", { type: "end" }),
  ],
};

const WITH_STARTS: HarnessProcess = {
  id: "jobsearch_other",
  slug: "jobsearch_other",
  top_level: true,
  children: [],
  name: "Search with two starts",
  about: "",
  steps: [
    step("ST-1", {
      type: "start", who: "You", trigger_kind: "message", trigger_detail: "A search starts",
      next: [{ to: "a", label: "" }],
    }),
    step("ST-6", {
      type: "start", who: "Automatic", trigger_kind: "timer", trigger_detail: "R/PT1M",
      trigger_subtype: "cycle", next: [{ to: "a", label: "" }],
    }),
    step("a", { next: [{ to: "done", label: "" }] }),
    step("done", { type: "end" }),
  ],
};

describe("harnessToProcess", () => {
  it("gives a process with no start event of its own one, into the first step, and draws a question as an exclusive gateway", () => {
    const { steps, edges } = harnessToProcess(ROUND);
    expect(steps.map((s) => [s.id, s.step_type])).toEqual([
      [START_ID, "event_start"], ["a", "task"], ["q", "gateway_exclusive"], ["done", "event_end"],
    ]);
    expect(edges[0]).toMatchObject({ source_step_id: START_ID, target_step_id: "a" });
  });

  it("labels the answers, keeps a loop back to an earlier step and drops a step's loop to itself", () => {
    const { edges } = harnessToProcess(ROUND);
    const from = (id: string) => edges.filter((e) => e.source_step_id === id).map((e) => [e.target_step_id, e.label]);
    expect(from("q")).toEqual([["a", "Not yet: again"], ["done", "All good"]]);
  });

  it("carries a question's conditions as its decision criteria", () => {
    const { steps } = harnessToProcess(ROUND);
    expect(steps.find((s) => s.id === "q")?.decision_criteria).toBe("Finish when nothing is off plan.");
  });

  it("shows a question's conditions without code marks", () => {
    const q = step("q", { type: "decision", criteria: "Stop when `_can_run` fails." });
    expect(harnessToProcess({ ...ROUND, steps: [q] }).steps.find((s) => s.id === "q")?.decision_criteria).toBe("Stop when _can_run fails.");
  });

  it("draws each start event as a start with what fires it, and adds no start of its own", () => {
    const { steps, edges } = harnessToProcess(WITH_STARTS);
    expect(steps.map((s) => [s.id, s.step_type])).toEqual([
      ["ST-1", "event_start"], ["ST-6", "event_start"], ["a", "task"], ["done", "event_end"],
    ]);
    // a message start says what fires it in its own Trigger; only a schedule is drawn as one
    expect(steps[0].event_trigger ?? null).toBeNull();
    expect(steps[0].event_trigger_detail ?? null).toBeNull();
    expect(steps[1]).toMatchObject({ event_trigger: "timer", event_trigger_detail: "R/PT1M", event_trigger_subtype: "cycle" });
    expect(edges.map((e) => [e.source_step_id, e.target_step_id])).toEqual([["ST-1", "a"], ["ST-6", "a"], ["a", "done"]]);
  });

  it("names the actor (the lane) of every step as its Who", () => {
    const { steps } = harnessToProcess(WITH_STARTS);
    expect(steps.find((s) => s.id === "a")?.performer).toBe("Specialist");
    expect(steps.find((s) => s.id === "ST-1")?.performer).toBe("You");
    expect(steps.find((s) => s.id === "ST-6")?.performer).toBe("Automatic");
  });

  it("draws a call activity as the view's sub-process, marked as a call to another process", () => {
    const { steps } = harnessToProcess({ ...ROUND, steps: [step("login", { type: "subprocess", calls: "get_a_login" }), step("a")] });
    expect(steps.find((s) => s.id === "login")).toMatchObject({ step_type: "subprocess", bpmn_type: "bpmn:callActivity" });
    expect(steps.find((s) => s.id === "a")?.bpmn_type).toBeUndefined();
  });
});
