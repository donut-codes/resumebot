import { fireEvent, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import type { GapCard, GapClarify as Clarify, GapProposal, Resume } from "../../api/types/me";
import { render } from "../../test-utils";
import GapClarify, { type ClarifyActions } from "./GapClarify";

// Made up: no real person's resume, employers or roles.
const CARDS: GapCard[] = [
  { id: "c1", theme: "Governance and risk", grade: "T", question: "Did you review a project's risks for a board?", state: "open", answer: "", can_change: true },
  { id: "c2", theme: "Governance and risk", grade: "T", question: "What changed because of it?", state: "open", answer: "", can_change: true },
  { id: "c3", theme: "Data catalog tooling", grade: "U", question: "Have you run a data catalog?", state: "not_true", answer: "", can_change: true },
];

const PROPOSAL: GapProposal = {
  id: "p1", theme: "Governance and risk", text: "ran the quarterly risk review for the board", why: "shows the bridge", status: "proposed",
  place: { kind: "new_line", job: "Senior Manager, Northwind Labs", line: null },
  question: "Did you review a project's risks for a board?", quote: "I ran the quarterly risk review for the board and cut its prep time.",
};

const MASTER = { summary: "After.", roles: [] } as unknown as Resume;

function clarify(over: Partial<Clarify> = {}): Clarify {
  return { cards: CARDS, proposals: [], coach: null, can_ask: false, can_propose: false, ...over };
}

function actions() {
  const calls: unknown[][] = [];
  const record = (name: string, result: unknown = {}) => (...args: unknown[]) => {
    calls.push([name, ...args]);
    return Promise.resolve(result);
  };
  const fake: ClarifyActions = {
    askQuestions: record("ask"),
    suggestLines: record("suggest"),
    answerCard: record("answer"),
    acceptProposal: record("accept", { version: 5, resume: MASTER }) as ClarifyActions["acceptProposal"],
    rejectProposal: record("reject"),
  };
  return { calls, fake };
}

function show(c: Clarify, extra: { ready?: boolean; why?: string | null } = {}) {
  const onChanged = vi.fn();
  const onSavedResume = vi.fn();
  const { calls, fake } = actions();
  render(<GapClarify runId="r1" actions={fake} clarify={c} ready={extra.ready ?? true} why={extra.why ?? null} whyId="why" onChanged={onChanged} onSavedResume={onSavedResume} />);
  return { onChanged, onSavedResume, calls };
}

describe("Working through the gaps: the Coach's questions", () => {
  it("groups the cards under their theme with its grade, and closes a theme marked not true", () => {
    show(clarify());
    expect(screen.getByRole("heading", { level: 4, name: /Governance and risk/ })).toBeInTheDocument();
    expect(screen.getByText("Transferable")).toBeInTheDocument();
    expect(screen.getByText("Unproven")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Answer" })).toHaveLength(2);
    expect(screen.getByText(/you won.t be asked about this for this role again/)).toBeInTheDocument();
  });

  it("keeps Answer off until the words are at least ten characters, then sends them whole", async () => {
    const { calls, onChanged } = show(clarify());
    const [answer] = screen.getAllByRole("button", { name: "Answer" });
    expect(answer).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Did you review a project's risks for a board?" }), { target: { value: "  I ran the review each quarter.  " } });
    expect(answer).toBeEnabled();
    fireEvent.click(answer);
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(calls).toEqual([["answer", "c1", "answer", "I ran the review each quarter."]]);
  });

  it("sends Skip and Not true with no words", async () => {
    const { calls } = show(clarify());
    fireEvent.click(screen.getAllByRole("button", { name: "Skip" })[0]);
    await waitFor(() => expect(calls).toHaveLength(1));
    fireEvent.click(screen.getAllByRole("button", { name: "Not true" })[1]);
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls).toEqual([
      ["answer", "c1", "skip", ""],
      ["answer", "c2", "not_true", ""],
    ]);
  });
});

describe("Working through the gaps: asking the Coach", () => {
  it("offers Work through the gaps and Suggest lines only when there is something to ask, and asks", async () => {
    const { calls } = show(clarify({ can_ask: true, can_propose: false }));
    expect(screen.getByRole("button", { name: "Suggest lines" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Work through the gaps" }));
    await waitFor(() => expect(calls).toEqual([["ask", "r1"]]));
  });

  it("asks for lines when answers are waiting", async () => {
    const { calls } = show(clarify({ can_propose: true }));
    fireEvent.click(screen.getByRole("button", { name: "Suggest lines" }));
    await waitFor(() => expect(calls).toEqual([["suggest", "r1"]]));
  });

  it("has both buttons off with the one-line why in peak hours, and while the Coach works shows its step", () => {
    show(clarify({ can_ask: true, can_propose: true, coach: { id: "j1", mode: "ask", state: "waiting", step: "Waiting for an agent" } }), {
      ready: false, why: "Claude's peak hours: no Claude work until 5:00 PM.",
    });
    expect(screen.getByRole("button", { name: "Work through the gaps" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Suggest lines" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Work through the gaps" })).toHaveAttribute("title", "Claude's peak hours: no Claude work until 5:00 PM.");
    expect(screen.getByRole("status")).toHaveTextContent("Waiting for an agent");
  });

  it("says why a job stopped", () => {
    show(clarify({ coach: { id: "j1", mode: "ask", state: "failed", step: "Stopped", error: "No agent picked this up within 5 minutes. Try again in a moment." } }));
    expect(screen.getByRole("alert")).toHaveTextContent("No agent picked this up within 5 minutes");
  });
});

describe("Working through the gaps: the Coach's proposals", () => {
  it("shows the words, where they go and the answer they came from", () => {
    show(clarify({ proposals: [PROPOSAL] }));
    expect(screen.getByText("ran the quarterly risk review for the board")).toBeInTheDocument();
    expect(screen.getByText("A new line under Senior Manager, Northwind Labs")).toBeInTheDocument();
    expect(screen.getByText(/I ran the quarterly risk review for the board and cut its prep time\./)).toBeInTheDocument();
    expect(screen.getByText("From your answer:")).toBeInTheDocument();
  });

  it("Accept saves the Coach's words and hands the saved master and its version up", async () => {
    const { calls, onSavedResume } = show(clarify({ proposals: [PROPOSAL] }));
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(onSavedResume).toHaveBeenCalledWith(MASTER, 5));
    expect(calls).toEqual([["accept", "p1", undefined]]);
  });

  it("Edit opens the words and Accept then sends the owner's own", async () => {
    const { calls } = show(clarify({ proposals: [PROPOSAL] }));
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByRole("textbox", { name: "The line, in your words" }), { target: { value: " Ran the board's quarterly risk review " } });
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls).toEqual([["accept", "p1", "Ran the board's quarterly risk review"]]);
  });

  it("Reject keeps the master as it was and the decided ones show as a quiet line", async () => {
    const { calls, onChanged, onSavedResume } = show(
      clarify({ proposals: [PROPOSAL, { ...PROPOSAL, id: "p2", text: "Added one", status: "accepted" }, { ...PROPOSAL, id: "p3", text: "Turned down", status: "rejected" }] }),
    );
    expect(screen.getByText("Added to your resume")).toBeInTheDocument();
    expect(screen.getByText("Rejected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(calls).toEqual([["reject", "p1"]]);
    expect(onSavedResume).not.toHaveBeenCalled();
  });
});
