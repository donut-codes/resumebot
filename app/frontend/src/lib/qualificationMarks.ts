// Where a role's graded qualifications and responsibilities sit in its posting text (docs/PRD.md 20.8). Pure code, no
// model: a posting line takes the mark of the graded item it resembles, by text with case, punctuation, bullets and
// spacing set aside.
import type { RequirementRow } from "../api/types/me";
import { HEADINGS } from "./postingSections";

export type Kind = "required" | "preferred";

/** The text as compared: lower case, letters and digits only, one space between words. */
export function normalizeLine(s: string): string {
  return s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

/** A section's heading says which graded items it holds. Preferred is tested first: "Preferred Qualifications" is both. */
export function sectionKind(heading: string | null): Kind | null {
  if (!heading) return null;
  if (/preferred|nice to have|bonus|a plus|desired/i.test(heading)) return "preferred";
  if (/qualif|required|requirement|minimum|basic|must have|what you(?:'|’)ll bring|who you are|skills/i.test(heading)) return "required";
  return null;
}

/** A section's heading says it lists the posting's duties (the analyst's `responsibilities`, which carry no line numbers).
 * Only a heading that is not a qualification part's counts. */
export function isDutiesHeading(heading: string | null): boolean {
  return !!heading && sectionKind(heading) === null && /responsibilit|duties|what you(?:'|’)ll do|what you will do/i.test(heading);
}

const words = (s: string) => normalizeLine(s).split(" ").filter(Boolean).map((w) => (w.length > 3 ? w.replace(/s$/, "") : w));

/** How alike a line and an item are, 0 to 1: 1 for the same words, a high score when one holds the other, else the share
 * of words they have in common (overlap coefficient). */
export function likeness(line: string, item: string): number {
  const a = words(line);
  const b = words(item);
  if (a.length === 0 || b.length === 0) return 0;
  const sa = a.join(" ");
  const sb = b.join(" ");
  if (sa === sb) return 1;
  const [short, long] = a.length <= b.length ? [sa, sb] : [sb, sa];
  if (Math.min(a.length, b.length) >= 3 && ` ${long} `.includes(` ${short} `)) return 0.9;
  const set = new Set(b);
  const shared = new Set(a.filter((w) => set.has(w))).size;
  return (2 * shared) / (new Set(a).size + set.size);
}

/** At or above this a line and an item are the same requirement, said a little differently. */
export const MATCH_AT = 0.7;

/** At or above this a line outside its kind's own section (or under a heading the page does not recognise) is still the
 * item's line: the same words, one holding the other, or nearly. */
export const STRONG_AT = 0.75;

/** One line of the posting as written, with the kind of section it sits in. `text` is the line as compared (its bullet
 * and any heading before it set aside); `raw` is the line as written; `shown` is it less its bullet, which a mark replaces. */
export interface PostingLine {
  /** The block (split at blank lines) the line sits in. */
  block: number;
  raw: string;
  text: string;
  shown: string;
  kind: Kind | null;
  /** The line sits in a section of the posting's duties (`isDutiesHeading`). Such a section is no requirement part:
   * `kind` stays null there. */
  duty: boolean;
}

/** A line that only names a part ("Education:", "Required Qualifications:"): it is not compared, so it gets no marker. */
export function isHeadingLine(line: PostingLine): boolean {
  return line.text.trim() === "" || /:$/.test(line.shown) && line.shown.length <= 60;
}

/** The posting's blocks (split at blank lines) as lines, in the posting's own order, each knowing its section's kind. */
export function postingBlocks(text: string): PostingLine[][] {
  let kind: Kind | null = null;
  let duty = false;
  return text.replace(/\r/g, "").split(/\n\s*\n/).map((block) => block.trim()).filter(Boolean).map((block, blockAt) =>
    block.split("\n").map((l) => l.trim()).filter(Boolean).map((raw) => {
      const shown = raw.replace(/^[-•*·▪]\s*/, "");
      let body = shown;
      const heads = [...shown.matchAll(new RegExp(HEADINGS.source, HEADINGS.flags))];
      const last = heads[heads.length - 1];
      if (last) {
        kind = sectionKind(last[1]);
        duty = isDutiesHeading(last[1]);
        body = shown.slice(last.index + last[0].length);
      }
      return { block: blockAt, raw, text: body, shown, kind, duty };
    }));
}

const spaced = (s: string) => s.replace(/\s+/g, " ").trim();

/** Items that answer a numbered line carry that line's own words (docs/PRD.md 26.12): each takes the first free line that
 * has exactly those words, or that ends with them after its heading ("Skills: ..."), in any section; no likeness test. */
function placeLineItems(lines: PostingLine[], rows: RequirementRow[], lineRows: Map<number, RequirementRow>): void {
  const byLine = [...rows].filter((r) => r.line != null).sort((a, b) => (a.line ?? 0) - (b.line ?? 0));
  for (const row of byLine) {
    const item = spaced(row.item);
    if (!item) continue;
    const free = (test: (l: PostingLine) => boolean) => lines.findIndex((l, at) => !lineRows.has(at) && test(l));
    const at = free((l) => spaced(l.shown) === item || spaced(l.text) === item);
    const found = at >= 0 ? at : free((l) => spaced(l.shown).endsWith(` ${item}`));
    if (found >= 0) lineRows.set(found, row);
  }
}

/** One item to be placed by likeness, and whether a line sits in the part the item belongs to (such a line matches at
 * `MATCH_AT`; any other only as a strong match, `STRONG_AT`). */
interface Candidate {
  row: RequirementRow;
  own: (line: PostingLine) => boolean;
}

/** Match each candidate to at most one line it resembles, best match first, a line in the item's own part winning a tie.
 * A line holding a mark in `lineRows` is not available; every match is added to it. A line takes one item. */
function placeByLikeness(lines: PostingLine[], candidates: Candidate[], lineRows: Map<number, RequirementRow>): void {
  const pairs: { at: number; row: RequirementRow; score: number; own: boolean; order: number }[] = [];
  let order = 0;
  for (const { row, own: inPart } of candidates) {
    lines.forEach((line, at) => {
      if (isHeadingLine(line)) return;
      const own = inPart(line);
      const score = likeness(line.text, row.item);
      if (score >= (own ? MATCH_AT : STRONG_AT)) pairs.push({ at, row, score, own, order: order++ });
    });
  }
  pairs.sort((x, y) => y.score - x.score || Number(y.own) - Number(x.own) || x.order - y.order);
  const taken = new Set<RequirementRow>();
  for (const p of pairs) {
    if (lineRows.has(p.at) || taken.has(p.row)) continue;
    lineRows.set(p.at, p.row);
    taken.add(p.row);
  }
}

/** Match each graded item to at most one line: an item that answers a numbered line by its exact words, the others by
 * likeness to a line, best match first, a line in a section of the item's own kind winning a tie; a line in any other
 * place counts only as a strong match (`STRONG_AT`), since a posting may title its requirements in words the page does
 * not know. A line takes one item. `lines` is every line of the posting in order; the result maps a line's position to
 * its item. */
export function placeMarks(lines: PostingLine[], required: RequirementRow[], preferred: RequirementRow[]): Map<number, RequirementRow> {
  const lineRows = new Map<number, RequirementRow>();
  placeLineItems(lines, [...required, ...preferred], lineRows);
  required = required.filter((r) => r.line == null);
  preferred = preferred.filter((r) => r.line == null);
  const rowsOf: Record<Kind, RequirementRow[]> = { required, preferred };
  const candidates = (["required", "preferred"] as const).flatMap((kind) =>
    rowsOf[kind].map((row): Candidate => ({ row, own: (line) => line.kind === kind })));
  placeByLikeness(lines, candidates, lineRows);
  return lineRows;
}

/** Match each graded duty (the analyst's `responsibilities`, docs/PRD.md 20.8) to at most one line, the same way as an
 * item with no line number: by text, a line in the posting's duties section at `MATCH_AT`, any other only as a strong
 * match, each duty marking at most one line and a line taking one duty. A duty carries no line number, so there is no
 * exact-words step. `marked` is what the qualifications already took (`placeMarks`): a line marked there stays that
 * item's. The result holds only the duties' lines. */
export function placeDuties(lines: PostingLine[], duties: RequirementRow[], marked: ReadonlyMap<number, RequirementRow>): Map<number, RequirementRow> {
  const placed = new Map(marked);
  placeByLikeness(lines, duties.map((row): Candidate => ({ row, own: (line) => line.duty })), placed);
  return new Map([...placed].filter(([at]) => !marked.has(at)));
}

const bulleted = (l: PostingLine) => l.raw !== l.shown;

/** The positions of the lines that are in a requirement part: every line of a section the page recognises as one, and the
 * bullets of the run a marked line sits in when the posting titled that run in words the page does not recognise (the
 * marked line is a strong match of a graded item, so the run is its list). A marked line that is no bullet is its own
 * part. Only these lines may carry the neutral "not compared" box. */
export function requirementPartLines(lines: PostingLine[], lineRows: Map<number, RequirementRow>): Set<number> {
  const part = new Set<number>();
  lines.forEach((l, at) => { if (l.kind !== null) part.add(at); });
  const sibling = (at: number, from: number) => {
    const l = lines[at];
    return l !== undefined && l.kind === null && bulleted(l) && l.block === lines[from].block && !isHeadingLine(l);
  };
  for (const at of lineRows.keys()) {
    if (lines[at].kind !== null) continue;
    part.add(at);
    if (!bulleted(lines[at])) continue;
    for (const step of [-1, 1]) {
      for (let k = at + step; sibling(k, at); k += step) part.add(k);
    }
  }
  return part;
}
