import { describe, expect, it } from "vitest";

// The frame never scrolls and each column scrolls on its own (the design notes, the Focus Inbox: "the page never does"). jsdom
// does no layout, so this pins the rules that make it so: the bar and its page are exactly the window's height, the one grid
// row is as tall as the frame (an auto row grew to the tallest column, so no column overflowed and the wheel over the rail
// scrolled the page), and the rail, list and pane are scroll containers that hand no wheel or touch to the page. It reads
// the files the app ships.

const files = import.meta.glob(["./shell.css", "./focus.css", "./parts.css"], { query: "?raw", import: "default", eager: true }) as Record<string, string>;

type Rule = { media: string; selector: string; body: string };

/** Every rule of a stylesheet with the media query it sits in ("" at the top level). */
function rules(css: string): Rule[] {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const out: Rule[] = [];
  const walk = (src: string, media: string) => {
    let i = 0;
    while (i < src.length) {
      const open = src.indexOf("{", i);
      if (open < 0) break;
      const head = src.slice(i, open).trim();
      let depth = 1;
      let j = open + 1;
      while (j < src.length && depth > 0) {
        if (src[j] === "{") depth++;
        else if (src[j] === "}") depth--;
        j++;
      }
      const inner = src.slice(open + 1, j - 1);
      if (head.startsWith("@media")) walk(inner, head.slice(6).trim());
      else if (!head.startsWith("@")) out.push({ media, selector: head.replace(/\s+/g, " "), body: inner });
      i = j;
    }
  };
  walk(text, "");
  return out;
}

/** The declarations that apply to one selector in one media query, later ones over earlier. */
function declared(file: string, selector: string, media = ""): Record<string, string> {
  const found = rules(files[file])
    .filter((r) => r.media === media && r.selector.split(",").map((s) => s.trim()).includes(selector))
    .flatMap((r) => r.body.split(";").map((d) => d.trim()).filter(Boolean));
  return Object.fromEntries(found.map((d) => [d.slice(0, d.indexOf(":")).trim(), d.slice(d.indexOf(":") + 1).trim()]));
}

const DESKTOP = "(min-width: 761px)";
const PHONE = "(max-width: 760px)";

describe("the frame", () => {
  it("is exactly the window's height under the bar", () => {
    const shell = declared("./shell.css", ".app-shell");
    expect(shell["block-size"]).toBe("100dvh");
    expect(shell["display"]).toBe("flex");
    expect(shell["flex-direction"]).toBe("column");
    expect(declared("./shell.css", ".app-main")["min-block-size"]).toBe("0");
  });

  it("is never scrolled by the document on a desktop window; the main area scrolls a page that has no columns", () => {
    expect(declared("./shell.css", "html", DESKTOP)["overflow"]).toBe("hidden");
    expect(declared("./shell.css", "body", DESKTOP)["overflow"]).toBe("hidden");
    expect(declared("./shell.css", ".app-shell", DESKTOP)["overflow"]).toBe("hidden");
    expect(declared("./shell.css", ".app-main", DESKTOP)["overflow"]).toBe("auto");
    expect(declared("./shell.css", ".app-main", DESKTOP)["overscroll-behavior"]).toBe("contain");
  });

  it("lets a phone's page be as tall as its content", () => {
    expect(declared("./shell.css", ".app-shell", PHONE)["block-size"]).toBe("auto");
    expect(declared("./shell.css", "html", PHONE)["overflow"]).toBeUndefined();
    expect(declared("./focus.css", ".fi", PHONE)["block-size"]).toBe("auto");
  });
});

describe("the three columns", () => {
  it("sit in one grid row exactly as tall as the frame, so each can overflow on its own", () => {
    const fi = declared("./focus.css", ".fi");
    expect(fi["grid-template-rows"]).toBe("minmax(0, 1fr)");
    expect(fi["block-size"]).toBe("100%");
    expect(fi["min-block-size"]).toBe("0");
  });

  it.each([".fi-rail", ".fi-rows", ".fi-pane"])("%s scrolls on its own and keeps its wheel and touch from the page", (selector) => {
    const d = declared("./focus.css", selector);
    expect(d["overflow-y"]).toBe("auto");
    expect(d["overscroll-behavior"]).toBe("contain");
    expect(d["min-block-size"]).toBe("0");
  });

  it("holds the list's rows in a column that may shrink under the list's head", () => {
    const list = declared("./focus.css", ".fi-list");
    expect(list["display"]).toBe("flex");
    expect(list["flex-direction"]).toBe("column");
    expect(list["min-block-size"]).toBe("0");
    expect(declared("./focus.css", ".fi-rows")["flex"]).toBe("1");
  });

  it("keeps a dialog's wheel from the page behind it", () => {
    expect(declared("./parts.css", ".fi-dialog")["overscroll-behavior"]).toBe("contain");
  });
});

describe("the stage chips of a phone's sideways strip", () => {
  it("are padded the same on both sides, a nested one too: its desktop set-in from the edge must not leave it lopsided", () => {
    const pads = [".fi-stage", ".fi-stage.is-small", ".fi-stage.is-nested"].map((selector) => declared("./focus.css", selector, PHONE)["padding"]);
    expect(pads[0]).toBeTruthy();
    expect(new Set(pads).size).toBe(1);
    expect(declared("./focus.css", ".fi-stage.is-nested", PHONE)["padding-inline-start"]).toBeUndefined();
  });

  it("do not wrap and do not shrink in the strip, nested or not", () => {
    for (const selector of [".fi-stage", ".fi-stage.is-small", ".fi-stage.is-nested"]) {
      expect(declared("./focus.css", selector, PHONE)["white-space"], selector).toBe("nowrap");
      expect(declared("./focus.css", selector, PHONE)["flex"], selector).toBe("none");
    }
  });
});

describe("the narrower windows", () => {
  it("keeps the role a page over the list under 1100px, inside the frame", () => {
    const fi = declared("./focus.css", ".fi", "(max-width: 1100px)");
    expect(fi["grid-template-columns"]).toBe("220px minmax(0, 1fr)");
    expect(fi["position"]).toBe("relative");
    expect(declared("./focus.css", ".fi.is-on-role .fi-pane", "(max-width: 1100px)")["position"]).toBe("absolute");
  });

  it("makes the rail a sideways strip on a phone that scrolls on its own and leaves the page its vertical scroll", () => {
    const rail = declared("./focus.css", ".fi-rail", PHONE);
    expect(rail["flex-direction"]).toBe("row");
    expect(rail["overflow-x"]).toBe("auto");
    expect(rail["overscroll-behavior-x"]).toBe("contain");
    expect(declared("./focus.css", ".fi-rows", PHONE)["overflow"]).toBe("visible");
    expect(declared("./focus.css", ".fi-pane", PHONE)["overscroll-behavior"]).toBe("auto");
  });
});
