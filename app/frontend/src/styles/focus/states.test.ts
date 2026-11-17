import { describe, expect, it } from "vitest";
import { ratio, type Theme } from "../contrast";

// The shared states (toast, loading, error banner, empty lines) are drawn only from the Focus Inbox tokens, and the words
// and marks they draw hold their contrast in both token sets. Nothing here renders: it reads the stylesheets the app ships.

const raw = (glob: Record<string, string>) => Object.values(glob)[0];
const tokens = raw(import.meta.glob("./tokens.css", { query: "?raw", import: "default", eager: true }) as Record<string, string>);
const states = raw(import.meta.glob("./states.css", { query: "?raw", import: "default", eager: true }) as Record<string, string>);

function read(block: string): Theme {
  return Object.fromEntries([...block.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6}|var\(--[\w-]+\))\s*;/g)].map((m) => [m[1], m[2]]));
}
const day = read(tokens.match(/^:root\s*\{([\s\S]*?)\n\}/m)![1]);
const night: Theme = { ...day, ...read(tokens.match(/@media \(prefers-color-scheme: dark\)\s*\{\s*:root\s*\{([\s\S]*?)\n {2}\}/)![1]) };

describe("the shared states' style", () => {
  it("names only Focus Inbox tokens: no legacy token, no hard-coded colour", () => {
    const body = states.replace(/\/\*[\s\S]*?\*\//g, "");
    const names = [...body.matchAll(/var\(--([\w-]+)/g)].map((m) => m[1]);
    expect(names.length).toBeGreaterThan(0);
    expect(names.filter((n) => !n.startsWith("fi-"))).toEqual([]);
    expect(body).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
  });

  it.each([["daylight", day], ["night", night]] as [string, Theme][])("holds its contrast in %s", (_n, theme) => {
    // Error words on their wash, and the "!" on its red disc.
    expect(ratio(theme, "fi-red-text", "fi-red-wash")).toBeGreaterThanOrEqual(4.5);
    expect(ratio(theme, "fi-on-dot", "fi-red")).toBeGreaterThanOrEqual(3);
    // Toast words on the sheet; loading and empty words on the ground and the sheet.
    expect(ratio(theme, "fi-ink", "fi-surface")).toBeGreaterThanOrEqual(4.5);
    for (const ground of ["fi-ground", "fi-surface"]) expect(ratio(theme, "fi-soft", ground)).toBeGreaterThanOrEqual(4.5);
  });
});
