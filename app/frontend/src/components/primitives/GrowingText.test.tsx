import { act, fireEvent, render } from "@testing-library/react";
import { useState } from "react";
import GrowingText from "./GrowingText";

/** jsdom has no layout: a textarea has no width and no scroll height until the test says what the page would measure. */
function measure(el: HTMLElement, { offsetWidth, scrollHeight, clientHeight = 40, offsetHeight = 42 }: { offsetWidth: number; scrollHeight: number; clientHeight?: number; offsetHeight?: number }) {
  for (const [name, value] of Object.entries({ offsetWidth, scrollHeight, clientHeight, offsetHeight })) {
    Object.defineProperty(el, name, { configurable: true, value });
  }
}

/** A ResizeObserver the test can fire. */
let fire: () => void = () => {};
const original = globalThis.ResizeObserver;
beforeEach(() => {
  class Observer {
    constructor(private cb: () => void) {
      fire = () => this.cb();
    }
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = Observer;
});
afterEach(() => {
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = original;
});

function Box({ start = "Some words." }: { start?: string }) {
  const [text, setText] = useState(start);
  return <GrowingText aria-label="Words" value={text} onText={setText} minRows={2} />;
}

describe("GrowingText", () => {
  it("grows to its text, and the browser's own rows are the start", () => {
    const { getByLabelText } = render(<Box />);
    const box = getByLabelText("Words") as HTMLTextAreaElement;
    expect(box).toHaveAttribute("rows", "2");
    expect(box).toHaveClass("growing-text");
    measure(box, { offsetWidth: 300, scrollHeight: 90, clientHeight: 40, offsetHeight: 42 });
    fireEvent.change(box, { target: { value: "Some more words than before." } });
    expect(box.style.height).toBe("92px"); // the scroll height and the box's own border
  });

  it("is measured again when it is shown (a box in a hidden tab has no width to measure), with the height its text needs", () => {
    const { getByLabelText } = render(<Box start="A long text that wraps over several lines once the box is shown." />);
    const box = getByLabelText("Words") as HTMLTextAreaElement;

    // The tab is shown: the box has a width and its text wraps to 120px.
    measure(box, { offsetWidth: 400, scrollHeight: 120, clientHeight: 120, offsetHeight: 122 });
    act(() => fire());
    expect(box.style.height).toBe("122px");

    // The window narrows: the same text wraps to more lines.
    measure(box, { offsetWidth: 250, scrollHeight: 190, clientHeight: 120, offsetHeight: 122 });
    act(() => fire());
    expect(box.style.height).toBe("192px");

    // Only its height changing (its own doing) does not measure it again.
    measure(box, { offsetWidth: 250, scrollHeight: 10, clientHeight: 10, offsetHeight: 12 });
    act(() => fire());
    expect(box.style.height).toBe("192px");
  });

  it("keeps a one-line box to one line: Enter adds no break and a pasted break becomes a space", () => {
    const seen: string[] = [];
    const { getByLabelText } = render(<GrowingText aria-label="Line" value="" onText={(t) => seen.push(t)} oneLine />);
    const box = getByLabelText("Line");
    expect(fireEvent.keyDown(box, { key: "Enter" })).toBe(false);
    fireEvent.change(box, { target: { value: "one\n  two" } });
    expect(seen).toEqual(["one two"]);
  });
});
