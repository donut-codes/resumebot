// A job posting's text read as sections. Pure: the same text gives the same sections every time.

/** Headings a posting is usually written under ("Responsibilities:", "What you'll bring"), at the start of a line or
 * after a sentence. */
export const HEADINGS = /(?:^|(?<=[.!?]\s))((?:About (?:the|this|us|the role|the team|you)|The role|Role|Responsibilities|Key responsibilities|What you(?:'|’)ll do|What you will do|Duties|Required|Requirements|Required qualifications|Minimum qualifications|Basic qualifications|Qualifications|Preferred|Preferred qualifications|Nice to have|What you(?:'|’)ll bring|Who you are|Skills|Benefits|Compensation|Pay|Salary|Location|Why join us)[^:\n]{0,40}):\s*/gim;

export type PostingBlock = { kind: "p"; text: string } | { kind: "ul"; items: string[] };
/** `blocks` is the section in the posting's own order; `paragraphs` and `items` are the same lines grouped by kind.
 * Show `blocks`: paragraphs then items would move a list below the text that followed it. */
export type PostingSection = { heading: string | null; paragraphs: string[]; items: string[]; blocks: PostingBlock[] };

/** A posting's text as sections: a heading with its paragraph, or with a list when the paragraph is a run of items
 * split by semicolons or bullet marks. Read the same way every time, nothing rewritten. */
export function postingSections(text: string): PostingSection[] {
  const marked = text.replace(/\r/g, "").replace(HEADINGS, "\n\u0000$1\u0000\n");
  const out: PostingSection[] = [];
  let current: PostingSection = { heading: null, paragraphs: [], items: [], blocks: [] };
  const addItems = (items: string[]) => {
    current.items.push(...items);
    const last = current.blocks[current.blocks.length - 1];
    if (last?.kind === "ul") last.items.push(...items);
    else current.blocks.push({ kind: "ul", items: [...items] });
  };
  for (const raw of marked.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const h = line.match(/^\u0000(.*)\u0000$/);
    if (h) {
      if (current.heading || current.paragraphs.length || current.items.length) out.push(current);
      current = { heading: h[1].trim(), paragraphs: [], items: [], blocks: [] };
      continue;
    }
    const bullet = line.match(/^[-•*·▪]\s*(.*)$/);
    if (bullet) {
      addItems([bullet[1]]);
    } else if (current.heading && line.split(";").length >= 3) {
      addItems(line.split(";").map((x) => x.trim().replace(/\.$/, "")).filter(Boolean));
    } else {
      current.paragraphs.push(line);
      current.blocks.push({ kind: "p", text: line });
    }
  }
  if (current.heading || current.paragraphs.length || current.items.length) out.push(current);
  return out;
}
