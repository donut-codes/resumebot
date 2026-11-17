import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { ProcessRunDetail } from "../../api/types/agents";
import { json, render, stubFetch } from "../../test-utils";
import { howProcessRoutes } from "./howBpmn.fixture";
import { EXAMPLE, OVERVIEW, RUN } from "./how.fixture";
import HowTheyWork from "./HowTheyWork";
import { fakeLive, pollTimers } from "../../test-live";

function stub(run: ProcessRunDetail | null = RUN) {
  const urls: string[] = [];
  stubFetch((input: RequestInfo | URL) => {
    const url = String(input);
    urls.push(url);
    if (url.includes("/harness/runs/")) return run ? json(run) : json({ detail: "no such run" }, 404);
    const own = howProcessRoutes(url); // the process on its own, once the run is left, asks for its file and versions
    if (own) return own;
    if (url.includes("/harness/tasks/")) {
      return json({ process: "jobsearch_round", step: OVERVIEW.processes[0].steps[2], example: EXAMPLE });
    }
    if (url.includes("/harness")) return json(OVERVIEW);
    return json({}, 404);
  });
  return urls;
}

describe("HowTheyWork, a running process", () => {
  it("marks the step the run is at and the steps it passed", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how?live=i1" });
    expect(await screen.findByRole("heading", { name: "Where this is in its process" })).toBeInTheDocument();
    expect(screen.queryByText("Checking alongside")).toBeNull();
    expect(await screen.findByText(/Waiting at a step, at .The scout searches the boards./)).toBeInTheDocument();
    expect(screen.getByText("Waiting for the scout's report.")).toBeInTheDocument();
    // The ported process view draws it, with the run's steps marked on the list.
    expect(await screen.findByRole("heading", { level: 2, name: "Process flow" })).toBeInTheDocument();
    const mark = (id: string) => document.getElementById(`step-${id}`)!.querySelector(".step-run-mark")?.textContent ?? null;
    expect(mark("Scout_searches")).toBe("Here now");
    expect(mark("Start_round")).toBe("Passed");
    expect(mark("Open_run")).toBe("Passed");
    expect(mark("E-done")).toBeNull();
    // Two nodes of the graph wear the passed mark and one the current one.
    expect(document.querySelectorAll(".pw-block.pw-run-current")).toHaveLength(1);
    expect(document.querySelectorAll(".pw-block.pw-run-passed")).toHaveLength(2);
  });

  it("says a shadow run only checks alongside", async () => {
    stub({ ...RUN, alongside: true });
    render(<HowTheyWork />, { hash: "#/agents/how?live=i1" });
    expect(await screen.findByText("Checking alongside")).toBeInTheDocument();
    expect(screen.getByText(/This one only watches/)).toBeInTheDocument();
  });

  it("puts each mark in the same place on every row: in the right-hand tags, just left of the kind tag", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how?live=i1" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    for (const id of ["Start_round", "Open_run", "Scout_searches"]) {
      const mark = document.getElementById(`step-${id}`)!.querySelector(".step-run-mark")!;
      expect(mark.parentElement!.className).toContain("step-title-tags-run");
      expect(mark.nextElementSibling!.className).toContain("step-bpmn-badge");
      expect(mark.parentElement!.parentElement!.className).toBe("step-title-row");
    }
  });

  it("draws the process only once the run is loaded, so the graph is fitted once and stays put", async () => {
    stubFetch((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/harness/runs/")) return new Promise<Response>(() => {});
      return json(OVERVIEW);
    });
    render(<HowTheyWork />, { hash: "#/agents/how?live=i1" });
    await screen.findByRole("link", { name: "Search for roles" });
    expect(screen.getByText(/Loading where this is/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 2, name: "Process flow" })).toBeNull();
  });

  it("shows What was sent for an agent step it reached, opening the kept copy", async () => {
    const urls = stub();
    render(<HowTheyWork />, { hash: "#/agents/how?live=i1" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    // The step the run is at is open already.
    const row = document.getElementById("step-Scout_searches")!;
    fireEvent.click(await within(row).findByRole("button", { name: /Show what was sent to The scout/ }));
    expect(await within(row).findByText(/as sent to the scout/i)).toBeInTheDocument();
    expect(urls.some((u) => u.includes("/harness/tasks/r1"))).toBe(true);
  });

  it("opens another run of the same search, and leaves the run for the process on its own", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how?live=i1" });
    // The other run of this search is a link in the run's own block.
    const band = await screen.findByRole("region", { name: "Where this is in its process" });
    fireEvent.click(within(band).getByRole("button", { name: "Search for roles" }));
    await waitFor(() => expect(window.location.hash).toBe("#/agents/how?live=i0"));
    fireEvent.click(screen.getByRole("button", { name: "See the process on its own" }));
    await waitFor(() => expect(window.location.hash).toBe("#/agents/how/jobsearch_round"));
  });

  it("says a run that has ended ended there", async () => {
    stub({
      ...RUN, status: "completed", status_words: "Finished", ended: true, current: "E-done",
      ended_at: "2026-10-04T16:00:00Z", step_title: "Search done", waiting_for: [], others: [],
      passed: [{ step: "Start_round", at: "x" }, { step: "Open_run", at: "x" }, { step: "Scout_searches", at: "x" }],
    });
    render(<HowTheyWork />, { hash: "#/agents/how?live=i1" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    expect(document.getElementById("step-E-done")!.querySelector(".step-run-mark")?.textContent).toBe("Ended here");
  });

  it("tells you when the run can't be loaded", async () => {
    stub(null);
    render(<HowTheyWork />, { hash: "#/agents/how?live=gone" });
    expect(await screen.findByRole("alert")).toHaveTextContent("no such run");
  });

  it("without a run, the page is as it was", async () => {
    stub();
    render(<HowTheyWork />);
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    expect(screen.queryByRole("heading", { name: "Where this is in its process" })).toBeNull();
    expect(document.querySelector(".step-run-mark")).toBeNull();
  });
});

describe("HowTheyWork, a running process, kept fresh by the live stream", () => {
  let live: ReturnType<typeof fakeLive>;
  beforeEach(() => {
    live = fakeLive();
  });
  afterEach(() => live.restore());

  it("reads the run again when the server says the harness changed, and sets no timer", async () => {
    const timers = pollTimers();
    const urls = stub();
    render(<HowTheyWork />, { hash: "#/agents/how?live=i1" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    const runReads = () => urls.filter((u) => u.includes("/harness/runs/i1")).length;
    await waitFor(() => expect(runReads()).toBeGreaterThan(0));
    const before = runReads();
    await live.changed("agents");
    expect(runReads()).toBe(before);
    await live.changed("harness");
    await waitFor(() => expect(runReads()).toBeGreaterThan(before));
    expect(timers.seen()).toEqual([]);
    timers.stop();
  });
});
