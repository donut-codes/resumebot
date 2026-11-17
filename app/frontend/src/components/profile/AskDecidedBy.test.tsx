import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import type { Ask } from "../../api/types/profile";
import AskDecidedBy from "./AskDecidedBy";

// Made up: no real person's asks, employers or pay.

const READER: Ask = { id: "ask-a", section: "company", says: "The employer is small", points: 2, checked_by: "reader", roles: [] };
const BY_RECORD: Ask = { ...READER, checked_by: "app", fact: "employees", op: "<", value: "1000" };

/** Holds the ask the way the page does, so an edit comes back as the next render. */
function Held({ start, seen }: { start: Ask; seen?: (next: Ask) => void }) {
  const [ask, setAsk] = useState(start);
  return (
    <AskDecidedBy
      ask={ask}
      name={ask.says}
      onChange={(next) => {
        setAsk(next);
        seen?.(next);
      }}
    />
  );
}

describe("who decides an ask", () => {
  it("is Claude by default, said in one quiet line with a text button to let the app decide", () => {
    const { container } = render(<Held start={READER} />);
    expect(screen.getByText(/^Answered by: Claude, from the job posting/)).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "Answer from the company record instead: The employer is small" });
    expect(button).toHaveTextContent("Answer from the company record instead");
    expect(button).toHaveClass("fi-link");
    expect(container.querySelector("p.fi-ss-decider")).not.toBeNull();
  });

  it("lets the app decide from the employer's record: the ask keeps everything it had and is the app's, from a fact, a comparison and a number to type", () => {
    const seen = vi.fn();
    render(<Held start={READER} seen={seen} />);
    fireEvent.click(screen.getByRole("button", { name: "Answer from the company record instead: The employer is small" }));
    expect(seen).toHaveBeenCalledTimes(1);
    expect(seen).toHaveBeenCalledWith({ ...READER, checked_by: "app", fact: "employees", op: ">=", value: "" });
    // The line now says where it comes from.
    expect(screen.getByText(/^Answered by: the company record/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Answer from the company record instead/ })).toBeNull();
  });

  it("starts with a comparison that can't tell until a number is typed: a number to compare with, so a blank one decides nothing", () => {
    const seen = vi.fn();
    render(<Held start={READER} seen={seen} />);
    fireEvent.click(screen.getByRole("button", { name: /^Answer from the company record instead/ }));
    const next = seen.mock.lastCall![0] as Ask;
    expect(["<", ">", "<=", ">="]).toContain(next.op);
    expect(next.value).toBe("");
  });

  it("an ask the app decides from the employer's record offers no way back to the job posting", () => {
    render(<Held start={BY_RECORD} />);
    expect(screen.getByText(/^Answered by: the company record/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("a reader's ask that holds a fact the app uses once the record has it is the app's too", () => {
    render(<Held start={{ ...READER, fact: "employees", op: "<", value: "50" }} />);
    expect(screen.getByText(/^Answered by: the company record/)).toBeInTheDocument();
  });

  it("has nothing to offer an ask that is not a plain one: a pay tier, a posting's age, the requirement classes, one counted for each item, or the built-in ask", () => {
    for (const other of [
      { checked_by: "app" as const, amount: 100000 },
      { checked_by: "app" as const, days: 30 },
      { checked_by: "app" as const, from_classes: "U" as const, per_item: true, limit: -3 },
      { per_item: true, limit: 3 },
      { derived: "responsibilities" as const },
    ]) {
      const { container, unmount } = render(<Held start={{ ...READER, ...other }} />);
      expect(container, JSON.stringify(other)).toBeEmptyDOMElement();
      unmount();
    }
  });

  it("never says group, member, kind, pick one, AND or OR, and never offers the requirements as a way to decide", () => {
    const { container } = render(<Held start={READER} />);
    expect(container.textContent).not.toMatch(/\bgroups?\b|\bmembers?\b|\bkind\b|pick one|requirement/i);
    expect(container.textContent).not.toMatch(/\bAND\b|\bOR\b/);
  });
});
