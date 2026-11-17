import { render } from "@testing-library/react";
import BotMark from "./BotMark";

function fills(container: HTMLElement): string[] {
  return [...container.querySelectorAll("rect")].map((r) => r.getAttribute("fill") ?? r.getAttribute("class") ?? "");
}

describe("BotMark", () => {
  it("is plain resumebot without an outfit, and every agent is resumebot in disguise, lamp and all", () => {
    const plain = fills(render(<BotMark />).container);
    expect(plain).not.toContain("var(--fi-bot-khaki)");
    for (const [outfit, colour] of [["scout", "var(--fi-bot-khaki)"], ["analyst", "var(--fi-bot-tweed)"], ["writer", "var(--fi-bot-beret)"]] as const) {
      const { container } = render(<BotMark outfit={outfit} active />);
      const got = fills(container);
      expect(got).toContain(colour);
      expect(got.filter((f) => f === "bot-lamp")).toHaveLength(2); // the lamp pokes through the hat
      expect(got).toContain("var(--fi-signal)"); // the visor still shows: it fools nobody
      expect(container.querySelector("svg")).toHaveClass("is-active");
    }
  });
});
