// A step's text as the page shows it: numbered lines as a numbered list, bullets as a list, `code` as code, *emphasis*
// as emphasis, everything else as a paragraph. Never HTML. Moved here from the host app.
import { Fragment, type ReactNode } from 'react'

/** Inline code (`like this`) as <code>, and *emphasis* as <em>; everything else as written. Never HTML. */
export function Inline({ text }: { text: string }) {
  const parts = text.split(/(`[^`\n]+`)/g)
  return (
    <>
      {parts.map((part, i) => {
        if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) {
          return <code key={i}>{part.slice(1, -1)}</code>
        }
        return <Fragment key={i}>{emphasis(part)}</Fragment>
      })}
    </>
  )
}

function emphasis(text: string): ReactNode[] {
  return text.split(/(\*[^\s*][^*\n]*?\*)/g).map((piece, i) =>
    piece.length > 2 && piece.startsWith('*') && piece.endsWith('*') ? <em key={i}>{piece.slice(1, -1)}</em> : piece,
  )
}

/** A label a paragraph opens with ("Principle." "Every ending.") reads as a label, not as the first words. */
const LEAD_LABEL = /^(Principle|Inputs and rules|Every ending|Processing rules)(?=[.,( ])/

type Item = { text: string; sub: string[] }
type Block =
  | { kind: 'p'; text: string }
  | { kind: 'ol'; start: number; items: Item[] }
  | { kind: 'ul'; items: Item[] }

/** A step's text as blocks: numbered lines are a numbered list (one rule per item, keeping the file's numbers),
 * bullet lines are a bulleted list, an indented bullet under a numbered line belongs to that line, and any other
 * line is a paragraph. */
export function blocksOf(text: string): Block[] {
  const blocks: Block[] = []
  for (const raw of text.split('\n')) {
    const line = raw.trimEnd()
    if (!line.trim()) continue
    const num = /^(\d+)\.\s+(.*)$/.exec(line)
    const bullet = /^(\s*)[•\-*]\s+(.*)$/.exec(line)
    const last = blocks[blocks.length - 1]
    if (num) {
      if (last?.kind === 'ol') last.items.push({ text: num[2], sub: [] })
      else blocks.push({ kind: 'ol', start: Number(num[1]), items: [{ text: num[2], sub: [] }] })
    } else if (bullet) {
      if (bullet[1].length >= 2 && last?.kind === 'ol') last.items[last.items.length - 1].sub.push(bullet[2])
      else if (last?.kind === 'ul') last.items.push({ text: bullet[2], sub: [] })
      else blocks.push({ kind: 'ul', items: [{ text: bullet[2], sub: [] }] })
    } else {
      blocks.push({ kind: 'p', text: line.trim() })
    }
  }
  return blocks
}

function Items({ items }: { items: Item[] }) {
  return (
    <>
      {items.map((item, i) => (
        <li key={i}>
          <Inline text={item.text} />
          {item.sub.length > 0 && (
            <ul>
              {item.sub.map((s, j) => (
                <li key={j}>
                  <Inline text={s} />
                </li>
              ))}
            </ul>
          )}
        </li>
      ))}
    </>
  )
}

/** One section's text: paragraphs, numbered lists and bullet lists, with inline code as code. */
export function RichText({ text }: { text: string }) {
  return (
    <>
      {blocksOf(text).map((b, i) => {
        if (b.kind === 'ol') {
          return (
            <ol key={i} start={b.start}>
              <Items items={b.items} />
            </ol>
          )
        }
        if (b.kind === 'ul') {
          return (
            <ul key={i}>
              <Items items={b.items} />
            </ul>
          )
        }
        const lead = LEAD_LABEL.exec(b.text)
        return (
          <p key={i}>
            {lead ? (
              <>
                <strong>{lead[1]}</strong>
                <Inline text={b.text.slice(lead[0].length)} />
              </>
            ) : (
              <Inline text={b.text} />
            )}
          </p>
        )
      })}
    </>
  )
}
