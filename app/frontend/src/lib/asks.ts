// How an ask of the owner is told in words.

const signed = (n: number) => (n > 0 ? `+${n}` : n < 0 ? `−${Math.abs(n)}` : "0");

/** An ask's points in a word or two: "+2", "−1", "noted" when it has none, "−1 each, down to −3" per item. */
export function pointsLabel(ask: { points?: unknown; per_item?: unknown; limit?: unknown }): string {
  const points = Number(ask.points ?? 0);
  if (!points) return "noted";
  if (!ask.per_item) return signed(points);
  const limit = ask.limit === null || ask.limit === undefined || ask.limit === "" ? "" : `, ${points < 0 ? "down" : "up"} to ${signed(Number(ask.limit))}`;
  return `${signed(points)} each${limit}`;
}

/** What a line with negative points says (docs/PRD.md 30.10): Prioritization only adds points, so it earns nothing. */
export const EARNS_NOTHING = "Earns nothing: Prioritization only adds points; use Hard pass to exclude.";
