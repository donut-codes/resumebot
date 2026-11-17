import { render, screen } from "@testing-library/react";
import StepSpec, { RichText, blocksOf } from "./StepSpec";

describe("RichText", () => {
  it("shows the rules as a numbered list, one rule to an item, with the file's own numbers", () => {
    const { container } = render(<RichText text={"Processing rule lead-in.\n3. third\n4. fourth `x`"} />);
    const ol = container.querySelector("ol")!;
    expect(ol.getAttribute("start")).toBe("3");
    expect(Array.from(ol.children).map((li) => li.textContent)).toEqual(["third", "fourth x"]);
    expect(container.querySelector("p")!.textContent).toBe("Processing rule lead-in.");
  });

  it("shows bullets as a list, and an indented bullet under a numbered rule as part of that rule", () => {
    const { container } = render(
      <RichText text={"1. Open it:\n   • *Blocked* when none\n   • Ready\n2. Next\n• G6 outcomes. done"} />,
    );
    const first = container.querySelector("ol > li")!;
    expect(Array.from(first.querySelectorAll("ul > li")).map((li) => li.textContent)).toEqual(["Blocked when none", "Ready"]);
    expect(first.querySelector("em")!.textContent).toBe("Blocked");
    expect(container.querySelectorAll("ol > li")).toHaveLength(2);
    expect(container.querySelector(":scope > ul > li")!.textContent).toBe("G6 outcomes. done");
  });

  it("renders backticks as code and never as HTML", () => {
    const { container } = render(<RichText text={"Call `plan.propose` for <b>x</b> and `a*b*c`."} />);
    expect(Array.from(container.querySelectorAll("code")).map((c) => c.textContent)).toEqual(["plan.propose", "a*b*c"]);
    expect(container.querySelector("b")).toBeNull();
    expect(container.textContent).toContain("<b>x</b>");
    expect(container.textContent).not.toContain("`");
  });

  it("keeps blank lines out and groups consecutive numbered lines into one list", () => {
    expect(blocksOf("1. a\n\n2. b\nLast words.").map((b) => b.kind)).toEqual(["ol", "p"]);
  });
});

describe("StepSpec", () => {
  const step = {
    id: "s1", harness_id: null, title: "Screen it", type: "task" as const, who: "Scout", who_about: "", role: "scout",
    actor: "The scout", trigger: "", fired_by: "", enters_at: "", preconditions: "", inputs: "", rules: "1. Check pay\n2. Check place",
    outputs: "A decision", exceptions: "", owner_sees: "Roles worth a look", source: "", criteria: null,
    trigger_kind: null, trigger_detail: null, trigger_subtype: null, calls: null, next: [], sent: null,
  };

  it("shows each filled section under its own heading, and leaves the empty ones out", () => {
    render(<StepSpec step={step} />);
    expect(screen.getByText("Actor")).toBeInTheDocument();
    expect(screen.getByText("Rules")).toBeInTheDocument();
    expect(screen.getByText("Outputs")).toBeInTheDocument();
    expect(screen.getByText("What you see")).toBeInTheDocument();
    expect(screen.getByText("Roles worth a look")).toBeInTheDocument();
    expect(screen.queryByText("Trigger")).toBeNull();
  });

  it("renders nothing for a step with nothing written", () => {
    const { container } = render(<StepSpec step={{ ...step, actor: "", rules: "", outputs: "", owner_sees: "" }} />);
    expect(container).toBeEmptyDOMElement();
  });
});
