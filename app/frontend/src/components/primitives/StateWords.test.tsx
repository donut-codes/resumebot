import { render, screen } from "@testing-library/react";
import StateWords, { TAG_MAX_WORDS, wordCount } from "./StateWords";

describe("StateWords", () => {
  it("counts words the way a person does", () => {
    expect(wordCount("Paused")).toBe(1);
    expect(wordCount("  On a   task ")).toBe(3);
    expect(wordCount("Waits: the scouting mission is paused")).toBe(6);
    expect(wordCount("")).toBe(0);
    expect(TAG_MAX_WORDS).toBe(3);
  });

  it("sets a state of three words or fewer as a badge, in its ink", () => {
    render(<StateWords words="On a task" ink="green" className="x" />);
    const el = screen.getByText("On a task");
    expect(el).toHaveClass("fi-state", "x");
    expect(el).not.toHaveClass("fi-state-words");
    expect(el).toHaveAttribute("data-ink", "green");
  });

  it("sets a longer state as ordinary words with its ink's dot, never as a badge", () => {
    const words = "Waits: the scouting mission is paused";
    render(<StateWords words={words} ink="saffron" className="x" />);
    const el = screen.getByText(words);
    expect(el).toHaveClass("fi-state-words", "x");
    expect(el).not.toHaveClass("fi-state");
    expect(el).toHaveAttribute("data-ink", "saffron");
    // The words are as given, not changed to capitals.
    expect(el.textContent).toBe(words);
  });

  it("a state with no ink has none, in either form", () => {
    render(
      <>
        <StateWords words="Off" />
        <StateWords words="Set aside after a long wait" />
      </>,
    );
    expect(screen.getByText("Off")).not.toHaveAttribute("data-ink");
    expect(screen.getByText("Set aside after a long wait")).not.toHaveAttribute("data-ink");
  });

  it("a short state that needs the owner carries the lamp; a long one carries its dot instead", () => {
    const { container } = render(
      <>
        <StateWords words="Needs you" ink="oxblood" lamp />
        <StateWords words="Needs you to sign in first" ink="oxblood" lamp />
      </>,
    );
    expect(screen.getByText("Needs you").querySelector(".fi-state-lamp")).not.toBeNull();
    expect(screen.getByText("Needs you to sign in first").querySelector(".fi-state-lamp")).toBeNull();
    expect(container.querySelectorAll(".fi-state-lamp")).toHaveLength(1);
  });
});
