// A share kept as a fraction (0.85) and shown as a percent (85), moved by the decimal point and never by arithmetic, so the
// number kept is exactly the one the digits say: 57 is 0.57, never 0.5700000000000001, and 0.57 reads back as 57, never
// 56.99999999999999. The value stored and sent stays the fraction.

/** A plain decimal written as digits (one sign and one point at most) with its point moved `by` places; null when the text
 * is no plain decimal (empty, or written with an exponent). */
function movePoint(text: string, by: number): string | null {
  const m = /^(-?)(\d*)\.?(\d*)$/.exec(text.trim());
  if (!m || (m[2] === "" && m[3] === "")) return null;
  const [, sign, whole, part] = m;
  const digits = whole + part;
  const at = whole.length + by;
  let moved: string;
  if (at <= 0) moved = `0.${"0".repeat(-at)}${digits}`;
  else if (at >= digits.length) moved = digits + "0".repeat(at - digits.length);
  else moved = `${digits.slice(0, at)}.${digits.slice(at)}`;
  return sign + moved;
}

/** The fraction a percent typed as `text` stands for: "57" is 0.57, "62.5" is 0.625. NaN when it is not a number. */
export function fractionOf(text: string): number {
  if (text.trim() === "") return NaN;
  const moved = movePoint(text, -2);
  return moved === null ? Number(text) / 100 : Number(moved);
}

/** The percent a fraction stands for: 0.57 is 57, 0.625 is 62.5. */
export function percentOf(fraction: number): number {
  const moved = movePoint(String(fraction), 2);
  return moved === null ? fraction * 100 : Number(moved);
}
