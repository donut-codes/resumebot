// The two palettes of the Focus Inbox (styles/focus/tokens.css: daylight is the :root block; night is the
// prefers-color-scheme: dark block laid over it) and the WCAG contrast between two of their colours. Shared by the tests
// that hold the design to its contrast (tokens.test.ts, and the style tests of each screen). Nothing here renders: it
// reads the stylesheet the app ships.
export const tokens = Object.values(
  import.meta.glob("./focus/tokens.css", { query: "?raw", import: "default", eager: true }) as Record<string, string>,
)[0];

export type Theme = Record<string, string>;

function read(block: string): Theme {
  return Object.fromEntries([...block.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6}|var\(--[\w-]+\))\s*;/g)].map((m) => [m[1], m[2]]));
}

export const day = read(tokens.match(/^:root\s*\{([\s\S]*?)\n\}/m)![1]);
export const night: Theme = { ...day, ...read(tokens.match(/@media \(prefers-color-scheme: dark\)\s*\{\s*:root\s*\{([\s\S]*?)\n {2}\}/)![1]) };

/** A token's colour, following `var(--other)` aliases. */
export function hex(theme: Theme, name: string): string {
  let value = theme[name];
  for (let hops = 0; value && value.startsWith("var(") && hops < 8; hops++) value = theme[value.slice(6, -1)];
  if (!value || !value.startsWith("#")) throw new Error(`--${name} is not a colour in this theme`);
  return value;
}

/** The relative luminance of a #rrggbb colour (WCAG 2). */
export const light = (color: string) => {
  const [r, g, b] = [1, 3, 5]
    .map((i) => parseInt(color.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

/** The WCAG contrast ratio of two tokens in a theme. */
export function ratio(theme: Theme, a: string, b: string): number {
  const [hi, lo] = [light(hex(theme, a)), light(hex(theme, b))].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}
