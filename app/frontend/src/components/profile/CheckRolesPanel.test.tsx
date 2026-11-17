import { fireEvent, render, screen, within } from "@testing-library/react";
import { api } from "../../api/client";
import type { CheckJob, CheckListResult, CheckPostingResult } from "../../api/types/profile";
import { json, stubFetch } from "../../test-utils";
import { fakeLive, pollTimers } from "../../test-live";
import CheckRolesPanel from "./CheckRolesPanel";

// Made-up roles: no real person's employer, city or pay.
const PURSUED_RESULT: CheckListResult = {
  scope: "pursued",
  checked: 7,
  of: 9,
  tabs: [
    { id: "builders", name: "Builders", passed: 2, stopped: 1 },
    { id: "ops", name: "Operations", passed: 1, stopped: 0 },
  ],
  passed: {
    count: 3,
    items: [
      { id: "r1", title: "Projects Director", company: "Acme", tab: "builders", tab_name: "Builders", why: "builds projects" },
    ],
  },
  stopped: {
    count: 2,
    items: [
      { id: "r4", title: "Sales Lead", company: "Globex", tab: "builders", tab_name: "Builders", why: "a sales job" },
      { id: "r7", title: "Night Nurse", company: "Initech", why: "a nurse" },
    ],
  },
  fits_none: {
    count: 2,
    items: [{ id: "r9", title: "Events Coordinator", company: "Hooli", why: "no role is this" }],
  },
};

const GATED_RESULT: CheckListResult = {
  ...PURSUED_RESULT,
  scope: "gated",
  checked: 200,
  of: 200,
  passed: { count: 4, items: [{ id: "p1", title: "Projects Lead", company: "Acme", tab: "builders", tab_name: "Builders", why: "the same work" }] },
};

interface Server {
  ready?: boolean;
  /** What POST /profile/check answers: an id, or a refusal. */
  start: { id: string } | { detail: string; status: number };
  /** The answers asked for the job, in turn (the last one repeats). */
  job: CheckJob[];
  posts: Record<string, unknown>[];
}

function serve(server: Server) {
  let asked = 0;
  stubFetch((input: string, init?: RequestInit) => {
    if (init?.method === "POST" && input.endsWith("/api/v1/profile/check")) {
      server.posts.push(JSON.parse(String(init.body)));
      return "detail" in server.start ? json({ detail: server.start.detail }, server.start.status) : json(server.start);
    }
    if (/\/api\/v1\/profile\/check\/[^/]+$/.test(input)) {
      const answer = server.job[Math.min(asked++, server.job.length - 1)];
      return json(answer);
    }
    return json({}, 404);
  });
}

function open(server: Server, ready: { ready: boolean; why: string | null } | null = { ready: true, why: null }) {
  serve(server);
  const onClose = vi.fn();
  render(<CheckRolesPanel ready={ready} onClose={onClose} begin={api.checkRoles} load={api.checkJob} />);
  return { onClose, region: screen.getByRole("dialog", { name: "Check my roles" }) };
}

const job = (over: Partial<CheckJob>): CheckJob => ({ id: "c1", scope: "pursued", state: "waiting", step: "Waiting for an agent", ...over });
let live: ReturnType<typeof fakeLive>;
/** The server says the job moved: what the clock's tick used to bring. `times` messages, one after the other. */
const tick = async (times = 1) => {
  for (let i = 0; i < times; i++) await live.changed("jobs");
};

describe("Check my roles", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    live = fakeLive();
  });
  afterEach(() => {
    vi.useRealTimers();
    live.restore();
  });

  it("offers the three checks, the first chosen, and says it is a dry run", () => {
    const { region } = open({ start: { id: "c1" }, job: [], posts: [] });
    const choices = within(region).getAllByRole("radio");
    expect(choices.map((c) => c.parentElement?.textContent?.trim())).toEqual([
      "The roles I pursued",
      "Recent postings stopped for not matching your roles",
      "One job posting",
    ]);
    expect(choices[0]).toBeChecked();
    expect(within(region).getByText(/A dry run: it shows whether your target roles/)).toBeInTheDocument();
    expect(within(region).queryByLabelText("The job posting")).toBeNull();
    expect(within(region).getByRole("button", { name: "Check" })).toBeEnabled();
  });

  it("cannot be started while no agent is free, and says why", () => {
    const why = "Claude's peak hours: no Claude work until 3 PM.";
    const { region } = open({ start: { id: "c1" }, job: [], posts: [] }, { ready: false, why });
    expect(within(region).getByRole("button", { name: "Check" })).toBeDisabled();
    expect(within(region).getByText(why)).toBeInTheDocument();
  });

  it("asks after the check only when the server says a job moved, and sets no timer", async () => {
    const timers = pollTimers();
    const { region } = open({
      start: { id: "c1" },
      posts: [],
      job: [job({}), job({ state: "working", step: "Checked 3 of 7 roles", progress: { done: 3, total: 7 } })],
    });
    fireEvent.click(within(region).getByRole("button", { name: "Check" }));
    expect(await within(region).findByText("Waiting for an agent")).toBeInTheDocument();
    await live.changed("gaps");
    expect(within(region).getByText("Waiting for an agent")).toBeInTheDocument();
    await tick();
    expect(await within(region).findByText("Checked 3 of 7 roles")).toBeInTheDocument();
    expect(timers.seen()).toEqual([]);
    timers.stop();
  });

  it("checks the roles the owner pursued, shows how far along it is, then the lists", async () => {
    const server: Server = {
      start: { id: "c1" },
      posts: [],
      job: [
        job({}),
        job({ state: "working", step: "Checked 3 of 7 roles", progress: { done: 3, total: 7 } }),
        job({ state: "done", step: "Done", result: PURSUED_RESULT }),
      ],
    };
    const { region } = open(server);
    fireEvent.click(within(region).getByRole("button", { name: "Check" }));
    expect(await within(region).findByText("Waiting for an agent")).toBeInTheDocument();
    expect(server.posts).toEqual([{ scope: "pursued" }]);
    await tick();
    expect(await within(region).findByText("Checked 3 of 7 roles")).toBeInTheDocument();
    await tick();
    expect(await within(region).findByText(/Checked 7 of 9 roles: 3 would pass, 2 would be stopped, 2 fit none of your roles\./)).toBeInTheDocument();
    // Per role.
    expect(within(region).getByText("Builders: 2 would pass, 1 would be stopped")).toBeInTheDocument();
    expect(within(region).getByText("Operations: 1 would pass, 0 would be stopped")).toBeInTheDocument();
    // The lists: what would be stopped and what fits none are open; each item with its company, tab and line why.
    const stopped = within(region).getByText("Would be stopped (2)").closest("details") as HTMLElement;
    expect(stopped).toHaveAttribute("open");
    expect(within(stopped).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Sales Lead, Globex (Builders): a sales job",
      "Night Nurse, Initech: a nurse",
    ]);
    const none = within(region).getByText("Fit none of your roles (2)").closest("details") as HTMLElement;
    expect(none).toHaveAttribute("open");
    expect(within(none).getByText("Events Coordinator")).toBeInTheDocument();
    expect(within(none).getByText("Showing the first 1.")).toBeInTheDocument(); // the count is whole, the list is cut
    expect(within(region).getByText("Would pass (3)").closest("details")).not.toHaveAttribute("open");
    // Check again goes back to the choices.
    fireEvent.click(within(region).getByRole("button", { name: "Check again" }));
    expect(within(region).getByRole("button", { name: "Check" })).toBeEnabled();
  });

  it("opens the list of postings it would now pass when it checks the stopped ones", async () => {
    const server: Server = { start: { id: "c1" }, posts: [], job: [job({ scope: "gated", state: "done", step: "Done", result: GATED_RESULT })] };
    const { region } = open(server);
    fireEvent.click(within(region).getAllByRole("radio")[1]);
    fireEvent.click(within(region).getByRole("button", { name: "Check" }));
    expect(await within(region).findByText(/Checked 200 postings: 4 would pass/)).toBeInTheDocument();
    expect(server.posts).toEqual([{ scope: "gated" }]);
    expect(within(region).getByText("Would pass (4)").closest("details")).toHaveAttribute("open");
    expect(within(region).getByText("Would be stopped (2)").closest("details")).not.toHaveAttribute("open");
  });

  it("takes a pasted posting of any length: the box has no maxlength and no count, and all of the text is sent", async () => {
    const server: Server = { start: { id: "c1" }, posts: [], job: [job({ scope: "posting" })] };
    const { region } = open(server);
    fireEvent.click(within(region).getAllByRole("radio")[2]);
    const box = within(region).getByLabelText("The job posting");
    expect(box).not.toHaveAttribute("maxlength");
    const long = "Operations manager wanted. ".repeat(3_000).trim();
    fireEvent.change(box, { target: { value: long } });
    expect(box).toHaveValue(long);
    expect(within(region).queryByText(/characters/)).toBeNull();
    fireEvent.click(within(region).getByRole("button", { name: "Check" }));
    expect(await within(region).findByText("Waiting for an agent")).toBeInTheDocument();
    expect(server.posts).toEqual([{ scope: "posting", text: long }]);
  });

  it("checks one pasted posting, which needs some text, and says which role it matches or none", async () => {
    const matched: CheckPostingResult = { scope: "posting", matched: true, outcome: "passed", tab: { id: "ops", name: "Operations" }, why: "the same work" };
    const server: Server = { start: { id: "c1" }, posts: [], job: [job({ scope: "posting", state: "done", step: "Done", result: matched })] };
    const { region } = open(server);
    fireEvent.click(within(region).getAllByRole("radio")[2]);
    expect(within(region).getByRole("button", { name: "Check" })).toBeDisabled();
    // The paste box is read whole: it starts at ten rows (the same size as the other paste box, at twelve) and then grows.
    const box = within(region).getByLabelText("The job posting");
    expect(box).toHaveClass("growing-text");
    expect(box).toHaveAttribute("rows", "10");
    fireEvent.change(within(region).getByLabelText("The job posting"), { target: { value: "  Operations Manager wanted.  " } });
    fireEvent.click(within(region).getByRole("button", { name: "Check" }));
    expect(await within(region).findByText("It matches Operations: the same work")).toBeInTheDocument();
    expect(server.posts).toEqual([{ scope: "posting", text: "Operations Manager wanted." }]);
  });

  it.each([
    [{ outcome: "fits_none", tab: null, why: "no role is this" }, "It matches none of your roles: no role is this"],
    [{ outcome: "stopped", tab: null, why: "a sales job" }, "Your roles would stop it: a sales job"],
  ] as const)("says a posting no role matches (%o) in words", async (over, sentence) => {
    const result: CheckPostingResult = { scope: "posting", matched: false, ...over };
    const server: Server = { start: { id: "c1" }, posts: [], job: [job({ scope: "posting", state: "done", step: "Done", result })] };
    const { region } = open(server);
    fireEvent.click(within(region).getAllByRole("radio")[2]);
    fireEvent.change(within(region).getByLabelText("The job posting"), { target: { value: "A posting." } });
    fireEvent.click(within(region).getByRole("button", { name: "Check" }));
    expect(await within(region).findByText(sentence)).toBeInTheDocument();
  });

  it("says why a check failed and offers to try again", async () => {
    const error = "No agent picked this up within 5 minutes. Try again in a moment.";
    const { region } = open({ start: { id: "c1" }, posts: [], job: [job({ state: "failed", step: "Stopped", error })] });
    fireEvent.click(within(region).getByRole("button", { name: "Check" }));
    expect(await within(region).findByText(error)).toBeInTheDocument();
    expect(within(region).getByRole("button", { name: "Try again" })).toBeEnabled();
  });

  it("shows the server's words when the check can't be started", async () => {
    const detail = "No agent has checked in yet.";
    const { region } = open({ start: { detail, status: 409 }, posts: [], job: [] });
    fireEvent.click(within(region).getByRole("button", { name: "Check" }));
    expect(await within(region).findByText(detail)).toBeInTheDocument();
  });

  it("closes from Cancel while it works", async () => {
    const { region, onClose } = open({ start: { id: "c1" }, posts: [], job: [job({})] });
    fireEvent.click(within(region).getByRole("button", { name: "Check" }));
    expect(await within(region).findByText("Waiting for an agent")).toBeInTheDocument(); // the progress, not the form
    fireEvent.click(within(region).getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
