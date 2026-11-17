import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Block, FactStrip, Field, ListHead, Menu, Pane, PaneHead, PillRow, Rail, RailEntry, RailGroup, Row, SavedLine, Sheet, Slider, Switch } from "./parts";

// The shared parts draw from props alone: each says what it shows, and calls back on the one thing it does.

describe("the shared parts", () => {
  it("Rail and RailEntry: the parts with their counts, the one waiting on you as a pill, the current one pressed", () => {
    const pick = vi.fn();
    render(
      <Rail label="Parts">
        <RailGroup>
          <RailEntry label="Needs you" count={3} yours current onPick={pick} />
        </RailGroup>
        <RailGroup label="Kept">
          <RailEntry label="Past" current={false} onPick={vi.fn()} />
        </RailGroup>
      </Rail>,
    );
    const rail = screen.getByRole("navigation", { name: "Parts" });
    const needs = within(rail).getByRole("button", { name: /Needs you/ });
    expect(needs).toHaveAttribute("aria-pressed", "true");
    expect(needs).toHaveClass("is-yours");
    expect(needs).toHaveTextContent("3");
    expect(within(rail).getByText("Kept")).toHaveClass("fi-rail-head");
    expect(within(rail).getByRole("button", { name: "Past" })).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(needs);
    expect(pick).toHaveBeenCalled();
  });

  it("RailEntry nested sits under the entry above it: the same entry, set in from the edge", () => {
    render(
      <Rail label="Parts">
        <RailGroup>
          <RailEntry label="Whole" current={false} onPick={vi.fn()} />
          <RailEntry label="Part" nested current={false} onPick={vi.fn()} />
        </RailGroup>
      </Rail>,
    );
    expect(screen.getByRole("button", { name: "Part" })).toHaveClass("is-nested");
    expect(screen.getByRole("button", { name: "Whole" })).not.toHaveClass("is-nested");
  });

  it("RailEntry with an address is a link: a plain click is onPick, a click that means a new window is left to the browser", () => {
    const pick = vi.fn();
    render(
      <Rail label="Parts">
        <RailGroup>
          <RailEntry label="One" href="#/one" current onPick={pick} />
          <RailEntry label="Two" href="#/two" current={false} onPick={pick} />
        </RailGroup>
      </Rail>,
    );
    const one = screen.getByRole("link", { name: "One" });
    expect(one).toHaveAttribute("href", "#/one");
    expect(one).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Two" })).not.toHaveAttribute("aria-current");
    fireEvent.click(screen.getByRole("link", { name: "Two" }));
    expect(pick).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("link", { name: "Two" }), { ctrlKey: true });
    expect(pick).toHaveBeenCalledTimes(1);
  });

  it("ListHead: the find field, the title and its one action", () => {
    const find = vi.fn();
    render(<ListHead find={{ value: "", onChange: find, placeholder: "Find", label: "Find things" }} title="Things" action={<button type="button">Do</button>} />);
    fireEvent.change(screen.getByRole("searchbox", { name: "Find things" }), { target: { value: "x" } });
    expect(find).toHaveBeenCalledWith("x");
    expect(screen.getByRole("heading", { level: 1, name: "Things" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Do" })).toBeInTheDocument();
  });

  it("Row: the whole row opens it; it says which one is open and which are quiet", () => {
    const open = vi.fn();
    render(
      <ul>
        <Row title="One" meta="meta" current quiet onOpen={open} end="2h" />
      </ul>,
    );
    const row = screen.getByRole("listitem");
    expect(row).toHaveAttribute("aria-current", "true");
    expect(row).toHaveClass("is-quiet");
    expect(row).toHaveTextContent("2h");
    fireEvent.click(screen.getByRole("button", { name: "One" }));
    expect(open).toHaveBeenCalled();
  });

  it("Pane, PaneHead, Sheet and FactStrip: the open thing, its head, its sheets and its facts", () => {
    const back = vi.fn();
    render(
      <Pane openKey="a" label="The thing" backLabel="Things" onBack={back}>
        <PaneHead title="The thing" meta={["Kind", null, "2h ago"]} />
        <FactStrip facts={[{ label: "Roles", value: "4", sub: "this week" }]} />
        <Sheet title="History" count={2}>
          <p>Lines</p>
        </Sheet>
      </Pane>,
    );
    const pane = screen.getByRole("region", { name: "The thing" });
    expect(within(pane).getByRole("heading", { level: 2, name: "The thing" })).toBeInTheDocument();
    expect(pane.querySelectorAll(".fi-head-meta > span")).toHaveLength(2);
    expect(within(pane).getByText("Roles").tagName).toBe("DT");
    expect(within(pane).getByText("this week")).toHaveClass("fi-facts-sub");
    expect(within(pane).getByRole("heading", { level: 3 })).toHaveTextContent("History2");
    fireEvent.click(within(pane).getByRole("button", { name: "Things" }));
    expect(back).toHaveBeenCalled();
  });

  it("Switch: says its name and state, and flips", () => {
    const change = vi.fn();
    render(<Switch checked={false} onChange={change} label="Watch" />);
    const sw = screen.getByRole("switch", { name: "Watch" });
    expect(sw).toHaveAttribute("aria-checked", "false");
    fireEvent.click(sw);
    expect(change).toHaveBeenCalledWith(true);
  });

  it("Switch with words: shows them inside the switch, so clicking them flips it too", () => {
    const change = vi.fn();
    render(<Switch checked onChange={change} label="Watch its job board" shown="Watch its job board" />);
    const sw = screen.getByRole("switch", { name: "Watch its job board" });
    expect(sw).toHaveClass("is-labelled");
    fireEvent.click(screen.getByText("Watch its job board"));
    expect(change).toHaveBeenCalledWith(false);
  });

  it("Field: saves itself when it loses focus with a changed value, and not otherwise; no cap on what is written", () => {
    const save = vi.fn();
    const long = "x".repeat(5000);
    render(<Field label="Title" value="Old" onSave={save} />);
    const box = screen.getByRole("textbox", { name: "Title" });
    fireEvent.focus(box);
    fireEvent.blur(box);
    expect(save).not.toHaveBeenCalled();
    fireEvent.focus(box);
    fireEvent.change(box, { target: { value: long } });
    fireEvent.blur(box);
    expect(save).toHaveBeenCalledWith(long);
    expect(box).not.toHaveAttribute("maxlength");
  });

  it("SavedLine: Saved with Undo, nothing when idle", () => {
    const undo = vi.fn();
    const { rerender } = render(<SavedLine state="saved" onUndo={undo} />);
    expect(screen.getByRole("status")).toHaveTextContent("Saved");
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(undo).toHaveBeenCalled();
    rerender(<SavedLine state="idle" />);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("Menu: opens under its button, picks one and closes", () => {
    const pick = vi.fn();
    render(<Menu label="Close" heading="Why close it?" items={[{ key: "a", label: "Wrong level" }]} onPick={pick} />);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(within(screen.getByRole("menu", { name: "Why close it?" })).getByRole("menuitem", { name: "Wrong level" }));
    expect(pick).toHaveBeenCalledWith("a");
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("Slider: moves freely and saves once, when let go", () => {
    const commit = vi.fn();
    render(<Slider label="Weight" value={50} ends={["Can I do it", "Does it fit"]} onCommit={commit} />);
    const slider = screen.getByRole("slider", { name: "Weight" });
    fireEvent.change(slider, { target: { value: "60" } });
    fireEvent.change(slider, { target: { value: "70" } });
    expect(commit).not.toHaveBeenCalled();
    fireEvent.pointerUp(slider);
    expect(commit).toHaveBeenCalledTimes(1);
    expect(commit).toHaveBeenCalledWith(70);
  });

  it("Block: a landmark named by its heading, the count read as part of the name, the state as a dot on the block", () => {
    render(
      <Block id="roster" title="Roster" count="3" ink="green" aside={<button type="button">Do</button>} hint="One line.">
        <p>rows</p>
      </Block>,
    );
    const region = screen.getByRole("region", { name: "Roster 3" });
    expect(region).toHaveAttribute("id", "roster");
    expect(region).toHaveAttribute("data-ink", "green");
    const heading = screen.getByRole("heading", { level: 2, name: "Roster 3" });
    expect(heading).toHaveAttribute("id", "roster-head");
    expect(region).toContainElement(screen.getByRole("button", { name: "Do" }));
    expect(region).toHaveTextContent("One line.");
    expect(region).toHaveTextContent("rows");
  });

  it("Block: a block inside a block is a third-level heading, and one that holds its own list takes no padding", () => {
    render(
      <Block id="inner" level={3} title="Inner" flush>
        <p>list</p>
      </Block>,
    );
    expect(screen.getByRole("heading", { level: 3, name: "Inner" })).toBeInTheDocument();
    expect(screen.getByText("list").parentElement).toHaveClass("fi-block-body", "is-flush");
  });

  it("PillRow with addresses is navigation: links, the current one marked, a plain click is onPick and a new-window click is left alone", () => {
    const pick = vi.fn();
    render(
      <PillRow
        label="Which process"
        tabs={[
          { key: "a", label: "One", current: true, href: "#/one", onPick: vi.fn() },
          { key: "b", label: "Two", current: false, href: "#/two", onPick: pick },
        ]}
      />,
    );
    const nav = screen.getByRole("navigation", { name: "Which process" });
    expect(nav).toHaveClass("fi-pills");
    expect(screen.getByRole("link", { name: "One" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Two" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "Two" })).toHaveAttribute("href", "#/two");
    fireEvent.click(screen.getByRole("link", { name: "Two" }), { ctrlKey: true });
    expect(pick).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("link", { name: "Two" }));
    expect(pick).toHaveBeenCalledTimes(1);
  });

  it("PillRow without addresses swaps in place: buttons that say which is pressed, in a lighter row when it is a sub-row", () => {
    const pick = vi.fn();
    render(<PillRow label="Sub-processes" sub tabs={[{ key: "a", label: "One", current: false, onPick: pick }, { key: "b", label: "Two", current: true, onPick: vi.fn() }]} />);
    expect(screen.getByRole("navigation", { name: "Sub-processes" })).toHaveClass("is-sub");
    expect(screen.getByRole("button", { name: "Two" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "One" }));
    expect(pick).toHaveBeenCalledTimes(1);
  });
});
