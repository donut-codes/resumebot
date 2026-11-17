import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import type { RequirementsGrade } from "../../api/types/profile";
import GradeRequirements from "./GradeRequirements";
import { GRADE_START } from "./profileSpec";

const CHECK = "Let the app grade the requirements";
const HINT = "The analyst rates each requirement in a posting as Proven, Transferable or Unproven. With this on, each one earns the points set here.";

/** Holds the setting the way the page does, so an edit comes back as the next render. */
function Held({ start = GRADE_START, seen }: { start?: RequirementsGrade; seen?: (next: RequirementsGrade) => void }) {
  const [grade, setGrade] = useState(start);
  return (
    <GradeRequirements
      grade={grade}
      onChange={(next) => {
        setGrade(next);
        seen?.(next);
      }}
    />
  );
}

/** A sentence as it reads, with each box shown as its number in brackets. */
function said(el: Element): string {
  return Array.from(el.childNodes)
    .map((n) => {
      if (n.nodeType === Node.TEXT_NODE) return n.textContent;
      const box = (n as Element).querySelector("input");
      return box ? `[${box.value}]` : (n as Element).textContent;
    })
    .join("")
    .replace(/\s+/g, " ")
    .trim();
}

const box = (name: string) => screen.getByLabelText(name) as HTMLInputElement;
const REQUIRED = { proven: "Points for each proven required item", transferable: "Points for each transferable required item", unproven: "Points for each unproven required item" };
const PREFERRED = { either: "Points for each proven or transferable preferred item", unproven: "Points for each unproven preferred item" };

describe("Let the app grade the requirements", () => {
  it("is a checkbox, unticked, with no hint under it", () => {
    render(<Held />);
    const check = screen.getByRole("checkbox", { name: CHECK });
    expect(check).not.toBeChecked();
    expect(screen.queryByText(HINT)).toBeNull();
    expect(check.closest("label")).toHaveClass("fi-ss-tick");
  });

  it("starts at the starting numbers: required proven 2, transferable 0, unproven -2; preferred proven or transferable 1, unproven 0", () => {
    render(<Held />);
    expect(GRADE_START).toEqual({
      on: false, required_proven: 2, required_transferable: 0, required_unproven: -2, preferred_proven_or_transferable: 1, preferred_unproven: 0,
    });
    expect(box(REQUIRED.proven).value).toBe("2");
    expect(box(REQUIRED.transferable).value).toBe("0");
    expect(box(REQUIRED.unproven).value).toBe("-2");
    expect(box(PREFERRED.either).value).toBe("1");
    expect(box(PREFERRED.unproven).value).toBe("0");
  });

  it("says each as a sentence with its number in a box inside it, as the estimated ask's steps do", () => {
    const { container } = render(<Held />);
    const lines = Array.from(container.querySelectorAll(".fi-ss-grade-line")).map(said);
    expect(lines).toEqual([
      "Required: each proven [2], each transferable [0], each unproven [-2].",
      "Preferred: each proven or transferable [1], each unproven [0].",
    ]);
    // A number and its box are one piece of the sentence, as on the estimated ask's page.
    for (const input of container.querySelectorAll("input[type=number]")) expect(input.closest(".fi-ss-numbox")).not.toBeNull();
  });

  it("shows its numbers whether it is ticked or not, and keeps the ones typed", () => {
    const seen = vi.fn();
    render(<Held seen={seen} />);
    fireEvent.change(box(REQUIRED.proven), { target: { value: "3" } });
    expect(seen).toHaveBeenLastCalledWith({ ...GRADE_START, required_proven: 3 }); // unticked: still kept
    fireEvent.click(screen.getByRole("checkbox", { name: CHECK }));
    expect(seen).toHaveBeenLastCalledWith({ ...GRADE_START, required_proven: 3, on: true });
    fireEvent.change(box(PREFERRED.unproven), { target: { value: "-1" } });
    expect(seen).toHaveBeenLastCalledWith({ ...GRADE_START, required_proven: 3, preferred_unproven: -1, on: true });
    fireEvent.click(screen.getByRole("checkbox", { name: CHECK }));
    expect(seen).toHaveBeenLastCalledWith({ ...GRADE_START, required_proven: 3, preferred_unproven: -1, on: false });
    expect(box(REQUIRED.proven).value).toBe("3");
  });

  it("takes whole numbers of either sign: a fraction is cut to its whole part, and an emptied or half-typed box keeps the number it had", () => {
    const seen = vi.fn();
    render(<Held seen={seen} />);
    fireEvent.change(box(REQUIRED.transferable), { target: { value: "1.8" } });
    expect(seen).toHaveBeenLastCalledWith(expect.objectContaining({ required_transferable: 1 }));
    fireEvent.change(box(REQUIRED.unproven), { target: { value: "-5" } });
    expect(seen).toHaveBeenLastCalledWith(expect.objectContaining({ required_unproven: -5 }));
    fireEvent.change(box(PREFERRED.either), { target: { value: "" } });
    expect(seen.mock.calls.filter(([g]) => (g as RequirementsGrade).preferred_proven_or_transferable !== 1)).toHaveLength(0);
    fireEvent.blur(box(PREFERRED.either));
    expect(box(PREFERRED.either).value).toBe("1"); // the number that stands
  });

  it("marks a plus, a minus and a zero as the ask rows' points are: green for plus, oxblood for minus", () => {
    render(<Held />);
    expect(box(REQUIRED.proven)).toHaveAttribute("data-sign", "plus");
    expect(box(REQUIRED.transferable)).toHaveAttribute("data-sign", "none");
    expect(box(REQUIRED.unproven)).toHaveAttribute("data-sign", "minus");
    fireEvent.change(box(REQUIRED.transferable), { target: { value: "-1" } });
    expect(box(REQUIRED.transferable)).toHaveAttribute("data-sign", "minus");
  });

  it("shows what it was handed: a ticked setting with other numbers", () => {
    render(<Held start={{ on: true, required_proven: 5, required_transferable: 2, required_unproven: -4, preferred_proven_or_transferable: 3, preferred_unproven: -1 }} />);
    expect(screen.getByRole("checkbox", { name: CHECK })).toBeChecked();
    expect(box(REQUIRED.proven).value).toBe("5");
    expect(box(REQUIRED.unproven).value).toBe("-4");
    expect(box(PREFERRED.unproven).value).toBe("-1");
  });

  it("is a setting and not a row: it has no Remove, and it never says group, member, kind, pick one, AND or OR", () => {
    const { container } = render(<Held />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(container.textContent).not.toMatch(/\bgroups?\b|\bmembers?\b|\bkind\b|pick one/i);
    expect(container.textContent).not.toMatch(/\bAND\b|\bOR\b/);
  });
});
