import { describe, expect, it } from "vitest";
import { ratio, type Theme } from "../contrast";

// Search Settings is wholly in the Focus Inbox design: its stylesheet and the dialog and field pieces it shares name only
// Focus tokens, the words and marks it draws on washes and chips hold their contrast in both token sets, and none of its
// components still carries a class of the listings desk. Nothing here renders: it reads the files the app ships.

const files = import.meta.glob(["./tokens.css", "./search.css", "./parts.css"], { query: "?raw", import: "default", eager: true }) as Record<string, string>;
const tokens = files["./tokens.css"];

function read(block: string): Theme {
  return Object.fromEntries([...block.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6}|var\(--[\w-]+\))\s*;/g)].map((m) => [m[1], m[2]]));
}
const day = read(tokens.match(/^:root\s*\{([\s\S]*?)\n\}/m)![1]);
const night: Theme = { ...day, ...read(tokens.match(/@media \(prefers-color-scheme: dark\)\s*\{\s*:root\s*\{([\s\S]*?)\n {2}\}/)![1]) };

describe("Search Settings' style", () => {
  it.each(["./search.css", "./parts.css"])("%s names only Focus Inbox tokens: no legacy token, no hard-coded colour", (name) => {
    const body = files[name].replace(/\/\*[\s\S]*?\*\//g, "");
    const names = [...body.matchAll(/var\(--([\w-]+)/g)].map((m) => m[1]);
    expect(names.length).toBeGreaterThan(0);
    expect(names.filter((n) => !n.startsWith("fi-"))).toEqual([]);
    expect(body).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
  });

  it("re-skins no legacy class under .fi-ss", () => {
    const body = files["./search.css"].replace(/\/\*[\s\S]*?\*\//g, "");
    expect(body).not.toMatch(/\.fi-ss\s+\.(btn|spec-|tag|edit-|set|ask-|gate-|score-|suggestion-)/);
  });

  it.each([["daylight", day], ["night", night]] as [string, Theme][])("holds its contrast in %s", (_n, theme) => {
    // Words on the sheet: the ink, the soft hints, the placeholder, a link, a stopped rule's Remove.
    expect(ratio(theme, "fi-ink", "fi-surface")).toBeGreaterThanOrEqual(4.5);
    for (const word of ["fi-soft", "fi-faint", "fi-blue-text", "fi-red-text", "fi-teal-text"]) expect(ratio(theme, word, "fi-surface"), word).toBeGreaterThanOrEqual(4.5);
    // Points chips of the suggestion band and a quiet badge; the worked example and its problem.
    expect(ratio(theme, "fi-teal-text", "fi-teal-wash")).toBeGreaterThanOrEqual(4.5);
    expect(ratio(theme, "fi-red-text", "fi-red-wash")).toBeGreaterThanOrEqual(4.5);
    expect(ratio(theme, "fi-soft", "fi-chip")).toBeGreaterThanOrEqual(4.5);
    expect(ratio(theme, "fi-ink", "fi-blue-wash")).toBeGreaterThanOrEqual(4.5);
    expect(ratio(theme, "fi-soft", "fi-blue-wash")).toBeGreaterThanOrEqual(4.5);
  });
});

// The components of Search Settings and the editors under it carry Focus classes (fi-*) and the shared utility classes only.
const sources = import.meta.glob(["../../components/search/*.tsx", "../../components/profile/*.tsx", "../../components/primitives/JobProgress.tsx"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;
const LEGACY = /\b(btn|btn-[a-z]+|spec-input|spec-number|inline-number|tag|edit-field|edit-tick|edit-hint|part-hint|gate-[a-z-]+|ask-[a-z-]+|set|set-[a-z-]+|grade-line|grade-requirements|suggestion-[a-z-]+|example-[a-z-]+|check-result|small|muted|visually-hidden|listing)\b/;

describe("Search Settings' components", () => {
  it("name no class of the listings desk", () => {
    const found: string[] = [];
    for (const [file, text] of Object.entries(sources)) {
      if (file.endsWith(".test.tsx")) continue;
      for (const m of text.matchAll(/className=(?:"([^"]*)"|\{`([^`]*)`\})/g)) {
        const classes = (m[1] ?? m[2]).replace(/\$\{[^}]*\}/g, " ").split(/\s+/).filter(Boolean);
        for (const c of classes) if (!c.startsWith("fi-") && !c.startsWith("is-") && LEGACY.test(c)) found.push(`${file.split("/").pop()}: ${c}`);
      }
    }
    expect(found).toEqual([]);
  });
});
