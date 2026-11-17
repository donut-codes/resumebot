import { render } from "@testing-library/react";
import { MiniMarkdown } from "./miniMarkdown";

describe("MiniMarkdown", () => {
  it("shows headings, emphasis, lists and code as formatted text, with no markdown marks left", () => {
    const { container } = render(
      <MiniMarkdown text={"## Laying out\n\nThe *Global* rules and **strong** words with `code`.\n\n- one\n- two\n\n1. first\n2. second\n\n```\nresume:\n  a: b\n```"} />,
    );
    expect(container.querySelector("h4")?.textContent).toBe("Laying out");
    expect(container.querySelector("p em")?.textContent).toBe("Global");
    expect(container.querySelector("p strong")?.textContent).toBe("strong");
    expect(container.querySelector("p code")?.textContent).toBe("code");
    expect([...container.querySelectorAll("ul li")].map((l) => l.textContent)).toEqual(["one", "two"]);
    expect([...container.querySelectorAll("ol li")].map((l) => l.textContent)).toEqual(["first", "second"]);
    expect(container.querySelector("pre")?.textContent).toBe("resume:\n  a: b");
    expect(container.textContent).not.toMatch(/[#*`]/);
  });

  it("escapes everything: markup in the text is shown as text, never made into elements", () => {
    const { container } = render(<MiniMarkdown text={'A <img src=x onerror="boom()"> and <script>alert(1)</script>'} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<img src=x");
  });
});
