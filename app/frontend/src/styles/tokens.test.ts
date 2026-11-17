import { describe, expect, it } from "vitest";
import { day, hex, night, ratio, tokens, type Theme } from "./contrast";

// The Focus Inbox's two palettes (focus/tokens.css) held to the contrast the design promises: words 4.5:1 on whatever they sit
// on, the edges a person has to find (a field, a focus ring, a diagram's line) 3:1. Daylight is the :root block; night is the
// dark block laid over it. Nothing here renders: it reads the stylesheet the app ships (./contrast). It took over from the
// listings desk's palette test when the last screen of that design moved.

const THEMES: [string, Theme][] = [["daylight", day], ["night", night]];

describe.each(THEMES)("the Focus Inbox palette, %s", (_name, theme) => {
  const GROUNDS = ["fi-ground", "fi-surface", "fi-rail"];

  it("reading ink is at least 4.5:1 on the ground, the sheet and the rail, the quietest (faint) included", () => {
    for (const ink of ["fi-ink", "fi-text", "fi-soft", "fi-faint"]) {
      for (const ground of GROUNDS) expect(ratio(theme, ink, ground), `--${ink} on --${ground}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("reading ink is at least 4.5:1 on the hover, selected and chip fills a row or a badge can take", () => {
    for (const fill of ["fi-hover", "fi-selected", "fi-chip"]) {
      for (const ink of ["fi-ink", "fi-text", "fi-soft"]) expect(ratio(theme, ink, fill), `--${ink} on --${fill}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("each state ink, as words, is at least 4.5:1 on the sheet, the ground and its own wash", () => {
    for (const ink of ["teal", "red", "blue", "sun"]) {
      for (const ground of ["fi-surface", "fi-ground", `fi-${ink}-wash`]) {
        expect(ratio(theme, `fi-${ink}-text`, ground), `--fi-${ink}-text on --${ground}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("ink and soft words are at least 4.5:1 on every state wash", () => {
    for (const ink of ["teal", "red", "blue", "sun"]) {
      for (const word of ["fi-ink", "fi-soft"]) expect(ratio(theme, word, `fi-${ink}-wash`), `--${word} on --fi-${ink}-wash`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("the words on a filled control or step are at least 4.5:1: the sun button, and a teal, blue, red or slate fill", () => {
    expect(ratio(theme, "fi-on-sun", "fi-sun")).toBeGreaterThanOrEqual(4.5);
    for (const fill of ["fi-teal", "fi-blue", "fi-red", "fi-soft"]) expect(ratio(theme, "fi-on-dot", fill), `--fi-on-dot on --${fill}`).toBeGreaterThanOrEqual(4.5);
  });

  it("a field's edge is at least 3:1 against the sheet it is drawn on, and the focus ring against the ground and the sheet", () => {
    expect(ratio(theme, "fi-field", "fi-surface")).toBeGreaterThanOrEqual(3);
    for (const around of ["fi-ground", "fi-surface"]) expect(ratio(theme, "fi-focus", around), `--fi-focus on --${around}`).toBeGreaterThanOrEqual(3);
  });

  it("a hairline is a visible step off the sheet", () => {
    expect(ratio(theme, "fi-line", "fi-surface")).toBeGreaterThanOrEqual(1.1);
  });

  it("the caption of a step in the diagram (the sheet's colour on ink) is at least 4.5:1", () => {
    expect(ratio(theme, "fi-surface", "fi-ink")).toBeGreaterThanOrEqual(4.5);
  });
});

describe("night is its own tuned set, not an inversion of daylight", () => {
  it("has a near-black rail darker than its ground, and sheets lighter than the ground", () => {
    const lum = (name: string) => {
      const c = hex(night, name);
      return [1, 3, 5].reduce((n, i) => n + parseInt(c.slice(i, i + 2), 16), 0);
    };
    expect(lum("fi-rail")).toBeLessThan(lum("fi-ground"));
    expect(lum("fi-surface")).toBeGreaterThan(lum("fi-ground"));
    // Daylight runs the other way: the rail is a step darker than the ground, the sheet is the lightest.
    expect(hex(day, "fi-surface")).toBe("#ffffff");
  });

  it("keeps the sun the same colour by day and by night", () => {
    expect(hex(night, "fi-sun")).toBe(hex(day, "fi-sun"));
  });
});

describe("the type, the target and the shape", () => {
  it("is Archivo, with a body of at least 1rem and a smallest size of 0.78rem kept for badges and rail heads", () => {
    expect(tokens).toMatch(/--fi-font:\s*"Archivo Variable"/);
    expect(tokens).toMatch(/--fi-size-base:\s*1rem/);
    expect(tokens).toMatch(/--fi-size-xs:\s*0\.78rem/);
    expect(tokens).toMatch(/--fi-size-s:\s*0\.875rem/);
  });

  it("makes every target 44px and rounds its corners from 6px to 14px", () => {
    expect(tokens).toMatch(/--fi-target:\s*44px/);
    const radii = [...tokens.matchAll(/--fi-r-(?:s|m|l|xl):\s*(\d+)px/g)].map((m) => Number(m[1]));
    expect(radii).toEqual([6, 9, 12, 14]);
  });
});
