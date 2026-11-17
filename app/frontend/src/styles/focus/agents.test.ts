import { describe, expect, it } from "vitest";
import { day, night, ratio, type Theme } from "../contrast";

// Agent Settings is wholly in the Focus Inbox design: the stylesheets it is drawn with (and the one it shares with the
// process view) name only Focus tokens, its components carry no class of the listings desk, and what it draws on washes,
// chips and ink fills holds its contrast in both token sets. Nothing here renders: it reads the files the app ships.

const css = import.meta.glob(["./agents.css", "./trail.css", "./how-they-work.css", "./missions.css", "./process-view.css", "./marks.css", "./base.css"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

describe("Agent Settings' style", () => {
  it.each(Object.keys(css))("%s names only Focus Inbox tokens, and hard-codes no colour", (name) => {
    const body = css[name].replace(/\/\*[\s\S]*?\*\//g, "");
    const names = [...body.matchAll(/var\(--([\w-]+)/g)].map((m) => m[1]);
    expect(names.length).toBeGreaterThan(0);
    // The few local properties a stylesheet sets for its own rules (a column template, an ink pair, the package's own, the size the process view gives a step number to fit its node).
    const local = new Set(["cols", "qmark-ink", "qmark-wash", "pv-edge", "pv-start", "pv-start-ink", "pv-decision", "pv-decision-ink", "pv-end", "pw-num-fit", "pw-num-w"]);
    expect(names.filter((n) => !n.startsWith("fi-") && !local.has(n))).toEqual([]);
    expect(body).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
  });

  it("answers every name the process-view package reads, in Focus tokens, so nothing falls back to its dark defaults", () => {
    const pv = css["./process-view.css"];
    for (const name of ["ink", "text", "soft", "muted", "surface", "card", "ground", "hover", "line", "line-2", "focus", "porter", "porter-2", "porter-wash", "done", "done-wash", "radius-lg", "space-3"]) {
      expect(pv, `--${name}`).toMatch(new RegExp(`--${name}:\\s*var\\(--fi-`));
    }
  });
});

// The components of Agent Settings carry Focus classes (fi-*) and their own domain classes only.
const sources = import.meta.glob(["../../components/agents/*.tsx", "../../components/missions/*.tsx", "../../components/trail/*.tsx", "../../components/primitives/StateWords.tsx", "../../components/role/*.tsx"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;
const LEGACY = /^(btn|btn-[a-z]+|spec-input|tag|small|muted|visually-hidden|listing|listing-[a-z]+|link-inline|state-words|lamp-mark|band|band-[a-z]+|section|section-[a-z]+|subhead|tab-strip[a-z-]*|page-index[a-z-]*|field|sub|page-top)$/;

describe("Agent Settings' components", () => {
  it("name no class of the listings desk", () => {
    const found: string[] = [];
    for (const [file, text] of Object.entries(sources)) {
      if (file.endsWith(".test.tsx")) continue;
      for (const m of text.matchAll(/className=(?:"([^"]*)"|\{`([^`]*)`\})/g)) {
        const classes = (m[1] ?? m[2]).replace(/\$\{[^}]*\}/g, " ").split(/\s+/).filter(Boolean);
        for (const c of classes) if (LEGACY.test(c)) found.push(`${file.split("/").pop()}: ${c}`);
      }
    }
    expect(found).toEqual([]);
  });
});

describe.each([["daylight", day], ["night", night]] as [string, Theme][])("what Agent Settings draws holds its contrast in %s", (_n, theme) => {
  it("state badges and dots: each ink's words on its wash, the quiet words on a chip", () => {
    for (const ink of ["teal", "red", "sun", "blue"]) expect(ratio(theme, `fi-${ink}-text`, `fi-${ink}-wash`), ink).toBeGreaterThanOrEqual(4.5);
    expect(ratio(theme, "fi-soft", "fi-chip")).toBeGreaterThanOrEqual(4.5);
  });

  it("the open roster row, an open step and the version in use keep their words legible", () => {
    expect(ratio(theme, "fi-ink", "fi-selected")).toBeGreaterThanOrEqual(4.5);
    expect(ratio(theme, "fi-soft", "fi-selected")).toBeGreaterThanOrEqual(4.5);
    expect(ratio(theme, "fi-ink", "fi-teal-wash")).toBeGreaterThanOrEqual(4.5);
    expect(ratio(theme, "fi-soft", "fi-teal-wash")).toBeGreaterThanOrEqual(4.5);
  });

  it("the process diagram: the number or sign on a teal, blue, red or slate square, a line and a box edge on the sheet", () => {
    for (const fill of ["fi-teal", "fi-blue", "fi-red", "fi-soft"]) expect(ratio(theme, "fi-on-dot", fill), fill).toBeGreaterThanOrEqual(4.5);
    expect(ratio(theme, "fi-ink", "fi-surface")).toBeGreaterThanOrEqual(4.5);
    expect(ratio(theme, "fi-field", "fi-surface"), "a line").toBeGreaterThanOrEqual(3);
    expect(ratio(theme, "fi-blue", "fi-surface"), "the run's place").toBeGreaterThanOrEqual(3);
    expect(ratio(theme, "fi-teal", "fi-surface"), "a step passed").toBeGreaterThanOrEqual(3);
  });

  it("the cost bars are seen against the sheet at 3:1", () => {
    expect(ratio(theme, "fi-field", "fi-surface")).toBeGreaterThanOrEqual(3);
    expect(ratio(theme, "fi-blue", "fi-surface")).toBeGreaterThanOrEqual(3);
  });

  it("the grade marks on a posting: each ink's words on its wash", () => {
    for (const ink of ["teal", "sun", "red"]) expect(ratio(theme, `fi-${ink}-text`, `fi-${ink}-wash`), ink).toBeGreaterThanOrEqual(4.5);
  });
});
