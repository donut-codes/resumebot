// The small piece of markdown the writer's instruction file uses, shown as formatted text: headings, paragraphs, bulleted
// and numbered lists, fenced code, and *emphasis*, **strong** and `code` within a line. It builds React elements from the
// text, so everything is escaped; nothing in the text can become markup.

import { Fragment, type ReactNode } from "react";

const INLINE = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*)/g;

/** One line of text with its `code`, **strong** and *emphasis* made elements; the rest stays plain text. */
function inline(text: string): ReactNode[] {
  return text.split(INLINE).map((part, i) => {
    if (part.length > 2 && part.startsWith("`") && part.endsWith("`")) return <code key={i}>{part.slice(1, -1)}</code>;
    if (part.length > 4 && part.startsWith("**") && part.endsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (part.length > 2 && part.startsWith("*") && part.endsWith("*")) return <em key={i}>{part.slice(1, -1)}</em>;
    return <Fragment key={i}>{part}</Fragment>;
  });
}

const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^\s*[-*]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;
const FENCE = /^\s*```/;

/** Markdown text as elements. `headingLevel` is the heading the text's shallowest heading becomes, so it nests under the
 * page's own headings; deeper ones go one level down each, to h6. */
export function MiniMarkdown({ text, headingLevel = 4 }: { text: string; headingLevel?: number }) {
  const lines = text.replace(/<!--[\s\S]*?-->/g, "").split("\n");
  const shallowest = Math.min(6, ...lines.map((l) => HEADING.exec(l)?.[1].length ?? 6));
  const out: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.trim() === "") {
      i++;
    } else if (FENCE.test(line)) {
      const code: string[] = [];
      i++;
      while (i < lines.length && !FENCE.test(lines[i])) code.push(lines[i++]);
      i++;
      out.push(
        <pre key={out.length} className="md-code">
          {code.join("\n")}
        </pre>,
      );
    } else if (HEADING.test(line)) {
      const [, hashes, words] = HEADING.exec(line)!;
      const Tag = `h${Math.min(6, headingLevel + hashes.length - shallowest)}` as "h4";
      out.push(<Tag key={out.length}>{inline(words)}</Tag>);
      i++;
    } else if (BULLET.test(line) || NUMBERED.test(line)) {
      const pattern = BULLET.test(line) ? BULLET : NUMBERED;
      const items: ReactNode[] = [];
      while (i < lines.length && pattern.test(lines[i])) {
        let words = pattern.exec(lines[i])![1];
        i++;
        // A line indented under an item goes on with it.
        while (i < lines.length && /^\s+\S/.test(lines[i]) && !BULLET.test(lines[i]) && !NUMBERED.test(lines[i])) words += ` ${lines[i++].trim()}`;
        items.push(<li key={items.length}>{inline(words)}</li>);
      }
      const List = pattern === BULLET ? "ul" : "ol";
      out.push(<List key={out.length}>{items}</List>);
    } else {
      const words: string[] = [];
      while (i < lines.length && lines[i].trim() !== "" && !HEADING.test(lines[i]) && !FENCE.test(lines[i]) && !BULLET.test(lines[i]) && !NUMBERED.test(lines[i])) {
        words.push(lines[i++].trim());
      }
      out.push(<p key={out.length}>{inline(words.join(" "))}</p>);
    }
  }
  return <>{out}</>;
}
