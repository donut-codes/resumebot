import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import { OVERVIEW } from "./how.fixture";
import { ProcessView, comparePathNumbers, embedCondition, FLOW_MIN_ZOOM, isDefaultPath, seedForWideBoard } from "process-view";
import { harnessToProcess } from "./harnessProcess";

// The search for roles, as How they work feeds it: a start, three tasks, a call to a sub-process, one question and an end.
const graph = harnessToProcess(OVERVIEW.processes[0]);

function renderView(detail = false) {
  // live=false: settle the layout once, so no timer is left running under the test.
  return render(
    <ProcessView
      steps={graph.steps}
      edges={graph.edges}
      live={false}
      renderDetail={detail ? (s) => <p>More about {s.label}</p> : undefined}
    />,
  );
}

function titles(): string[] {
  return Array.from(document.querySelectorAll(".step-list .step-title-editable")).map((n) => (n.textContent ?? "").replace(/^(End of this path|Ends the whole process): /, ""));
}

function numbers(): string[] {
  return Array.from(document.querySelectorAll(".step-list .step-number")).map((n) => (n.firstChild?.textContent ?? "").trim());
}

describe("ProcessView", () => {
  it("has a Process flow section with Full screen, and a Steps section with Reading and Show details", () => {
    renderView();
    expect(screen.getByRole("heading", { level: 2, name: "Process flow" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Steps" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Full screen" })).toBeInTheDocument();
    expect(screen.getByText("Reading")).toBeInTheDocument();
    expect(screen.getByLabelText("Show details")).toBeInTheDocument();
    // Edit affordances don't exist here.
    expect(screen.queryByText(/Edit in builder|Export BPMN|Archive|Verify/)).toBeNull();
  });

  it("draws one circle per step in the Living graph", () => {
    renderView();
    expect(document.querySelectorAll(".react-flow__node")).toHaveLength(graph.steps.length);
    expect(document.querySelectorAll(".pw-living-node")).toHaveLength(graph.steps.length);
    // Start is the 'event' kind (green), the end is 'terminal' (grey).
    expect(document.querySelectorAll(".pw-block-event")).toHaveLength(1);
    expect(document.querySelectorAll(".pw-block-terminal")).toHaveLength(1);
  });

  it("toggles Full screen", () => {
    renderView();
    const board = document.querySelector(".read-flow-board")!;
    expect(board).not.toHaveClass("read-flow-board-expanded");
    fireEvent.click(screen.getByRole("button", { name: "Full screen" }));
    expect(board).toHaveClass("read-flow-board-expanded");
    fireEvent.click(screen.getByRole("button", { name: "Collapse" }));
    expect(board).not.toHaveClass("read-flow-board-expanded");
  });

  it("lists every step exactly once in the Steps list", () => {
    renderView();
    const listed = titles();
    expect(listed.slice().sort()).toEqual(
      [
        "A search starts",
        "Read the gates",
        "The scout searches the boards",
        "Screen each role",
        "Is any role worth a look?",
        "Show you the roles",
        "Search done",
      ].sort(),
    );
    expect(new Set(listed).size).toBe(listed.length);
    expect(listed).toHaveLength(graph.steps.length);
  });

  it("numbers the main line 1, 2, 3 and the ends carry no number", () => {
    renderView();
    // Start 1, then each step down the main line; a decision takes a number like any step.
    expect(numbers().slice(0, 5)).toEqual(["1", "2", "3", "4", "5"]);
    // The end is where a path stops, not a step.
    expect(document.querySelectorAll(".step-number-end").length).toBeGreaterThanOrEqual(1);
  });

  it("gives each step a kind pill: Start, Task, Decision, Subprocess, End", () => {
    renderView();
    const pills = Array.from(document.querySelectorAll(".step-row .step-bpmn-badge")).map((p) => p.textContent);
    expect(pills.filter((p) => p === "Start")).toHaveLength(1);
    expect(pills.filter((p) => p === "Decision")).toHaveLength(1);
    expect(pills.filter((p) => p === "End")).toHaveLength(1);
    expect(pills.filter((p) => p === "Subprocess")).toHaveLength(1);
    expect(pills.filter((p) => p === "Task")).toHaveLength(3);
  });

  it("shows who does each step on its row when asked, and not otherwise", () => {
    const { unmount } = render(<ProcessView steps={graph.steps} edges={graph.edges} live={false} performerOnRows />);
    const actors = Array.from(document.querySelectorAll(".step-list .step-actor-line")).map((n) => n.textContent);
    expect(actors).toContain("Scout");
    expect(actors).toContain("You");
    unmount();
    render(<ProcessView steps={graph.steps} edges={graph.edges} live={false} />);
    expect(document.querySelectorAll(".step-actor-line")).toHaveLength(0);
  });

  it("opens a step's own detail from its row", () => {
    renderView(true);
    expect(screen.queryByText("More about Read the gates")).toBeNull();
    const row = document.getElementById("step-Open_run")!;
    fireEvent.click(within(row).getByRole("button", { name: /Expand details for Read the gates/ }));
    expect(screen.getByText("More about Read the gates")).toBeInTheDocument();
  });

  it("selects a step on click and marks the steps before and after it", () => {
    renderView();
    const row = document.getElementById("step-Scout_searches")!;
    fireEvent.click(row);
    expect(row).toHaveClass("step-row-highlighted");
    expect(document.querySelectorAll(".step-trace-arrow-up").length).toBeGreaterThan(0);
    expect(document.querySelectorAll(".step-trace-arrow-down").length).toBeGreaterThan(0);
    // Keys reach the view only when pressed inside it (process-view 8cdd383e), as after a click on a step.
    fireEvent.keyDown(document.querySelector(".process-view") as HTMLElement, { key: "Escape" });
    expect(row).not.toHaveClass("step-row-highlighted");
  });

  it("renders nothing for a process with no steps", () => {
    const { container } = render(<ProcessView steps={[]} edges={[]} live={false} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("the Process flow board's framing", () => {
  it("lets a tall graph be fitted below React Flow's default 0.5 zoom", () => {
    // A 23-step chain is ~1400px tall on a 600px board: it needs about 0.3. The default floor of 0.5
    // made fitView stop early and clip both ends.
    expect(FLOW_MIN_ZOOM).toBeLessThan(0.3);
  });

  it("turns a tall seed on its side for the wide board, and leaves a wide one alone", () => {
    const block = (id: string, x: number, y: number) => ({ id, position: { x, y }, data: {} });
    const column = [block("a", 0, 0), block("b", 0, 300), block("c", 0, 600)];
    const turned = seedForWideBoard(column);
    const xs = turned.map((n) => n.position.x);
    const ys = turned.map((n) => n.position.y);
    expect(Math.max(...xs) - Math.min(...xs)).toBeGreaterThan(Math.max(...ys) - Math.min(...ys));
    // Order along the chain is kept.
    expect(xs[0]).toBeLessThan(xs[1]);
    expect(xs[1]).toBeLessThan(xs[2]);

    const row = [block("a", 0, 0), block("b", 400, 0), block("c", 800, 0)];
    expect(seedForWideBoard(row)).toBe(row);
    expect(seedForWideBoard([])).toEqual([]);
  });
});

describe("the Process flow board's reveal and fit", () => {
  // jsdom has no layout: give the board a size, as a browser would.
  function sizeBoards(width: number, height: number) {
    return vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const sized = this.classList.contains("read-flow-board");
      return { x: 0, y: 0, left: 0, top: 0, right: sized ? width : 0, bottom: sized ? height : 0, width: sized ? width : 0, height: sized ? height : 0, toJSON() {} } as DOMRect;
    });
  }
  const viewportTransform = () => (document.querySelector(".react-flow__viewport") as HTMLElement).style.transform;

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("shows the board even when the tab never runs an animation frame (hidden or throttled)", () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    vi.stubGlobal("requestAnimationFrame", () => 1);
    vi.stubGlobal("cancelAnimationFrame", () => {});
    sizeBoards(1000, 600);
    renderView();
    const board = document.querySelector(".read-flow-board")!;
    expect(board).toHaveClass("pw-flow-framing");
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(board).not.toHaveClass("pw-flow-framing");
    // And it was fitted to the board, not left at React Flow's own start (no pan, no zoom).
    expect(viewportTransform()).not.toBe("translate(0px,0px) scale(1)");
  });

  it("fits the whole graph again to the board's new size on entering and leaving full screen", () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    vi.stubGlobal("requestAnimationFrame", () => 1);
    vi.stubGlobal("cancelAnimationFrame", () => {});
    const rect = sizeBoards(1000, 400);
    renderView();
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    const normal = viewportTransform();
    rect.mockRestore();
    sizeBoards(1000, 900);
    fireEvent.click(screen.getByRole("button", { name: "Full screen" }));
    const full = viewportTransform();
    expect(full).not.toBe(normal);
    // The same graph, centred in the taller board: its centre is the board's centre.
    sizeBoards(1000, 400);
    fireEvent.click(screen.getByRole("button", { name: "Collapse" }));
    expect(viewportTransform()).toBe(normal);
  });

  it("breaks a step's number after each dot and sizes it to its node, leaving its text as it was", () => {
    renderView();
    const nums = Array.from(document.querySelectorAll<HTMLElement>(".pw-living-node-num"));
    expect(nums.length).toBeGreaterThan(0);
    for (const n of nums) {
      expect(n.style.getPropertyValue("--pw-num-fit")).toMatch(/^\d+(\.\d+)?px$/);
      expect(n.querySelectorAll("wbr")).toHaveLength((n.textContent ?? "").split(".").length);
    }
  });
});

describe("the path helpers", () => {
  it("orders path numbers the way a reader counts", () => {
    expect(["10a", "3a", "2a"].sort(comparePathNumbers)).toEqual(["2a", "3a", "10a"]);
    expect(["4a.2b", "4a.10b", "4a.2a"].sort(comparePathNumbers)).toEqual(["4a.2a", "4a.2b", "4a.10b"]);
  });

  it("puts a path name inside a sentence and keeps an acronym's case", () => {
    expect(embedCondition("Tax relevant")).toBe("tax relevant");
    expect(embedCondition("SAP unavailable")).toBe("SAP unavailable");
    expect(isDefaultPath(" Otherwise ")).toBe(true);
    expect(isDefaultPath("Yes")).toBe(false);
  });
});
