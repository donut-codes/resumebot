import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { HarnessExample } from "../../api/types/agents";
import { json, render, stubFetch } from "../../test-utils";
import { EXAMPLE, OVERVIEW } from "./how.fixture";
import HowTheyWork from "./HowTheyWork";
import { howProcessRoutes } from "./howBpmn.fixture";

function stub(example: HarnessExample | null = EXAMPLE) {
  const urls: string[] = [];
  stubFetch((input: RequestInfo | URL) => {
    const url = String(input);
    urls.push(url);
    if (url.includes("/harness/steps/")) return json({ step_id: "jobsearch.scout", example });
    if (url.includes("/harness/tasks/")) {
      return json({ process: "jobsearch_round", step: OVERVIEW.processes[0].steps.find((s) => s.id === "Scout_searches"), example: EXAMPLE });
    }
    const edit = howProcessRoutes(url);
    if (edit) return edit;
    if (url.includes("/harness")) return json(OVERVIEW);
    return json({}, 404);
  });
  return urls;
}

describe("HowTheyWork", () => {
  it("draws the search with the process view: the flow and the numbered steps", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how" });
    expect(await screen.findByRole("heading", { level: 2, name: "Process flow" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { level: 2, name: "Steps" })).toBeInTheDocument();
    // The search is the one top-level process, open first.
    expect(await screen.findByRole("link", { name: "Search for roles", current: "page" })).toBeInTheDocument();
    const listed = Array.from(document.querySelectorAll(".step-list .step-title-editable")).map((n) => n.textContent ?? "");
    for (const title of ["Read the gates", "The scout searches the boards", "Is any role worth a look?", "Show you the roles"]) {
      expect(listed.some((t) => t.includes(title))).toBe(true);
    }
    // The question is a Decision, and the search ends.
    const pills = Array.from(document.querySelectorAll(".step-row .step-bpmn-badge")).map((p) => p.textContent);
    expect(pills.filter((p) => p === "Decision")).toHaveLength(1);
    expect(pills.filter((p) => p === "End")).toHaveLength(1);
    // What starts the search is a start event, and every step shows its actor on its row.
    expect(pills.filter((p) => p === "Start")).toHaveLength(1);
    const actors = Array.from(document.querySelectorAll(".step-list .step-actor-line")).map((n) => n.textContent);
    expect(actors).toContain("Scout");
    expect(actors).toContain("You");
    expect(actors).toContain("Automatic");
  });

  it("says nothing about goals or projects anywhere on the page", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    expect(document.body.textContent).not.toMatch(/goal|project/i);
  });

  it("draws a start event as a start with a short name and, in its detail, what fires it and where it enters", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    const row = document.getElementById("step-Start_round")!;
    expect(row.querySelector(".step-title-editable")!.textContent).toBe("A search starts");
    expect(row.querySelector(".step-bpmn-badge")!.textContent).toBe("Start");
    fireEvent.click(await within(row).findByRole("button", { name: /Expand details for A search starts/ }));
    const headings = Array.from(row.querySelectorAll(".how-spec dt")).map((n) => n.textContent);
    expect(headings).toEqual(["Actor", "Trigger", "Enters at"]);
    expect(row.querySelector(".how-spec")!.textContent).toContain("You press Search now");
    // the process has its own start event, so no generic Start step is added in front of it
    const titles = Array.from(document.querySelectorAll(".step-list .step-title-editable")).map((n) => n.textContent);
    expect(titles).not.toContain("Start");
  });

  it("opens a step and shows who does it and, behind 'What the agent is sent', every file verbatim", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    const row = document.getElementById("step-Scout_searches")!;
    fireEvent.click(await within(row).findByRole("button", { name: /Expand details for The scout searches the boards/ }));
    // What the specification says, under its own headings.
    const headings = Array.from(row.querySelectorAll(".how-spec dt")).map((n) => n.textContent);
    expect(headings).toEqual([
      "Actor", "Trigger", "Preconditions", "Inputs", "Rules", "Outputs", "Exceptions", "What you see", "Source",
    ]);
    expect(await within(row).findByText("Scout, with jobs.report")).toBeInTheDocument();
    expect(await within(row).findByText("A report with no roles ends the search.")).toBeInTheDocument();
    expect(await within(row).findByText("The roles to look at on the Pipeline.")).toBeInTheDocument();
    // The rules are a numbered list, one rule to an item, keeping the file's numbers; code is code.
    const rules = row.querySelectorAll(".how-spec-row")[4].querySelector("ol")!;
    expect(Array.from(rules.querySelectorAll(":scope > li")).map((li) => li.textContent)).toEqual([
      "Reads the profile (jobs.profile).", "Searches the boards.", "Reports each role with a line of evidence.",
    ]);
    expect(rules.querySelector("li code")!.textContent).toBe("jobs.profile");
    expect((await within(row).findByText("Principle")).tagName).toBe("STRONG");
    expect(row.querySelector(".how-spec")!.textContent).not.toContain("`");
    // Nothing is shown until it is opened.
    expect(screen.queryByText(/You search the boards/)).toBeNull();
    fireEvent.click(await within(row).findByRole("button", { name: "What the agent is sent" }));
    for (const name of ["agents.scout.role", "jobsearch.scout.instructions", "jobsearch.scout.report"]) {
      expect(await within(row).findByText(name)).toBeInTheDocument();
    }
    // A file is a monospace block, text exactly as served, placeholders as written.
    fireEvent.click(await within(row).findByRole("button", { name: /Show instructions for this step/ }));
    const block = row.querySelector("pre.how-file")!;
    expect(block.textContent).toBe("# Search for roles\nReport with {{report_call}}.");
    // The parts added with the task, field by field, with what each is.
    expect(await within(row).findByText("known")).toBeInTheDocument();
    expect(await within(row).findByText("How many roles from your tracker are already known.")).toBeInTheDocument();
  });

  it("shows a real example with its size against the budget", async () => {
    const urls = stub();
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    const row = document.getElementById("step-Scout_searches")!;
    fireEvent.click(await within(row).findByRole("button", { name: /Expand details for The scout searches the boards/ }));
    fireEvent.click(await within(row).findByRole("button", { name: "What the agent is sent" }));
    fireEvent.click(await within(row).findByRole("button", { name: "Show a real example" }));
    expect(await within(row).findByText(/6,000 characters of 200,000 characters/)).toBeInTheDocument();
    expect(urls.some((u) => u.includes("/harness/steps/jobsearch.scout/example"))).toBe(true);
    expect(await within(row).findByText("40 characters")).toBeInTheDocument();
    expect(row.querySelectorAll("pre.how-file")[0].textContent).toContain('"focus": "remote design roles"');
  });

  it("labels an example as what the agent was sent, with the files that changed since", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    const row = document.getElementById("step-Scout_searches")!;
    fireEvent.click(await within(row).findByRole("button", { name: /Expand details for The scout searches the boards/ }));
    fireEvent.click(await within(row).findByRole("button", { name: "What the agent is sent" }));
    fireEvent.click(await within(row).findByRole("button", { name: "Show a real example" }));
    const label = await within(row).findByText(/^As sent to the scout at /);
    expect(label.textContent).toContain("Changed since: jobsearch.scout.instructions.");
  });

  it("says plainly when an older task has no copy kept and is rebuilt", async () => {
    stub({
      ...EXAMPLE, source: "rebuilt", label: "no copy kept, rebuilt as it would be sent now", agent: null, sent_at: null,
      changed_files: [],
    });
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    const row = document.getElementById("step-Scout_searches")!;
    fireEvent.click(await within(row).findByRole("button", { name: /Expand details for The scout searches the boards/ }));
    fireEvent.click(await within(row).findByRole("button", { name: "What the agent is sent" }));
    fireEvent.click(await within(row).findByRole("button", { name: "Show a real example" }));
    expect(await within(row).findByText("No copy kept, rebuilt as it would be sent now.")).toBeInTheDocument();
  });

  it("says plainly when no task like it has been sent", async () => {
    stub(null);
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    const row = document.getElementById("step-Scout_searches")!;
    fireEvent.click(await within(row).findByRole("button", { name: /Expand details for The scout searches the boards/ }));
    fireEvent.click(await within(row).findByRole("button", { name: "What the agent is sent" }));
    fireEvent.click(await within(row).findByRole("button", { name: "Show a real example" }));
    expect(await within(row).findByText("No task like this has been sent yet.")).toBeInTheDocument();
  });

  it("a step nobody is sent anything at says so", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    const row = document.getElementById("step-Open_run")!;
    fireEvent.click(await within(row).findByRole("button", { name: /Expand details for Read the gates/ }));
    expect(await within(row).findByText("Nothing is sent to an agent at this step.")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: "What the agent is sent" })).toBeNull();
  });

  it("a decision lists its ways out, read-only, with no way to change a rule", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    const row = document.getElementById("step-Gw_any")!;
    fireEvent.click(await within(row).findByRole("button", { name: /Expand details for Is any role worth a look\?/ }));
    expect(await within(row).findByText("Which way it goes")).toBeInTheDocument();
    expect(within(row).getByText("At least one role was kept.")).toBeInTheDocument();
    expect(within(row).getByText("stored.kept > 0").tagName).toBe("CODE");
    expect(within(row).getByText("When no other way applies.")).toBeInTheDocument();
    expect(within(row).getByText("Always: Nothing is ever applied to.")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /change this rule/i })).toBeNull();
    expect(document.querySelector("textarea")).toBeNull();
  });

  it("shows the facts a rule can look at behind a toggle, leaving out the engine's own", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    expect(screen.queryByText("stored.kept")).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: /Show what a rule can look at/ }));
    expect(screen.getByText("stored.kept")).toBeInTheDocument();
    expect(screen.getByText("Roles kept for the page.")).toBeInTheDocument();
    expect(screen.queryByText("Hidden.")).toBeNull();
    expect(screen.getByText("2 facts", { exact: false })).toBeInTheDocument();
  });

  it("opened from an agent's activity it shows that one search as sent, open", async () => {
    const urls = stub();
    render(<HowTheyWork />, { hash: "#/agents/how?sent=r1" });
    const panel = await screen.findByRole("region", { name: "What was sent for this search" });
    expect(await within(panel).findByText(/Step: The scout searches the boards/)).toBeInTheDocument();
    expect(urls.some((u) => u.includes("/harness/tasks/r1"))).toBe(true);
    expect(await within(panel).findByText("agents.scout.role")).toBeInTheDocument();
    expect(await within(panel).findByText(/6,000 characters of 200,000 characters/)).toBeInTheDocument();
    fireEvent.click(await within(panel).findByRole("button", { name: "See how all the steps work" }));
    // Back to the whole page: the search's payload is out of the address.
    await waitFor(() => expect(window.location.hash).toBe("#/agents/how"));
    expect(screen.queryByRole("region", { name: "What was sent for this search" })).toBeNull();
  });

  it("lists the processes as a tree: the top-level one is the tab and it shows its sub-process under it", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    // Only the top-level processes are in the first row; a sub-process is not one of them.
    const top = await screen.findByRole("navigation", { name: "Which process" });
    expect((await within(top).findAllByRole("link")).map((t) => t.textContent)).toEqual(["Search for roles"]);
    const sub = await screen.findByRole("navigation", { name: "Sub-processes of Search for roles" });
    expect((await within(sub).findAllByRole("link")).map((t) => t.textContent)).toEqual(["Screen one role"]);
    expect(await within(sub).findByRole("link", { name: "Screen one role" })).not.toHaveAttribute("aria-current");
  });

  it("clicks between a process and its sub-process, and the sub-process shows the same detail", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    fireEvent.click(await screen.findByRole("link", { name: "Screen one role" }));
    expect(await screen.findByText("One role against your hard gates.")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Screen one role", current: "page" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Search for roles", current: "page" })).toBeInTheDocument();
    const row = document.getElementById("step-Gate")!;
    fireEvent.click(await within(row).findByRole("button", { name: /Expand details for Check the gates/ }));
    expect(await within(row).findByText("Pay is at or over your floor.")).toBeInTheDocument();
    // It is one page, shared: it says who calls it, and the link opens the parent's tab.
    expect(await screen.findByText(/Called from/)).toBeInTheDocument();
    // Picking the open sub-process again goes back up to its parent.
    fireEvent.click(await screen.findByRole("link", { name: "Screen one role" }));
    expect(await screen.findByRole("link", { name: "Screen one role", current: false })).toBeInTheDocument();
    expect(await screen.findByText("One search for new roles.")).toBeInTheDocument();
  });

  it("a call activity is a sub-process step with a link that opens its tab", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    const row = document.getElementById("step-Screen_each")!;
    expect(await within(row).findByText("Subprocess")).toBeInTheDocument();
    fireEvent.click(await within(row).findByRole("button", { name: /Expand details for Screen each role/ }));
    fireEvent.click(await within(row).findByRole("button", { name: "Open Screen one role" }));
    expect(await screen.findByText("One role against your hard gates.")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Screen one role", current: "page" })).toBeInTheDocument();
  });

  it("follows the address: the path opens the tab, and picking a tab is the new path", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how/jobsearch_round/jobsearch_screen_role" });
    expect(await screen.findByText("One role against your hard gates.")).toBeInTheDocument();
    // The open sub-process is a link back up to the process it is under; the address says so.
    const sub = await screen.findByRole("link", { name: "Screen one role" });
    expect(sub).toHaveAttribute("href", "#/agents/how/jobsearch_round");
    fireEvent.click(sub);
    await waitFor(() => expect(window.location.hash).toBe("#/agents/how/jobsearch_round"));
    expect(await screen.findByText("One search for new roles.")).toBeInTheDocument();
  });

  it("falls back to the process it can open when the address names one the page doesn't have", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how/nope" });
    expect(await screen.findByRole("link", { name: "Search for roles", current: "page" })).toBeInTheDocument();
    expect(await screen.findByText("One search for new roles.")).toBeInTheDocument();
  });

  it("is a page of blocks: the process under a heading of its name, its versions, and the facts a rule can look at", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how" });
    expect(await screen.findByRole("heading", { level: 2, name: "Search for roles" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Versions" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "What a rule can look at" })).toBeInTheDocument();
    // The process sits in its block, under its sub-heads; its tabs are pills on a track above it.
    expect(document.querySelector("#how-process .process-view")).not.toBeNull();
    expect(document.querySelector("nav.fi-pills")).not.toBeNull();
  });

  it("a process shown alone has no page around it: no blocks, no other tabs", async () => {
    stub();
    render(<HowTheyWork only="jobsearch_round" />);
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    expect(document.querySelector(".fi-block")).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Which process" })).toBeNull();
    expect(screen.getByText("One search for new roles.")).toBeInTheDocument();
  });

  it("lists the files an agent is sent as a sheet, one to a row, the text in a scrollable box", async () => {
    stub();
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    const row = document.getElementById("step-Scout_searches")!;
    fireEvent.click(await within(row).findByRole("button", { name: /Expand details for The scout searches the boards/ }));
    fireEvent.click(await within(row).findByRole("button", { name: "What the agent is sent" }));
    const items = row.querySelectorAll(".how-files > .how-file-item");
    expect(items).toHaveLength(3);
    expect(Array.from(items).map((i) => i.querySelector(".how-file-name")!.textContent)).toEqual([
      "agents.scout.role", "jobsearch.scout.instructions", "jobsearch.scout.report",
    ]);
    // Closed until asked for: no box yet. Opened, the text sits in a box of its own in the item, with Edit and History over it.
    expect(items[1].querySelector("pre.how-file")).toBeNull();
    fireEvent.click(within(items[1] as HTMLElement).getByRole("button", { name: /Show instructions for this step/ }));
    expect(items[1].querySelector(":scope > .how-file-body > pre.how-file")).not.toBeNull();
    expect(within(items[1] as HTMLElement).getByRole("button", { name: "Edit" })).toBeEnabled();
    expect(row.querySelectorAll(".how-part-title").length).toBeGreaterThanOrEqual(3);
  });

  it("a new role needs no change: roles in a step are whatever words the server sends", async () => {
    const overview = JSON.parse(JSON.stringify(OVERVIEW));
    overview.processes[0].steps[1].who = "Writer";
    stubFetch(() => json(overview));
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await screen.findByRole("heading", { level: 2, name: "Steps" });
    const actors = Array.from(document.querySelectorAll(".step-list .step-actor-line")).map((n) => n.textContent);
    expect(actors).toContain("Writer");
  });
});
