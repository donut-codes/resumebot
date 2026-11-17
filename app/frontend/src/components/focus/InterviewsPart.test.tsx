import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import type { Interview, Prep, PrepReport } from "../../api/types/pipeline";
import InterviewsPart from "./InterviewsPart";
import { fakeLive, pollTimers } from "../../test-live";

// The Interviews part of a role's pane: the interviews as cards, the form that adds or changes one, the status line of
// the prep, and the report once it is ready. Every name and employer here is made up.

const SOURCE = { url: "https://example.com/news", title: "Example newsroom", checked_on: "2026-10-29" };

const REPORT: PrepReport = {
  quick_sheet: {
    when_at: "2099-01-15T15:00:00Z",
    round_label: "Hiring manager",
    people: [{ name: "Ada Sample", title: "Director" }],
    core_need: "Stand up a project that gets the first audit passed.",
    opener: "I build projects from nothing.",
    pay: { expectation: "My starting base salary expectation is $140,000.", market: { low: 120000, high: 150000, basis: "base" } },
    first_choice: [{ question: "How did the last audit go?", round: "hiring_manager" }],
    story_map: [{ question: "Tell me about a hard launch.", story: "The first launch" }],
  },
  open_gaps: [{ text: "No story for budgets.", action: "Write one sentence about a budget you held." }],
  core_need: { text: "Stand up a project that gets the first audit passed." },
  good_looks_like: [{ text: "Evidence beats policy.", source: SOURCE }],
  stage_read: {
    screens_for: "Whether you can lead without a team yet.",
    interviewers: [
      { name: "Ada Sample", found: true, background: "Runs the project office.", screens_for: "Judgement", connection: { text: "You both started projects.", from: ["b1"], cites: [{ id: "b1", text: "Started a project." }] }, dont: "Don't recite frameworks.", questions: ["What would you do first?"] },
      { name: "Bo Unknown", found: false, background: "", dont: "Don't guess their role.", questions: ["How do you work with them?"] },
    ],
  },
  staples: {
    opener: [{ text: "I build projects from nothing.", from: ["b1"], cites: [{ id: "b1", text: "Started a project." }] }],
    why_here: [{ text: "They raised a round.", source: SOURCE }],
    why_leaving: { ask_owner: "Write one sentence on why you left." },
    pay: { expectation: "My starting base salary expectation is $140,000.", assumptions: "This is base salary.", market: { low: 120000, high: 150000, basis: "base", source: SOURCE } },
  },
  questions: [
    { question: "Tell me about a hard launch.", method: "How you handle risk", answer: { kind: "story", title: "The first launch", text: "We shipped on time.", result: "Zero rollbacks", from: ["b1"], cites: [{ id: "b1", text: "Started a project." }] } },
    { question: "How do you manage budgets?", gap: "Write one sentence about a budget you held.", answer: null },
  ],
  problem: { kind: "approach", approach: "Start with an inventory.", discovery_questions: ["What exists today?"] },
  ask: [
    { question: "Tell me about the last incident.", round: "hiring_manager", priority: 1, diagnoses: "How they learn", good_answer: "A specific incident", bad_answer: "Nothing happens here", follow_up: "What changed after?" },
    { question: "Who owns budget?", round: "recruiter_screen", priority: 2, diagnoses: "Authority", good_answer: "A name", bad_answer: "Unclear", follow_up: "Who signs?" },
  ],
  reference: [{ text: "The company raised a round.", source: SOURCE }],
};

function prep(over: Partial<Prep> = {}): Prep {
  return { id: "p1", version: 1, state: "ready", step: "Ready", error: null, changed: [], report: REPORT, review: { summary: "Two answers lean on one job.", findings: [{ where: "Questions", problem: "One job carries two answers.", fix: "Use the second job." }] }, ...over };
}

function interview(over: Partial<Interview> = {}): Interview {
  return {
    id: "i1", role_id: "r1", when_at: "2099-01-15T15:00:00Z", round_kind: "hiring_manager", round_label: "Hiring manager", label: "Team chat",
    people: [{ id: "x1", name: "Ada Sample", title: "Director" }], notes: "Bring the one-pager.", past: false, can_prepare_again: true,
    versions: [{ id: "p1", version: 1, state: "ready" }], prep: prep(), ...over,
  };
}

function setup(over: Partial<React.ComponentProps<typeof InterviewsPart>> = {}) {
  const props = {
    stage: "active_discussion",
    interviews: [] as Interview[],
    onAdd: vi.fn(async () => {}),
    onEdit: vi.fn(async () => {}),
    onRemove: vi.fn(async () => {}),
    onPrepare: vi.fn(async () => {}),
    onRefresh: vi.fn(),
    ...over,
  };
  render(<InterviewsPart {...props} />);
  return props;
}

afterEach(() => vi.useRealTimers());

let live: ReturnType<typeof fakeLive>;
beforeEach(() => {
  live = fakeLive();
});
afterEach(() => live.restore());

describe("the Interviews part", () => {
  it("a role in Active Discussion can add an interview; one in any other stage can not", () => {
    setup();
    expect(screen.getByRole("button", { name: "Add an interview" })).toBeInTheDocument();
  });

  it("another stage with no interviews shows nothing, and with some shows them but no add button", () => {
    const { container } = render(
      <InterviewsPart stage="applied" interviews={[]} onAdd={vi.fn()} onEdit={vi.fn()} onRemove={vi.fn()} onPrepare={vi.fn()} onRefresh={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
    render(
      <InterviewsPart stage="rejected" interviews={[interview({ prep: null, past: true })]} onAdd={vi.fn()} onEdit={vi.fn()} onRemove={vi.fn()} onPrepare={vi.fn()} onRefresh={vi.fn()} />,
    );
    expect(screen.getByText("Hiring manager")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add an interview" })).not.toBeInTheDocument();
  });

  it("adding sends the time as an instant, the round, the label, the people and the notes", async () => {
    const props = setup();
    fireEvent.click(screen.getByRole("button", { name: "Add an interview" }));
    fireEvent.change(screen.getByLabelText(/^When/), { target: { value: "2099-01-15T10:30" } });
    fireEvent.change(screen.getByLabelText("Round"), { target: { value: "panel" } });
    fireEvent.change(screen.getByLabelText("What the employer calls it"), { target: { value: "Loop" } });
    fireEvent.click(screen.getByRole("button", { name: "Add a person" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Ada Sample" } });
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Director" } });
    fireEvent.change(screen.getByLabelText("Notes"), { target: { value: "Bring a pen." } });
    fireEvent.click(screen.getByRole("button", { name: "Add interview" }));
    await waitFor(() => expect(props.onAdd).toHaveBeenCalled());
    // 10:30 typed is the owner's clock (UTC+14), not the browser's.
    expect(props.onAdd).toHaveBeenCalledWith({
      when: "2099-01-14T20:30:00.000Z", round_kind: "panel", label: "Loop", people: [{ name: "Ada Sample", title: "Director" }], notes: "Bring a pen.",
    });
  });

  it("a refusal from the server is shown in the form, and the form stays", async () => {
    const props = setup({ onAdd: vi.fn(async () => { throw new Error("An interview at that time is already on this role."); }) });
    fireEvent.click(screen.getByRole("button", { name: "Add an interview" }));
    fireEvent.change(screen.getByLabelText(/^When/), { target: { value: "2099-01-15T10:30" } });
    fireEvent.click(screen.getByRole("button", { name: "Add interview" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already on this role");
    expect(props.onAdd).toHaveBeenCalledTimes(1);
  });

  it("an interview without a time is not sent", () => {
    const props = setup();
    fireEvent.click(screen.getByRole("button", { name: "Add an interview" }));
    fireEvent.click(screen.getByRole("button", { name: "Add interview" }));
    expect(props.onAdd).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("Pick the date and time");
  });

  it("the card says its round, the employer's name for it, who is in it, the notes and where the prep is", () => {
    setup({ interviews: [interview({ prep: prep({ state: "writing", step: "Researching", report: null, review: null }) })] });
    const card = screen.getByRole("article", { name: /Hiring manager/ });
    expect(within(card).getByText("Team chat")).toBeInTheDocument();
    expect(within(card).getByText(/Ada Sample, Director/)).toBeInTheDocument();
    expect(within(card).getByText("Bring the one-pager.")).toBeInTheDocument();
    expect(within(card).getByText("Researching")).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: /report/i })).not.toBeInTheDocument();
  });

  it("a past interview with no prep says no report is made", () => {
    setup({ interviews: [interview({ past: true, prep: null, can_prepare_again: false })] });
    expect(screen.getByText("The time has passed, so no report is made.")).toBeInTheDocument();
  });

  it("Prepare again shows only when something changed, and asks for the next version", async () => {
    const props = setup({ interviews: [interview({ prep: prep({ changed: ["a new person, Sam Sample"] }) })] });
    expect(screen.getByText(/You changed this interview after the report was made: a new person, Sam Sample/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Prepare again" }));
    await waitFor(() => expect(props.onPrepare).toHaveBeenCalledWith("i1"));
  });

  it("with nothing changed there is no Prepare again", () => {
    setup({ interviews: [interview()] });
    expect(screen.queryByRole("button", { name: "Prepare again" })).not.toBeInTheDocument();
  });

  it("a prep that failed can be made again", () => {
    setup({ interviews: [interview({ prep: prep({ state: "failed", step: "The prep wasn't made.", error: "No agent took it.", report: null, review: null }) })] });
    expect(screen.getByText("The prep wasn't made.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Prepare again" })).toBeInTheDocument();
  });

  it("removing asks first, and only the confirmation removes", async () => {
    const props = setup({ interviews: [interview()] });
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(props.onRemove).not.toHaveBeenCalled();
    expect(screen.getByText(/Remove this interview and its report/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Keep it" }));
    expect(props.onRemove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove it" }));
    await waitFor(() => expect(props.onRemove).toHaveBeenCalledWith("i1"));
  });

  it("editing opens the form with what is there and sends only the save", async () => {
    const props = setup({ interviews: [interview()] });
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByLabelText("What the employer calls it")).toHaveValue("Team chat");
    expect(screen.getByLabelText("Name")).toHaveValue("Ada Sample");
    // 15:00 UTC is 05:00 the next day on the owner's clock, and the label says whose clock it is.
    expect(screen.getByLabelText(/^When \(.+\)$/)).toHaveValue("2099-01-16T05:00");
    fireEvent.change(screen.getByLabelText("Notes"), { target: { value: "Changed." } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(props.onEdit).toHaveBeenCalled());
    const [id, body] = (props.onEdit as unknown as Mock).mock.calls[0] as [string, { notes: string; people: unknown[] }];
    expect(id).toBe("i1");
    expect(body.notes).toBe("Changed.");
    expect(body.people).toEqual([{ name: "Ada Sample", title: "Director" }]);
  });

  it("while a prep is open the role is read again when the server says a prep changed, and sets no timer", async () => {
    const timers = pollTimers([5000]);
    const open = setup({ interviews: [interview({ prep: prep({ state: "reviewing", step: "Checking", report: null, review: null }) })] });
    await live.changed("agents");
    expect(open.onRefresh).not.toHaveBeenCalled();
    await live.changed("prep", ["i1"]);
    expect(open.onRefresh).toHaveBeenCalledTimes(1);
    await live.changed("prep", ["i1"]);
    expect(open.onRefresh).toHaveBeenCalledTimes(2);
    expect(timers.seen()).toEqual([]);
    timers.stop();
  });

  it("listens for nothing when no prep is open", async () => {
    const idle = setup({ interviews: [interview()] });
    await live.changed("prep");
    expect(idle.onRefresh).not.toHaveBeenCalled();
  });
});

describe("the report", () => {
  function open() {
    setup({ interviews: [interview()] });
    fireEvent.click(screen.getByRole("button", { name: "Open the report" }));
    return screen.getByRole("region", { name: "Interview prep report" });
  }

  it("opens on the Quick Sheet, then the sections in the report's order", () => {
    const report = open();
    const heads = within(report).getAllByRole("heading", { level: 4 }).map((h) => h.textContent);
    expect(heads).toEqual([
      "Quick Sheet", "Open gaps", "Core need", "What good looks like", "Stage read", "Staples", "Questions and stories", "Problem to solve", "Questions to ask", "Reference", "Red-team read",
    ]);
    const quick = within(report).getByRole("heading", { name: "Quick Sheet" }).closest("details");
    expect(quick).toHaveAttribute("open");
    expect(within(quick as HTMLElement).getByText("Stand up a project that gets the first audit passed.")).toBeInTheDocument();
    expect(within(quick as HTMLElement).getByText(/\$120,000 to \$150,000/)).toBeInTheDocument();
  });

  it("every interviewer has a card with a don't, and one nobody found says so", () => {
    const report = open();
    const cards = within(report).getAllByRole("group", { name: /Interviewer/ });
    expect(cards).toHaveLength(2);
    expect(within(cards[0]).getByText("Don't recite frameworks.")).toBeInTheDocument();
    expect(within(cards[1]).getByText("Don't guess their role.")).toBeInTheDocument();
    expect(within(cards[1]).getByText("Nothing found on public pages.")).toBeInTheDocument();
  });

  it("a claim about the owner shows the master line behind it, and a fact shows its source and the day it was checked", () => {
    const report = open();
    expect(within(report).getAllByText(/Started a project\./).length).toBeGreaterThan(0);
    const link = within(report).getAllByRole("link", { name: "Example newsroom" })[0];
    expect(link).toHaveAttribute("href", "https://example.com/news");
    expect(within(report).getAllByText(/checked 2026-10-29/).length).toBeGreaterThan(0);
  });

  it("a placeholder the owner must fill and a question with no story are said plainly", () => {
    const report = open();
    expect(within(report).getByText("You write this: Write one sentence on why you left.")).toBeInTheDocument();
    expect(within(report).getByText("No story yet: Write one sentence about a budget you held.")).toBeInTheDocument();
  });

  it("questions to ask are grouped by round, with each part on its own labelled line", () => {
    const report = open();
    const ask = within(report).getByRole("heading", { name: "Questions to ask" }).closest("details") as HTMLElement;
    const rounds = within(ask).getAllByRole("heading", { level: 5 }).map((h) => h.textContent);
    expect(rounds).toEqual(["Recruiter screen", "Hiring manager"]);
    expect(within(ask).getByText("A specific incident")).toBeInTheDocument();
    expect(within(ask).getByText("What changed after?")).toBeInTheDocument();
    expect(within(ask).getAllByText("A good answer")[0]).toBeInTheDocument();
  });

  it("the red-team read shows what the second pass found", () => {
    const report = open();
    expect(within(report).getByText("Two answers lean on one job.")).toBeInTheDocument();
    expect(within(report).getByText("One job carries two answers.")).toBeInTheDocument();
  });

  it("the index opens the section it names", () => {
    const report = open();
    const gaps = within(report).getByRole("heading", { name: "Open gaps" }).closest("details") as HTMLElement;
    expect(gaps).not.toHaveAttribute("open");
    fireEvent.click(within(report).getByRole("button", { name: "Open gaps" }));
    expect(gaps).toHaveAttribute("open");
  });

  it("hides again", () => {
    setup({ interviews: [interview()] });
    fireEvent.click(screen.getByRole("button", { name: "Open the report" }));
    fireEvent.click(screen.getByRole("button", { name: "Hide the report" }));
    expect(screen.queryByRole("region", { name: "Interview prep report" })).not.toBeInTheDocument();
  });
});
