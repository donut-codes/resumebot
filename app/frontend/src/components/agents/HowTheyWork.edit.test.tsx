import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { ProcessHistory } from "../../api/types/agents";
import { json, render, settle, stubFetch } from "../../test-utils";
import { bpmnOf, history, howProcessRoutes, version } from "./howBpmn.fixture";
import { OVERVIEW, RUN } from "./how.fixture";
import HowTheyWork from "./HowTheyWork";

const KEY = "jobsearch_round";
const FILE = bpmnOf(OVERVIEW.processes[0]);

interface Call {
  url: string;
  body: Record<string, unknown> | undefined;
}

/** The page's calls: the overview, the process's file and versions, and the edit routes, answered as the test says. */
function stub(
  opts: {
    valid?: boolean;
    number?: number;
    versions?: ProcessHistory;
    edits?: () => Promise<Response>;
    validate?: (call: number) => Promise<Response> | Response;
    overview?: object;
    run?: object;
  } = {},
) {
  const calls: Call[] = [];
  let checks = 0;
  stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, body });
    if (url.includes("/harness/runs/")) return json(opts.run ?? RUN);
    if (url.endsWith("/validate")) {
      if (opts.validate) return opts.validate(++checks);
      return json(
        opts.valid === false
          ? { ok: false, problems: ["The step 'Open_run' has no way out."], changes: [] }
          : { ok: true, problems: [], changes: ["Renamed."] },
      );
    }
    if (url.endsWith("/edits")) {
      return (
        opts.edits?.() ??
        json({
          process: KEY, running: { number: 2, note: null },
          versions: [version(2, { current: true, why: "Reads the owner's gates", summary: ["Renamed."] }), version(1, { can_restore: true })],
        })
      );
    }
    if (url.endsWith("/rollback")) return json(history(KEY, [version(3, { current: true, restored: 1 }), version(2), version(1)]));
    const routed = howProcessRoutes(url, { number: opts.number ?? 1, history: opts.versions });
    if (routed) return routed;
    if (url.includes("/harness")) return json(opts.overview ?? OVERVIEW);
    return json({}, 404);
  });
  return calls;
}

/** Opens the editor and renames "Read the gates", so there is something to save. */
async function rename(to = "Read the owner's gates") {
  fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
  const row = await waitFor(() => {
    const el = document.getElementById("step-Open_run");
    if (!el) throw new Error("no row yet");
    return el;
  });
  fireEvent.click(row);
  fireEvent.change(within(row).getByLabelText("Name"), { target: { value: to } });
}

/** The versions listed, one element each (a version's own changes are a list inside it). */
const itemsOf = (list: HTMLElement) => Array.from(list.children) as HTMLElement[];

const saveButton = () => screen.getByRole("button", { name: "Save as the next version" });
const why = () => screen.getByLabelText("Why are you changing it?");

describe("HowTheyWork: changing how a process works", () => {
  it("offers Edit, and not while a run is open", async () => {
    stub();
    const { unmount } = render(<HowTheyWork />, { hash: "#/agents/how" });
    expect(await screen.findByRole("button", { name: "Edit" })).toBeInTheDocument();
    unmount();
    // A run is open (a search going now): the process is drawn as it ran, and cannot be edited.
    render(<HowTheyWork />, { hash: "#/agents/how?live=i1" });
    await screen.findByRole("heading", { name: "Where this is in its process" });
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Versions" })).toBeNull();
  });

  it("opens the editor on the file that runs, checks it as it is edited and saves it with a why, with no approval", async () => {
    const calls = stub();
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await rename();
    expect(await screen.findByText("It can run as written.")).toBeInTheDocument();
    // Nothing is saved by editing, and Save waits for a why.
    expect(calls.some((c) => c.url.endsWith("/edits"))).toBe(false);
    expect(saveButton()).toBeDisabled();
    fireEvent.change(why(), { target: { value: "Reads the owner's gates" } });
    expect(saveButton()).not.toBeDisabled();
    fireEvent.click(saveButton());
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/harness/processes/jobsearch_round/edits"))).toBe(true));
    const sent = calls.find((c) => c.url.endsWith("/edits"))!;
    expect(sent.body!.xml).toBe(FILE.replace('name="Read the gates"', 'name="Read the owner\'s gates"'));
    expect(sent.body!.why).toBe("Reads the owner's gates");
    expect(sent.body!.based_on).toBe(1);
    // In force at once: no waiting for anyone's OK.
    expect(await screen.findByText(/Version 2 is in use: the next run follows it/)).toBeInTheDocument();
    expect(screen.queryByText(/waiting for your OK/i)).toBeNull();
  });

  it("takes a reason of any length: the box has no limit and the whole reason is sent", async () => {
    const calls = stub();
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await rename();
    await screen.findByText("It can run as written.");
    expect(why()).not.toHaveAttribute("maxlength");
    const long = Array.from({ length: 130 }, (_, i) => `Reason ${i + 1} that the owner gives for the change.`).join(" ");
    expect(long.length).toBeGreaterThan(5000);
    fireEvent.change(why(), { target: { value: long } });
    expect(why()).toHaveValue(long);
    fireEvent.click(saveButton());
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/edits"))).toBe(true));
    expect(calls.find((c) => c.url.endsWith("/edits"))!.body!.why).toBe(long);
  });

  it("will not save a file the engine says it cannot run, and shows why in its words", async () => {
    stub({ valid: false });
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await rename();
    expect(await screen.findByText("The step 'Open_run' has no way out.")).toBeInTheDocument();
    fireEvent.change(why(), { target: { value: "Because" } });
    expect(saveButton()).toBeDisabled();
  });

  it("shows the server's refusal when the process changed meanwhile, and reads the newer version", async () => {
    const calls = stub({
      edits: () => json({ detail: "The process changed while you were editing it. Reload it to see the new version." }, 409),
    });
    render(<HowTheyWork />, { hash: "#/agents/how" });
    await rename();
    await screen.findByText("It can run as written.");
    fireEvent.change(why(), { target: { value: "Because" } });
    const reads = () => calls.filter((c) => /\/bpmn(\?|$)/.test(c.url)).length;
    const before = reads();
    fireEvent.click(saveButton());
    expect(await screen.findByText(/The process changed while you were editing it/)).toBeInTheDocument();
    await waitFor(() => expect(reads()).toBeGreaterThan(before));
  });

  it("ignores the answer to a check that was asked for before a newer one", async () => {
    const late: ((r: Response) => void)[] = [];
    const calls = stub({
      validate: (call) =>
        call === 1
          ? new Promise<Response>((resolve) => late.push(resolve))
          : json({ ok: true, problems: [], changes: ["Renamed."] }),
    });
    render(<HowTheyWork />, { hash: "#/agents/how" });
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith("/validate"))).toHaveLength(1));
    const row = await waitFor(() => document.getElementById("step-Open_run")!);
    fireEvent.click(row);
    fireEvent.change(within(row).getByLabelText("Name"), { target: { value: "Read it" } });
    expect(await screen.findByText("It can run as written.")).toBeInTheDocument();
    // The older answer arrives now, inside act (a bare await here would let other loads land outside it).
    await act(async () => late[0](await json({ ok: false, problems: ["An answer about an older file."], changes: [] })));
    expect(screen.queryByText("An answer about an older file.")).toBeNull();
  });
});

describe("HowTheyWork: a process's versions", () => {
  const versions = history(KEY, [
    version(3, { current: true, why: "Put the gates back", restored: 1, source: "rollback", by: "Tester" }),
    version(2, { why: "Reads the owner's gates", summary: ["Renamed “Read the gates” to “Read the owner's gates”."] }),
    version(1, { why: "The file as it first shipped.", by: null }),
  ]);

  it("lists each version with when, who and why, and what an edit changed, the one in use marked", async () => {
    stub({ versions, number: 3 });
    render(<HowTheyWork />, { hash: "#/agents/how" });
    const list = await screen.findByRole("list", { name: "Versions of Search for roles" });
    const items = itemsOf(list);
    expect(items.map((li) => within(li).getByText(/^Version \d$/).textContent)).toEqual(["Version 3", "Version 2", "Version 1"]);
    expect(within(items[0]).getByText("In use")).toBeInTheDocument();
    expect(within(items[0]).getByText("Put the gates back")).toBeInTheDocument();
    expect(within(items[1]).getByText("Reads the owner's gates")).toBeInTheDocument();
    expect(within(items[1]).getByText("Renamed “Read the gates” to “Read the owner's gates”.")).toBeInTheDocument();
    expect(within(items[2]).getByText(/resumebot/)).toBeInTheDocument();
    // The one in use has nothing to restore.
    expect(within(items[0]).queryByRole("button", { name: "Restore this version" })).toBeNull();
  });

  it("shows an older version when asked, without changing anything", async () => {
    const calls = stub({ versions });
    render(<HowTheyWork />, { hash: "#/agents/how" });
    const list = await screen.findByRole("list", { name: "Versions of Search for roles" });
    const two = itemsOf(list)[1];
    fireEvent.click(within(two).getByRole("button", { name: "View this version" }));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/versions/2"))).toBe(true));
    expect(await within(two).findByRole("heading", { level: 2, name: "Steps" })).toBeInTheDocument();
    expect(calls.some((c) => c.body !== undefined && !c.url.endsWith("/validate"))).toBe(false);
    fireEvent.click(within(two).getByRole("button", { name: "Hide this version" }));
    expect(within(two).queryByRole("heading", { level: 2, name: "Steps" })).toBeNull();
    await settle();
  });

  it("restores a version as a new version", async () => {
    const calls = stub({ versions });
    render(<HowTheyWork />, { hash: "#/agents/how" });
    const list = await screen.findByRole("list", { name: "Versions of Search for roles" });
    const one = itemsOf(list)[2];
    fireEvent.click(within(one).getByRole("button", { name: "Restore this version" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/harness/processes/jobsearch_round/rollback"))).toBe(true));
    const sent = calls.find((c) => c.url.endsWith("/rollback"))!;
    expect(sent.body!.to).toBe(1);
  });

  it("says plainly when the newest version no longer works with this release", async () => {
    stub({
      versions: {
        ...history(KEY, [
          version(2, { loads: false, problem: "The step 'X' names an action that is gone.", can_restore: false }),
          version(1, { current: true, can_restore: false }),
        ]),
        running: { number: 1, note: "Version 2 no longer works with this release, so version 1 is running." },
      },
    });
    render(<HowTheyWork />, { hash: "#/agents/how" });
    expect(await screen.findByText("Version 2 no longer works with this release, so version 1 is running.")).toBeInTheDocument();
    expect(screen.getByText("The step 'X' names an action that is gone.")).toBeInTheDocument();
  });
});

describe("HowTheyWork: the version a run ran on", () => {
  it("says which version of the process the run ran on, and which is in use now", async () => {
    stub({ run: { ...RUN, version: 1, version_now: 3 } });
    render(<HowTheyWork />, { hash: "#/agents/how?live=i1" });
    expect(await screen.findByText("It ran on version 1 of this process; version 3 is in use now.")).toBeInTheDocument();
  });

  it("says only the version when it is the one in use", async () => {
    stub({ run: { ...RUN, version: 2, version_now: 2 } });
    render(<HowTheyWork />, { hash: "#/agents/how?live=i1" });
    expect(await screen.findByText("It ran on version 2 of this process.")).toBeInTheDocument();
  });
});
