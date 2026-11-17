// A small XML reader and writer that keeps everything: element and attribute order, every comment and run of
// whitespace, and the exact spelling of text and attribute values until something edits them. That is what lets a
// BPMN file that was only read be written back byte for byte, and an edited one differ only where it was edited.
//
// Not a general XML library: no DTDs (a file that carries one is refused, so nothing can expand), no encodings other
// than what JavaScript strings hold.
//
// What it reads is what an XML parser (the engine's) reads: line ends are normalised to "\n" in text and in
// attribute values, and a tab or line end typed into an attribute value reads as a space. What it writes is valid
// XML 1.0: characters the standard forbids are left out, and a newline, tab or CR in an attribute is written as a
// character reference so it survives being read again. A file that uses "\r\n" is written with "\r\n" wherever a
// line is new; the lines nobody edited keep their own spelling.

export class XmlError extends Error {}

export interface XAttr {
  name: string
  value: string
  /** The value as it was written (entities and all); dropped when the value is edited. */
  raw?: string
  quote: '"' | "'"
  /** The whitespace written before the attribute (a space, or a line break and indent). */
  sep: string
}

export interface XText {
  kind: 'text'
  /** The text as an XML parser reads it: entities decoded, line ends "\n". */
  text: string
  /** The text as written; dropped when the text is edited. */
  raw?: string
  /**
   * For edited text: the raw spelling of each line (split on "\n") whose meaning did not change, or null where the
   * line is new. Written text is these lines, or the encoded line, joined with the file's line end.
   */
  lines?: (string | null)[]
}

/** A comment, a processing instruction or a CDATA section, kept as written. */
export interface XRaw {
  kind: 'raw'
  raw: string
  /** 'cdata' counts as text for reading; the others are not content. */
  type: 'comment' | 'pi' | 'cdata'
}

export interface XEl {
  kind: 'el'
  /** Qualified name as written: `bpmn:task`. */
  name: string
  prefix: string
  local: string
  /** The namespace URI the prefix (or the default namespace) resolved to at parse time; '' when none. */
  ns: string
  attrs: XAttr[]
  children: XNode[]
  /** Written as `<a/>` (true) or `<a></a>`; changes to true only for an element that never had children. */
  selfClose: boolean
  /** Whitespace written between the last attribute and the closing `>` or `/>`, kept for a faithful write. */
  tail: string
}

export type XNode = XEl | XText | XRaw

export interface XDoc {
  nodes: XNode[]
  /** The line end the file uses: "\r\n" when most of its line ends are, else "\n". Where a new line is written. */
  eol?: '\n' | '\r\n'
}

/** How deep elements may nest. A BPMN file nests a dozen levels; this is for text made to run a reader out of stack. */
export const MAX_DEPTH = 256

const ENTITIES: Record<string, string> = { lt: '<', gt: '>', amp: '&', quot: '"', apos: "'" }

export function decode(raw: string): string {
  if (!raw.includes('&')) return raw
  return raw.replace(/&(#x[0-9a-fA-F]+|#[0-9]+|[a-zA-Z]+);/g, (whole, body: string) => {
    if (body[0] === '#') {
      const code = body[1] === 'x' ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10)
      return Number.isFinite(code) && code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : whole
    }
    return ENTITIES[body] ?? whole
  })
}

/** Text as an XML parser hands it over: "\r\n" and a lone "\r" are "\n". */
export function normalizeNewlines(raw: string): string {
  return raw.includes('\r') ? raw.replace(/\r\n?/g, '\n') : raw
}

/** An attribute's raw value as an XML parser reads it: line ends normalised, then each tab and line end a space. */
function decodeAttr(raw: string): string {
  const flat = raw.includes('\r') || raw.includes('\n') || raw.includes('\t') ? normalizeNewlines(raw).replace(/[\n\t]/g, ' ') : raw
  return decode(flat)
}

const FORBIDDEN = /[\u0000-\u0008\u000B\u000C\u000E-\u001F￾￿]/g
const SURROGATE = /[\uD800-\uDFFF]/

/** Whether `text` holds a character XML 1.0 forbids (a control character, or half of a surrogate pair). */
export function hasForbidden(text: string): boolean {
  return stripForbidden(text) !== text
}

/** `text` without the characters XML 1.0 forbids: control characters other than tab, line feed and carriage return. */
export function stripForbidden(text: string): string {
  let out = text.replace(FORBIDDEN, '')
  if (SURROGATE.test(out)) {
    let kept = ''
    for (let i = 0; i < out.length; i++) {
      const c = out.charCodeAt(i)
      if (c >= 0xd800 && c <= 0xdbff) {
        const d = out.charCodeAt(i + 1)
        if (d >= 0xdc00 && d <= 0xdfff) {
          kept += out[i] + out[i + 1]
          i++
        }
      } else if (c < 0xdc00 || c > 0xdfff) {
        kept += out[i]
      }
    }
    out = kept
  }
  return out
}

export function encodeText(text: string): string {
  return stripForbidden(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\r/g, '&#13;')
}

export function encodeAttr(value: string, quote: '"' | "'" = '"'): string {
  const escaped = stripForbidden(value).replace(/&/g, '&amp;').replace(/</g, '&lt;')
  // A parser turns a tab or a line end typed into an attribute into a space, so these are written as references.
  const refs = escaped.replace(/\n/g, '&#10;').replace(/\r/g, '&#13;').replace(/\t/g, '&#9;')
  return quote === '"' ? refs.replace(/"/g, '&quot;') : refs.replace(/'/g, '&apos;')
}

const NAME_START = /[A-Za-z_:À-￿]/
const NAME_CHAR = /[A-Za-z0-9_:.\-·À-￿]/

interface Scope {
  [prefix: string]: string
}

export function parseXml(source: string): XDoc {
  let i = 0
  const n = source.length
  const fail = (msg: string): never => {
    const line = source.slice(0, i).split('\n').length
    throw new XmlError(`${msg} (line ${line})`)
  }

  function readName(): string {
    const start = i
    if (i >= n || !NAME_START.test(source[i])) fail('Expected a name')
    i++
    while (i < n && NAME_CHAR.test(source[i])) i++
    return source.slice(start, i)
  }

  function skipSpace(): string {
    const start = i
    while (i < n && /\s/.test(source[i])) i++
    return source.slice(start, i)
  }

  function readNodes(scope: Scope, parent: XEl | null, depth: number): XNode[] {
    const out: XNode[] = []
    while (i < n) {
      if (source[i] !== '<') {
        const start = i
        while (i < n && source[i] !== '<') i++
        const raw = source.slice(start, i)
        out.push({ kind: 'text', text: decode(normalizeNewlines(raw)), raw })
        continue
      }
      if (source.startsWith('<!--', i)) {
        const end = source.indexOf('-->', i + 4)
        if (end < 0) fail('Unclosed comment')
        out.push({ kind: 'raw', type: 'comment', raw: source.slice(i, end + 3) })
        i = end + 3
      } else if (source.startsWith('<![CDATA[', i)) {
        const end = source.indexOf(']]>', i + 9)
        if (end < 0) fail('Unclosed CDATA section')
        out.push({ kind: 'raw', type: 'cdata', raw: source.slice(i, end + 3) })
        i = end + 3
      } else if (source.startsWith('<?', i)) {
        const end = source.indexOf('?>', i + 2)
        if (end < 0) fail('Unclosed processing instruction')
        out.push({ kind: 'raw', type: 'pi', raw: source.slice(i, end + 2) })
        i = end + 2
      } else if (source.startsWith('<!', i)) {
        fail('A DOCTYPE or entity declaration is not allowed')
      } else if (source.startsWith('</', i)) {
        return out
      } else {
        out.push(readElement(scope, depth))
      }
    }
    if (parent) fail(`Unclosed <${parent.name}>`)
    return out
  }

  function readElement(outer: Scope, depth: number): XEl {
    if (depth > MAX_DEPTH) fail(`Elements are nested more than ${MAX_DEPTH} deep`)
    i++ // <
    const name = readName()
    const attrs: XAttr[] = []
    let tail = ''
    let selfClose = false
    for (;;) {
      const space = skipSpace()
      if (source.startsWith('/>', i)) {
        tail = space
        selfClose = true
        i += 2
        break
      }
      if (source[i] === '>') {
        tail = space
        i++
        break
      }
      if (i >= n) fail(`Unclosed <${name}>`)
      if (!space && attrs.length > 0) fail('Expected a space between attributes')
      const attrName = readName()
      skipSpace()
      if (source[i] !== '=') fail(`Attribute ${attrName} has no value`)
      i++
      skipSpace()
      const quote = source[i]
      if (quote !== '"' && quote !== "'") fail(`Attribute ${attrName} is not quoted`)
      const end = source.indexOf(quote, i + 1)
      if (end < 0) fail(`Unclosed attribute ${attrName}`)
      const raw = source.slice(i + 1, end)
      if (raw.includes('<')) fail(`Attribute ${attrName} holds a "<"`)
      attrs.push({ name: attrName, value: decodeAttr(raw), raw, quote: quote as '"' | "'", sep: space })
      i = end + 1
      if (attrs.slice(0, -1).some((a) => a.name === attrName)) fail(`Attribute ${attrName} is written twice`)
    }
    const scope: Scope = { ...outer }
    for (const a of attrs) {
      if (a.name === 'xmlns') scope[''] = a.value
      else if (a.name.startsWith('xmlns:')) scope[a.name.slice(6)] = a.value
    }
    const colon = name.indexOf(':')
    const prefix = colon < 0 ? '' : name.slice(0, colon)
    const local = colon < 0 ? name : name.slice(colon + 1)
    const el: XEl = { kind: 'el', name, prefix, local, ns: scope[prefix] ?? '', attrs, children: [], selfClose, tail }
    if (!selfClose) {
      el.children = readNodes(scope, el, depth + 1)
      if (!source.startsWith('</', i)) fail(`Unclosed <${name}>`)
      i += 2
      const closing = readName()
      if (closing !== name) fail(`<${name}> is closed by </${closing}>`)
      skipSpace()
      if (source[i] !== '>') fail(`Unclosed </${name}>`)
      i++
    }
    return el
  }

  const nodes = readNodes({ xml: 'http://www.w3.org/XML/1998/namespace' }, null, 1)
  if (i < n) fail('Unexpected closing tag')
  const roots = nodes.filter((x) => x.kind === 'el')
  if (roots.length !== 1) throw new XmlError(`Expected one root element, found ${roots.length}`)
  return { nodes, eol: eolOf(source) }
}

/** The line end a text mostly uses. */
function eolOf(source: string): '\n' | '\r\n' {
  if (!source.includes('\r')) return '\n'
  const crlf = source.split('\r\n').length - 1
  const lf = source.split('\n').length - 1 - crlf
  return crlf > lf ? '\r\n' : '\n'
}

function writeAttr(a: XAttr): string {
  const value = a.raw !== undefined ? a.raw : encodeAttr(a.value, a.quote)
  return `${a.name}=${a.quote}${value}${a.quote}`
}

/** Whitespace made in code uses "\n"; it is written with the file's line end. */
function lineEnds(text: string, eol: string): string {
  return eol === '\n' ? text : text.replace(/\r?\n/g, eol)
}

function textRaw(node: XText, eol: string): string {
  if (node.raw !== undefined) return node.raw
  const lines = node.text.split('\n')
  return lines.map((l, k) => node.lines?.[k] ?? encodeText(l)).join(eol)
}

function writeNode(node: XNode, out: string[], eol: string): void {
  if (node.kind === 'text') {
    out.push(textRaw(node, eol))
  } else if (node.kind === 'raw') {
    out.push(node.raw)
  } else {
    out.push(`<${node.name}`)
    for (const a of node.attrs) {
      out.push(lineEnds(a.sep, eol))
      out.push(writeAttr(a))
    }
    if (node.selfClose && node.children.length === 0) {
      out.push(`${lineEnds(node.tail, eol)}/>`)
    } else {
      out.push(`${lineEnds(node.tail, eol)}>`)
      for (const c of node.children) writeNode(c, out, eol)
      out.push(`</${node.name}>`)
    }
  }
}

export function writeXml(doc: XDoc): string {
  const out: string[] = []
  const eol = doc.eol ?? '\n'
  for (const node of doc.nodes) writeNode(node, out, eol)
  return out.join('')
}

// --- helpers on the tree ----------------------------------------------------------------------------------------

export function attr(el: XEl, name: string): string | undefined {
  return el.attrs.find((a) => a.name === name)?.value
}

export function setAttr(el: XEl, name: string, value: string | null, before?: string[]): void {
  const found = el.attrs.find((a) => a.name === name)
  if (value === null) {
    if (found) el.attrs.splice(el.attrs.indexOf(found), 1)
    return
  }
  if (found) {
    if (found.value !== value) {
      found.value = value
      delete found.raw
    }
    return
  }
  const fresh: XAttr = { name, value, quote: '"', sep: ' ' }
  // `before` names attributes the new one should precede (e.g. `porter:` attributes after plain ones are kept apart).
  const at = before ? el.attrs.findIndex((a) => before.includes(a.name)) : -1
  if (at >= 0) el.attrs.splice(at, 0, fresh)
  else el.attrs.push(fresh)
}

export function elements(el: XEl, local?: string, ns?: string): XEl[] {
  return el.children.filter(
    (c): c is XEl => c.kind === 'el' && (local === undefined || c.local === local) && (ns === undefined || c.ns === ns),
  )
}

/** The text of an element: its text and CDATA children, concatenated, with "\n" line ends. */
export function textOf(el: XEl): string {
  let out = ''
  for (const c of el.children) {
    if (c.kind === 'text') out += c.text
    else if (c.kind === 'raw' && c.type === 'cdata') out += normalizeNewlines(c.raw.slice(9, -3))
  }
  return out
}

/** Which new line gets which of the old lines' spelling: the lines both texts share, in order (a longest common run). */
function sharedLines(old: string[], next: string[]): Map<number, number> {
  const map = new Map<number, number>()
  let head = 0
  while (head < old.length && head < next.length && old[head] === next[head]) {
    map.set(head, head)
    head++
  }
  let tail = 0
  while (tail < old.length - head && tail < next.length - head && old[old.length - 1 - tail] === next[next.length - 1 - tail]) {
    map.set(next.length - 1 - tail, old.length - 1 - tail)
    tail++
  }
  const a = old.slice(head, old.length - tail)
  const b = next.slice(head, next.length - tail)
  if (a.length === 0 || b.length === 0 || a.length * b.length > 250_000) return map
  const t: number[][] = Array.from({ length: a.length + 1 }, () => new Array<number>(b.length + 1).fill(0))
  for (let x = a.length - 1; x >= 0; x--) {
    for (let y = b.length - 1; y >= 0; y--) t[x][y] = a[x] === b[y] ? t[x + 1][y + 1] + 1 : Math.max(t[x + 1][y], t[x][y + 1])
  }
  let x = 0
  let y = 0
  while (x < a.length && y < b.length) {
    if (a[x] === b[y]) {
      map.set(head + y, head + x)
      x++
      y++
    } else if (t[x + 1][y] >= t[x][y + 1]) x++
    else y++
  }
  return map
}

/** The spelling of each line of an element's text, when the element holds one plain text node that can be split so. */
function rawLinesOf(el: XEl): string[] | null {
  if (el.children.length !== 1) return null
  const only = el.children[0]
  if (only.kind !== 'text' || only.raw === undefined) return null
  const lines = only.raw.split(/\r\n|\r|\n/)
  return lines.length === only.text.split('\n').length ? lines : null
}

/**
 * Replaces an element's content with `text`. Nothing is written when the text is the same; and the lines of the old
 * text that are still there keep their spelling (`&gt;`, `&quot;`, `&#8212;`), so an edit changes only its own lines.
 */
export function setText(el: XEl, text: string): void {
  const old = textOf(el)
  if (old === text) return
  if (text === '') {
    el.children = []
    el.selfClose = true
    return
  }
  const rawLines = rawLinesOf(el)
  const node: XText = { kind: 'text', text }
  if (rawLines) {
    const same = sharedLines(old.split('\n'), text.split('\n'))
    // A kept line is left as it was written, unless it holds what XML forbids: the edit repairs that.
    node.lines = text.split('\n').map((_, k) => {
      const at = same.get(k)
      return at !== undefined && !hasForbidden(rawLines[at]) ? rawLines[at] : null
    })
  }
  el.children = [node]
  el.selfClose = false
}

/** The whitespace run (`\n    `) that precedes a child of `el`, to indent a new sibling the same way. */
export function indentOf(el: XEl): string {
  for (const c of el.children) {
    if (c.kind === 'text' && /^\s*\n[ \t]*$/.test(c.raw ?? c.text)) return (c.raw ?? c.text).replace(/^\s*(\n[ \t]*)$/, '$1')
  }
  return '\n  '
}

/** Insert `child` into `parent.children` at index `at`, with the parent's indentation before it. */
export function insertChild(parent: XEl, at: number, child: XEl, indent?: string): void {
  const pad = indent ?? indentOf(parent)
  // If the parent is empty of children entirely, the closing tag needs the parent's own indent too.
  if (parent.children.length === 0 || parent.children.every((c) => c.kind === 'text' && c.text.trim() === '')) {
    const closing = pad.length >= 3 ? pad.slice(0, pad.length - 2) : '\n'
    parent.children = [
      { kind: 'text', text: pad },
      child,
      { kind: 'text', text: closing },
    ]
    parent.selfClose = false
    return
  }
  // At the end means before the whitespace that closes the parent, not after it.
  let pos = at
  const last = parent.children[parent.children.length - 1]
  if (pos >= parent.children.length && last && last.kind === 'text' && last.text.trim() === '' && last.text.includes('\n')) {
    pos = parent.children.length - 1
  }
  parent.children.splice(pos, 0, { kind: 'text', text: pad }, child)
}

/** Insert `child` on its own line just before `before`, which is a child of `parent`. */
export function insertBefore(parent: XEl, before: XNode, child: XEl, indent?: string): void {
  const at = parent.children.indexOf(before)
  if (at < 0) return insertChild(parent, parent.children.length, child, indent)
  const lead = parent.children[at - 1]
  // The whitespace in front of `before` stays in front of it; the new line goes ahead of that.
  const pos = lead && lead.kind === 'text' && lead.text.trim() === '' && lead.text.includes('\n') ? at - 1 : at
  insertChild(parent, pos, child, indent)
}

/** Remove a child and the whitespace line that led up to it. */
export function removeChild(parent: XEl, child: XNode): void {
  const at = parent.children.indexOf(child)
  if (at < 0) return
  const before = parent.children[at - 1]
  if (before && before.kind === 'text' && /^\s*$/.test(before.text) && before.text.includes('\n')) {
    parent.children.splice(at - 1, 2)
  } else {
    parent.children.splice(at, 1)
  }
}

export function cloneNode<T extends XNode>(node: T): T {
  return structuredClone(node)
}

export function cloneDoc(doc: XDoc): XDoc {
  return structuredClone(doc)
}

// --- names -----------------------------------------------------------------------------------------------------

/** Whether a code point may start an XML name (XML 1.0, fifth edition), the colon left out. */
function isNameStart(cp: number): boolean {
  return (
    (cp >= 0x41 && cp <= 0x5a) || cp === 0x5f || (cp >= 0x61 && cp <= 0x7a) || (cp >= 0xc0 && cp <= 0xd6) ||
    (cp >= 0xd8 && cp <= 0xf6) || (cp >= 0xf8 && cp <= 0x2ff) || (cp >= 0x370 && cp <= 0x37d) ||
    (cp >= 0x37f && cp <= 0x1fff) || (cp >= 0x200c && cp <= 0x200d) || (cp >= 0x2070 && cp <= 0x218f) ||
    (cp >= 0x2c00 && cp <= 0x2fef) || (cp >= 0x3001 && cp <= 0xd7ff) || (cp >= 0xf900 && cp <= 0xfdcf) ||
    (cp >= 0xfdf0 && cp <= 0xfffd) || (cp >= 0x10000 && cp <= 0xeffff)
  )
}

/** The words of `text` (letters and digits of any script) joined by "_"; '' when there are none. */
export function slugWords(text: string): string {
  let out = ''
  let gap = false
  for (const ch of text.trim()) {
    const cp = ch.codePointAt(0)!
    // What may be in a name: a letter or digit the XML standard allows in one.
    const word = /[\p{L}\p{N}]/u.test(ch) && (isNameStart(cp) || (cp >= 0x30 && cp <= 0x39))
    if (word) {
      if (gap && out) out += '_'
      gap = false
      out += ch
    } else {
      gap = true
    }
  }
  return out
}

/** An id made from words that is a valid XML name on its own: one that would start with a digit gets `prefix` first. */
export function nameFrom(text: string, prefix: string): string {
  const words = slugWords(text)
  if (!words) return prefix
  return isNameStart(words.codePointAt(0)!) ? words : `${prefix}_${words}`
}
