import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MarkedPosting } from "./MarkedPosting";

describe("MarkedPosting", () => {
  it("shows every line of the posting as written, in order, with no heading, rule or count added", () => {
    const text = [
      "Required Qualifications:",
      "- Ten years of leadership",
      "Preferred Qualifications:",
      "- Industry experience: regulated settings",
      "",
      "For degrees earned abroad, an equivalent credential is required.",
      "Physical Job Requirements",
    ].join("\n");
    const { container } = render(<MarkedPosting text={text} />);
    const body = container.querySelector(".role-description")!;
    expect(body.querySelectorAll("h4, hr, ul, .qmark-count")).toHaveLength(0);
    expect(Array.from(body.children).map((el) => el.textContent)).toEqual([
      "Required Qualifications:\n- Ten years of leadership\nPreferred Qualifications:\n- Industry experience: regulated settings",
      "For degrees earned abroad, an equivalent credential is required.\nPhysical Job Requirements",
    ]);
  });
});

describe("MarkedPosting qualification marks", () => {
  const TEXT = [
    "Required Qualifications:",
    "- Five years of project management",
    "- Strong written communication",
    "- A pilot's licence",
    "Preferred Qualifications:",
    "- Regulated industry experience",
  ].join("\n");
  const rows = [
    { index: 0, item: "Five years of project management", class: "P" as const, why: "ran projects for six years", by: "analyst" as const, owner_why: "", from: [{ id: "b1", text: "Ran the project office" }] },
    { index: 1, item: "Strong written communication", class: "T" as const, why: "writes briefs", by: "analyst" as const, owner_why: "", from: [] },
    { index: 2, item: "A pilot's licence", class: "U" as const, why: "", by: "analyst" as const, owner_why: "", from: [] },
    { index: 3, item: "Budget ownership, reworded", class: "U" as const, why: "", by: "analyst" as const, owner_why: "", from: [] },
  ];
  const preferred = [{ index: 0, item: "Regulated industry experience", class: "P" as const, why: "", by: "analyst" as const, owner_why: "", from: [] }];
  const correct = vi.fn().mockResolvedValue({});
  const graded = () => ({ correct, requirements: rows, preferred });

  const mark = (name: RegExp) => screen.getAllByRole("button", { name })[0];

  it("puts a labelled mark on each matching line, in place of its bullet", () => {
    const { container } = render(<MarkedPosting text={TEXT} graded={graded()} />);
    expect(mark(/Proven: show/)).toHaveTextContent("✓");
    expect(mark(/Transferable/)).toHaveTextContent("~");
    expect(screen.getAllByRole("button", { name: /Unproven/ })).toHaveLength(1);
    const line = mark(/Proven: show/).parentElement!;
    expect(line.textContent).toBe("✓Proven: show why Five years of project management");
    expect(container.textContent).not.toContain("- Five years");
  });

  it("shows no marks when nothing is graded", () => {
    render(<MarkedPosting text={TEXT} />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("adds no counts, headings or extra sections", () => {
    const { container } = render(<MarkedPosting text={TEXT} graded={graded()} />);
    expect(container.querySelectorAll("h4, .qmark-count, .qmark-extras")).toHaveLength(0);
  });

  it("does not show a graded item that matches no line", () => {
    const { container } = render(<MarkedPosting text={TEXT} graded={graded()} />);
    expect(container.textContent).not.toContain("Budget ownership, reworded");
    expect(container.textContent).not.toContain("Graded, but not found");
  });

  it("keeps the posting's text and order when marks are on", () => {
    const { container } = render(<MarkedPosting text={TEXT} graded={graded()} />);
    const lines = Array.from(container.querySelectorAll(".posting-line, .qmark-line"))
      .map((el) => el.textContent!.replace(/^[✓~✗](Proven|Transferable|Unproven): show why /, ""));
    expect(lines).toEqual([
      "Required Qualifications:", "Five years of project management", "Strong written communication", "A pilot's licence",
      "Preferred Qualifications:", "Regulated industry experience",
    ]);
  });

  it("opens the reason and the master lines in place, and closes again", () => {
    render(<MarkedPosting text={TEXT} graded={graded()} />);
    const first = mark(/Proven: show/);
    expect(first).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(first);
    expect(first).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("ran projects for six years")).toBeInTheDocument();
    expect(screen.getByText(/Ran the project office/)).toBeInTheDocument();
    fireEvent.click(first);
    expect(screen.queryByText("ran projects for six years")).toBeNull();
  });

  it("changes the grade with a reason through the existing route", async () => {
    render(<MarkedPosting text={TEXT} graded={graded()} />);
    fireEvent.click(screen.getByRole("button", { name: /Unproven/ }));
    fireEvent.click(screen.getByRole("button", { name: "Change the grade" }));
    fireEvent.change(screen.getByLabelText("Class"), { target: { value: "T" } });
    fireEvent.change(screen.getByLabelText("Why"), { target: { value: "close enough" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(correct).toHaveBeenCalledWith(2, "T", "close enough"));
  });

  describe("lines graded as not measurable", () => {
    const TEXT3 = ["Requirements", "- Five years of projects", "- Willingness to travel"].join("\n");
    const items = [
      { index: 0, line: 1, item: "Five years of projects", class: "P" as const, why: "ran projects", by: "analyst" as const, owner_why: "", from: [] },
      { index: 1, line: 2, item: "Willingness to travel", class: "N" as const, why: "a resume cannot show it", by: "analyst" as const, owner_why: "", from: [] },
    ];

    it("shows the neutral not-measurable box on an N line, with the analyst's reason on hover", () => {
      const { container } = render(<MarkedPosting text={TEXT3} graded={{ correct, requirements: items, preferred: [] }} />);
      const box = screen.getByRole("button", { name: /Not measurable: show why/ });
      expect(box).toHaveTextContent("–");
      expect(box).toHaveAttribute("title", "a resume cannot show it");
      expect(box.parentElement!.textContent).toContain("Willingness to travel");
      expect(container.textContent).not.toContain("- Willingness");
    });

    it("opens the reason in place, with no way to change the grade", () => {
      render(<MarkedPosting text={TEXT3} graded={{ correct, requirements: items, preferred: [] }} />);
      fireEvent.click(screen.getByRole("button", { name: /Not measurable: show why/ }));
      expect(screen.getByText("a resume cannot show it", { selector: "p" })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Change the grade/ })).toBeNull();
      expect(screen.queryByText(/Not linked to a resume line/)).toBeNull();
    });
  });

  describe("lines with no grade", () => {
    const TEXT2 = [
      "About the role",
      "We build things.",
      "",
      "Required Qualifications:",
      "- Five years of project management",
      "- Comfortable with ambiguity",
      "Education:",
      "- A bachelor's degree",
      "",
      "Benefits:",
      "- Free lunch",
    ].join("\n");
    const none = (c: HTMLElement) => Array.from(c.querySelectorAll(".qmark-none"));

    it("shows a neutral, labelled, unclickable marker on each ungraded line in a requirement part", () => {
      const { container } = render(<MarkedPosting text={TEXT2} graded={graded()} />);
      const lines = none(container).map((el) => el.parentElement!.textContent);
      expect(lines).toEqual(["Not compared Comfortable with ambiguity", "Not compared A bachelor's degree"]);
      expect(none(container)[0]).toHaveAttribute("title", expect.stringMatching(/not graded/i));
      expect(container.querySelectorAll("button")).toHaveLength(1);
    });

    it("leaves headings and lines outside requirement parts as plain text", () => {
      const { container } = render(<MarkedPosting text={TEXT2} graded={graded()} />);
      for (const t of ["Required Qualifications:", "Education:", "Benefits:", "- Free lunch", "We build things."]) {
        expect(container.textContent).toContain(t);
      }
      expect(container.textContent).not.toMatch(/Not compared\s*(Education|Required|Benefits|Free|We build)/);
      expect(none(container)).toHaveLength(2);
    });

    it("shows no neutral markers when nothing is graded", () => {
      const { container } = render(<MarkedPosting text={TEXT2} />);
      expect(none(container)).toHaveLength(0);
    });
  });

  describe("a posting that titles its requirements in words the page does not recognise", () => {
    const TEXT4 = [
      "About the role",
      "We run projects.",
      "“I am the person Acme Corp is looking for.”",
      "· You have 10+ years of project management experience.",
      "· You are a clear communicator who can explain trade-offs.",
      "· A line nobody graded.",
      "“I can apply in less than 4 minutes.”",
      "Apply now.",
    ].join("\n");
    const items = [
      { index: 0, item: "10+ years of project management experience", class: "P" as const, why: "ran projects", by: "analyst" as const, owner_why: "", from: [] },
      { index: 1, item: "A clear communicator who can explain trade-offs", class: "T" as const, why: "writes briefs", by: "analyst" as const, owner_why: "", from: [] },
    ];

    it("marks the matching bullets, boxes the bullet beside them, and leaves the rest of the posting plain", () => {
      const { container } = render(<MarkedPosting text={TEXT4} graded={{ correct, requirements: items, preferred: [] }} />);
      expect(screen.getByRole("button", { name: /Proven: show/ })).toHaveTextContent("✓");
      expect(screen.getByRole("button", { name: /Transferable/ })).toHaveTextContent("~");
      const boxed = Array.from(container.querySelectorAll(".qmark-none")).map((el) => el.parentElement!.textContent);
      expect(boxed).toEqual(["Not compared A line nobody graded."]);
      for (const t of ["We run projects.", "“I am the person Acme Corp is looking for.”", "Apply now."]) expect(container.textContent).toContain(t);
    });
  });

  describe("the posting's duties (docs/PRD.md 20.8)", () => {
    const TEXT5 = [
      "Key Responsibilities:",
      "- Lead the project office",
      "- Report progress to the board each quarter",
      "- Keep the lights on",
      "Required Qualifications:",
      "- Five years of project management",
      "- Strong written communication",
    ].join("\n");
    const duty = (index: number, item: string, cls: "P" | "T" | "U", from: { id: string; text: string }[] = []) =>
      ({ index, item, class: cls, why: `why duty ${index}`, by: "analyst" as const, owner_why: "", from, group: "Key Responsibilities" });
    const duties = [duty(0, "Lead the project office", "P", [{ id: "m1", text: "Ran the project office" }]), duty(1, "Report progress to the board each quarter", "T")];
    const one = [{ index: 0, item: "Five years of project management", class: "P" as const, why: "", by: "analyst" as const, owner_why: "", from: [] }];
    const withDuties = (extra = {}) => ({ correct, requirements: one, preferred: [], responsibilities: duties, ...extra });

    it("puts the mark of its class on each graded duty line, in place of its bullet", () => {
      const { container } = render(<MarkedPosting text={TEXT5} graded={withDuties()} />);
      const proven = screen.getAllByRole("button", { name: /Proven: show why/ }).map((b) => b.parentElement!.textContent);
      expect(proven).toContain("✓Proven: show why Lead the project office");
      const report = screen.getByRole("button", { name: /Transferable: show why/ }).parentElement!;
      expect(report.textContent).toBe("~Transferable: show why Report progress to the board each quarter");
      expect(container.textContent).not.toContain("- Lead the project");
    });

    it("opens a duty's reason and master lines in place, read-only", () => {
      render(<MarkedPosting text={TEXT5} graded={withDuties()} />);
      const mark = screen.getAllByRole("button", { name: /Proven: show why/ }).find((b) => b.parentElement!.textContent!.includes("Lead the project"))!;
      fireEvent.click(mark);
      expect(screen.getByText("why duty 0")).toBeInTheDocument();
      expect(screen.getByText(/Ran the project office/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Change the grade" })).toBeNull();
    });

    it("leaves a duty line that was not graded as it is: plain text, no box, no mark", () => {
      const { container } = render(<MarkedPosting text={TEXT5} graded={withDuties()} />);
      const plain = Array.from(container.querySelectorAll(".posting-line")).map((el) => el.textContent);
      expect(plain).toContain("- Keep the lights on");
      expect(plain).toContain("Key Responsibilities:");
      const boxed = Array.from(container.querySelectorAll(".qmark-none")).map((el) => el.parentElement!.textContent);
      expect(boxed).toEqual(["Not compared Strong written communication"]);
    });

    it("boxes no sibling of a graded duty under a heading the page does not recognise", () => {
      const odd = ["A day in the life:", "- Lead the project office", "- Keep the lights on"].join("\n");
      const { container } = render(<MarkedPosting text={odd} graded={withDuties({ requirements: [] })} />);
      expect(screen.getByRole("button", { name: /Proven: show why/ }).parentElement!.textContent).toContain("Lead the project office");
      expect(container.querySelectorAll(".qmark-none")).toHaveLength(0);
      expect(Array.from(container.querySelectorAll(".posting-line")).map((el) => el.textContent)).toContain("- Keep the lights on");
    });

    it("marks a posting whose only graded items are duties", () => {
      render(<MarkedPosting text={TEXT5} graded={{ correct, requirements: [], preferred: [], responsibilities: duties }} />);
      expect(screen.getAllByRole("button")).toHaveLength(2);
    });

    it("leaves a line a qualification holds to that qualification", () => {
      const twin = [duty(0, "Five years of project management", "U")];
      render(<MarkedPosting text={TEXT5} graded={withDuties({ responsibilities: twin })} />);
      expect(screen.getByRole("button", { name: /Proven: show why/ }).parentElement!.textContent).toBe("✓Proven: show why Five years of project management");
      expect(screen.queryByRole("button", { name: /Unproven/ })).toBeNull();
    });
  });
});
