import { fireEvent, screen, waitFor } from "@testing-library/react";
import type { GapProposal, Resume } from "../../api/types/me";
import { fakeLive } from "../../test-live";
import { json, render, stubFetch } from "../../test-utils";
import { api } from "../../api/client";
import ClaudeSuggestions, { type SuggestionCalls } from "./ClaudeSuggestions";

const CALLS: SuggestionCalls = {
  open: () => api.openGapProposals(),
  accept: (id, text, baseVersion) => api.acceptGapProposal(id, text, baseVersion),
  reject: (id) => api.rejectGapProposal(id),
};

// Made up: no real person's resume, employers or roles.
const SUGGESTED: GapProposal = {
  id: "p1", theme: "Senior Manager, Northwind Labs", text: "ran the quarterly risk review for the board", why: "shows the board work",
  status: "proposed", source: "owner.claude", place: { kind: "new_line", job: "Senior Manager, Northwind Labs", line: null },
  question: "", quote: "I ran the quarterly risk review for the board and cut its prep time in half.",
};
const MASTER = { summary: "After.", roles: [] } as unknown as Resume;

let live: ReturnType<typeof fakeLive>;
let waiting: GapProposal[];
let calls: { url: string; method: string; body: unknown }[];

function serve(over: { acceptStatus?: number } = {}) {
  let saved = 3;
  stubFetch((input: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url: input, method, body });
    if (input.endsWith("/find-gaps/proposals/open")) return json({ version: 3, proposals: waiting });
    if (input.endsWith("/accept")) {
      if (over.acceptStatus) return json({ detail: "The master resume was saved elsewhere since this page loaded." }, over.acceptStatus);
      waiting = waiting.slice(1);
      saved += 1;
      return json({ version: saved, resume: MASTER });
    }
    if (input.endsWith("/reject")) {
      waiting = [];
      return json({ id: "p1", status: "rejected" });
    }
    return json({});
  });
}

function show(dirty = false) {
  const onSavedResume = vi.fn();
  render(<ClaudeSuggestions calls={CALLS} baseVersion={3} dirty={dirty} onSavedResume={onSavedResume} />);
  return { onSavedResume };
}

beforeEach(() => {
  live = fakeLive();
  waiting = [];
  calls = [];
});
afterEach(() => live.restore());

describe("Suggested by your Claude on My Resume", () => {
  it("renders nothing when no suggestion waits", async () => {
    serve();
    show();
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/proposals/open"))).toBe(true));
    expect(screen.queryByRole("heading", { name: "Suggested by your Claude" })).not.toBeInTheDocument();
  });

  it("shows each waiting line with what Claude says the owner told it, not 'From your answer'", async () => {
    waiting = [SUGGESTED];
    serve();
    show();
    expect(await screen.findByRole("heading", { name: "Suggested by your Claude" })).toBeInTheDocument();
    expect(screen.getByText("ran the quarterly risk review for the board")).toBeInTheDocument();
    expect(screen.getByText("What your Claude says you told it:")).toBeInTheDocument();
    expect(screen.queryByText("From your answer:")).not.toBeInTheDocument();
  });

  it("reads the list again when Find Gaps' area changes, so a new suggestion appears with the page open", async () => {
    serve();
    show();
    await waitFor(() => expect(calls).toHaveLength(1));
    waiting = [SUGGESTED];
    await live.changed("gaps");
    expect(await screen.findByText("ran the quarterly risk review for the board")).toBeInTheDocument();
  });

  it("Accept names the master version the page loaded, hands the saved master up and the line leaves the block", async () => {
    waiting = [SUGGESTED];
    serve();
    const { onSavedResume } = show();
    fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
    await waitFor(() => expect(onSavedResume).toHaveBeenCalledWith(MASTER, 4));
    expect(calls.find((c) => c.url.endsWith("/accept"))?.body).toEqual({ base_version: 3 });
    await waitFor(() => expect(screen.queryByText("ran the quarterly risk review for the board")).not.toBeInTheDocument());
  });

  it("Reject keeps the master as it was and the line leaves the block", async () => {
    waiting = [SUGGESTED];
    serve();
    const { onSavedResume } = show();
    fireEvent.click(await screen.findByRole("button", { name: "Reject" }));
    await waitFor(() => expect(screen.queryByText("ran the quarterly risk review for the board")).not.toBeInTheDocument());
    expect(onSavedResume).not.toHaveBeenCalled();
  });

  it("a newer master refuses the accept in words, keeps the line and reads the list again", async () => {
    waiting = [SUGGESTED];
    serve({ acceptStatus: 409 });
    const { onSavedResume } = show();
    fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("saved elsewhere");
    expect(onSavedResume).not.toHaveBeenCalled();
    expect(calls.filter((c) => c.url.endsWith("/proposals/open")).length).toBeGreaterThan(1);
    expect(screen.getByText("ran the quarterly risk review for the board")).toBeInTheDocument();
  });

  it("with unsaved edits open, an accept keeps them: it does not replace the page's master and says so", async () => {
    waiting = [SUGGESTED];
    serve();
    const { onSavedResume } = show(true);
    fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Your unsaved edits are kept; reload to see the new line.");
    expect(onSavedResume).not.toHaveBeenCalled();
  });

  it("with unsaved edits open, a second accept names the version the first one saved, not the page's old base", async () => {
    waiting = [SUGGESTED, { ...SUGGESTED, id: "p2", text: "cut the review prep time in half" }];
    serve();
    show(true);
    fireEvent.click((await screen.findAllByRole("button", { name: "Accept" }))[0]);
    await screen.findByRole("status");
    await waitFor(() => expect(screen.getByText("cut the review prep time in half")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith("/accept"))).toHaveLength(2));
    const bodies = calls.filter((c) => c.url.endsWith("/accept")).map((c) => c.body);
    expect(bodies).toEqual([{ base_version: 3 }, { base_version: 4 }]);
  });
});
