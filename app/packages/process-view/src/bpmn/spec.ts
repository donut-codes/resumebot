// A step's untagged `<documentation>` holds its specification under headings:
//
//   Actor: ...
//   Trigger: ...
//   Rules:
//   Preconditions. ...            <- labelled parts inside Rules
//   Inputs. ...
//   1. A numbered rule.
//   Exceptions. ...
//   Outputs: ...
//   What the household sees: ...
//   Source: ...
//
// `readSpec` reads those into fields; `applySpec` writes changed fields back into the original text, so a field
// that was not edited keeps its exact words and place. Same reading as the app's backend (`how_they_work.doc_sections`
// and `split_rules`); this is where it lives now.
import type { SpecFields } from './model'
import { emptySpec } from './model'

const HEADINGS = ['Actor', 'Trigger', 'Rules', 'Outputs', 'What the household sees', 'Source'] as const
type Heading = (typeof HEADINGS)[number]
const HEADING_FIELD: Record<Heading, keyof SpecFields> = {
  Actor: 'actor', Trigger: 'trigger', Rules: 'rules', Outputs: 'outputs', 'What the household sees': 'householdSees',
  Source: 'source',
}
const HEAD_SOURCE = '^(Actor|Trigger|Rules|Outputs|What the household sees|Source):[ \\t]*'

export interface SpecBlock {
  heading: string
  /** The heading as written, with the spaces after its colon: `Actor: `. */
  head: string
  /** Everything after it up to the next heading, as written. */
  body: string
}

export interface SpecDoc {
  /** Text before the first heading. */
  pre: string
  blocks: SpecBlock[]
}

/** Does this documentation carry the specification's headings (so it is a spec, not free-text instructions)? */
export function hasSpecHeadings(text: string): boolean {
  return new RegExp(HEAD_SOURCE, 'm').test(text)
}

export function splitSpecDoc(text: string): SpecDoc {
  const re = new RegExp(HEAD_SOURCE, 'gm')
  const found = [...text.matchAll(re)]
  if (found.length === 0) return { pre: text, blocks: [] }
  const blocks: SpecBlock[] = found.map((m, i) => {
    const end = i + 1 < found.length ? (found[i + 1].index ?? text.length) : text.length
    const start = (m.index ?? 0) + m[0].length
    return { heading: m[1], head: m[0], body: text.slice(start, end) }
  })
  return { pre: text.slice(0, found[0].index ?? 0), blocks }
}

function joinSpecDoc(doc: SpecDoc): string {
  return doc.pre + doc.blocks.map((b) => b.head + b.body).join('')
}

// --- the Rules block ----------------------------------------------------------------------------------------------

type RuleKey = 'preconditions' | 'inputs' | 'rules' | 'exceptions' | 'firedBy' | 'entersAt'
const RULE_KEYS: RuleKey[] = ['preconditions', 'inputs', 'exceptions', 'firedBy', 'entersAt', 'rules']
/** Where a part is put when it is new: the labelled parts before the rules, exceptions after. */
const RULE_ORDER: RuleKey[] = ['preconditions', 'inputs', 'firedBy', 'entersAt', 'rules', 'exceptions']
const RULE_PART = /^(?:(Preconditions|Inputs|Exceptions)\.|(Fired by|Enters at):)[ \t]*(.*)$/
const PLAIN_RULES = /^Processing rules(?:, in order| and effects)?\.[ \t]*(.*)$/
const PART_KEY: Record<string, RuleKey> = {
  Preconditions: 'preconditions', Inputs: 'inputs', Exceptions: 'exceptions', 'Fired by': 'firedBy',
  'Enters at': 'entersAt',
}
const DEFAULT_LABEL: Record<RuleKey, string> = {
  preconditions: 'Preconditions. ', inputs: 'Inputs. ', exceptions: 'Exceptions. ', firedBy: 'Fired by: ',
  entersAt: 'Enters at: ', rules: '',
}

interface RulePart {
  key: RuleKey
  /** The label as written, with its trailing spaces (`Preconditions. `), or '' for plain rule lines. */
  label: string
  lines: string[]
}

interface RulesBody {
  lead: string
  trail: string
  parts: RulePart[]
}

function parseRulesBody(body: string): RulesBody {
  const lead = /^\s*/.exec(body)?.[0] ?? ''
  const trail = body.trim() === '' ? '' : (/\s*$/.exec(body)?.[0] ?? '')
  const text = body.trim()
  const parts: RulePart[] = []
  if (text === '') return { lead, trail, parts }
  for (const line of text.split('\n')) {
    const m = RULE_PART.exec(line)
    if (m) {
      parts.push({ key: PART_KEY[m[1] ?? m[2]], label: line.slice(0, line.length - m[3].length), lines: [m[3]] })
    } else if (line.startsWith('Processing rules')) {
      const plain = PLAIN_RULES.exec(line)
      if (plain) parts.push({ key: 'rules', label: line.slice(0, line.length - plain[1].length), lines: [plain[1]] })
      else parts.push({ key: 'rules', label: '', lines: [line] })
    } else if (parts.length === 0) {
      parts.push({ key: 'rules', label: '', lines: [line] })
    } else {
      parts[parts.length - 1].lines.push(line)
    }
  }
  return { lead, trail, parts }
}

function partText(parts: RulePart[], key: RuleKey): string {
  return parts
    .filter((p) => p.key === key)
    .flatMap((p) => p.lines)
    .join('\n')
    .trim()
}

/** The marker line that takes the reader back to the rules after a labelled part. */
const MARKER = 'Processing rules.'

/**
 * A rules part that sits after another part needs a line of its own to say where it starts, or the reader takes its
 * numbered lines for more of the part before it. A part that already starts with such a line is left alone.
 */
function markRules(parts: RulePart[]): RulePart[] {
  return parts.map((p, i) => {
    if (i === 0 || p.key !== 'rules' || p.label !== '' || (p.lines[0] ?? '').startsWith('Processing rules')) return p
    return { ...p, label: MARKER, lines: ['', ...p.lines] }
  })
}

function setPart(parts: RulePart[], key: RuleKey, value: string): RulePart[] {
  const text = value.trim()
  const at = parts.findIndex((p) => p.key === key)
  if (text === '') {
    // A rules marker line ("Processing rules.") survives with nothing after it; other parts go.
    return key === 'rules'
      ? parts.filter((p) => p.key !== 'rules' || p.label !== '').map((p) => (p.key === 'rules' ? { ...p, lines: [''] } : p))
      : parts.filter((p) => p.key !== key)
  }
  const lines = text.split('\n')
  if (at >= 0) {
    return markRules(
      parts.flatMap((p, i) => {
        if (p.key !== key) return [p]
        if (i !== at) return []
        // A label on a line of its own ("Processing rules." and the lines under it) stays on a line of its own.
        const stacked = p.label !== '' && p.lines[0] === ''
        return [{ ...p, lines: stacked ? ['', ...lines] : lines }]
      }),
    )
  }
  const rank = RULE_ORDER.indexOf(key)
  const next = parts.findIndex((p) => RULE_ORDER.indexOf(p.key) > rank)
  const fresh: RulePart = { key, label: DEFAULT_LABEL[key], lines }
  return markRules(next < 0 ? [...parts, fresh] : [...parts.slice(0, next), fresh, ...parts.slice(next)])
}

function writeRulesBody(r: RulesBody): string {
  const out: string[] = []
  for (const p of r.parts) {
    const [first, ...rest] = p.lines.length ? p.lines : ['']
    out.push(p.label + first, ...rest)
  }
  return r.lead + out.join('\n') + r.trail
}

// --- reading and writing the fields -------------------------------------------------------------------------------

/** The fields of a documentation text. A heading or part that is not there is ''. */
export function readSpec(text: string): SpecFields {
  const out = emptySpec()
  const doc = splitSpecDoc(text)
  const seen = new Set<string>()
  for (const b of doc.blocks) {
    if (seen.has(b.heading)) continue
    seen.add(b.heading)
    out[HEADING_FIELD[b.heading as Heading]] = b.body.trim()
  }
  const parts = parseRulesBody(out.rules).parts
  for (const key of RULE_KEYS) out[key] = partText(parts, key)
  return out
}

const SIMPLE: (keyof SpecFields)[] = ['actor', 'trigger', 'outputs', 'householdSees', 'source']
const FIELD_HEADING = Object.fromEntries(HEADINGS.map((h) => [HEADING_FIELD[h], h])) as Record<string, Heading>

function setBlock(doc: SpecDoc, heading: Heading, value: string, multiline: boolean): void {
  const at = doc.blocks.findIndex((b) => b.heading === heading)
  const text = value.trim()
  if (at >= 0) {
    if (text === '') {
      doc.blocks.splice(at, 1)
      return
    }
    const old = doc.blocks[at]
    const lead = /^\s*/.exec(old.body)?.[0] ?? ''
    const trail = old.body.trim() === '' ? '\n' : (/\s*$/.exec(old.body)?.[0] ?? '')
    const wantsBreak = multiline || text.includes('\n')
    doc.blocks[at] = { ...old, body: (lead === '' && wantsBreak && heading === 'Rules' ? '\n' : lead) + text + trail }
    return
  }
  if (text === '') return
  const rank = HEADINGS.indexOf(heading)
  let next = doc.blocks.findIndex((b) => HEADINGS.indexOf(b.heading as Heading) > rank)
  if (next < 0) next = doc.blocks.length
  // The block before ends its last line, whatever it ended with.
  if (next > 0 && !/\n\s*$/.test(doc.blocks[next - 1].body)) doc.blocks[next - 1].body += '\n'
  const head = multiline ? `${heading}:` : `${heading}: `
  const body = (multiline ? '\n' : '') + text + (next < doc.blocks.length ? '\n' : '')
  doc.blocks.splice(next, 0, { heading, head, body })
}

/**
 * `original` with the fields that differ between `prev` and `next` written in; nothing else is touched.
 * Pass `original = ''` and `prev = emptySpec()` to build a documentation text from scratch.
 */
export function applySpec(original: string, prev: SpecFields, next: SpecFields): string {
  const doc = splitSpecDoc(original)
  for (const key of SIMPLE) {
    if (prev[key] !== next[key]) setBlock(doc, FIELD_HEADING[key], next[key], false)
  }
  if (RULE_KEYS.some((k) => prev[k] !== next[k])) {
    const at = doc.blocks.findIndex((b) => b.heading === 'Rules')
    const current = at >= 0 ? parseRulesBody(doc.blocks[at].body) : { lead: '\n', trail: '', parts: [] as RulePart[] }
    let parts = current.parts
    for (const key of RULE_KEYS) {
      if (prev[key] !== next[key]) parts = setPart(parts, key, next[key])
    }
    let written = writeRulesBody({ ...current, parts })
    if (parts.length === 0 || written.trim() === '') setBlock(doc, 'Rules', '', true)
    else if (at >= 0) {
      const old = doc.blocks[at]
      let head = old.head
      // Rules that began on the heading's own line, and now begin with a labelled part, begin on the next line.
      if (current.lead === '' && current.parts[0]?.label !== parts[0].label && parts[0].label !== '') {
        written = '\n' + written
        head = head.replace(/[ \t]+$/, '')
      }
      // The next heading starts a line.
      if (at < doc.blocks.length - 1 && !/\n\s*$/.test(written)) written += '\n'
      doc.blocks[at] = { ...old, head, body: written }
    } else setBlock(doc, 'Rules', written, true)
  }
  return joinSpecDoc(doc)
}

// --- rules as a list ----------------------------------------------------------------------------------------------

export interface RuleItem {
  /** The number it was written with; null for a line that isn't numbered. */
  n: number | null
  text: string
}

const NUMBERED = /^\s*(\d+)[.)]\s+(.*)$/

/** A rules text as items: each numbered line starts one, and the lines after it that are not numbered continue it. */
export function parseRuleItems(text: string): RuleItem[] {
  const items: RuleItem[] = []
  for (const line of text.split('\n')) {
    if (line.trim() === '' && items.length === 0) continue
    const m = NUMBERED.exec(line)
    if (m) items.push({ n: Number(m[1]), text: m[2] })
    else if (items.length === 0) items.push({ n: null, text: line })
    else items[items.length - 1].text += '\n' + line
  }
  return items
}

export function formatRuleItems(items: RuleItem[]): string {
  return items.map((i) => (i.n === null ? i.text : `${i.n}. ${i.text}`)).join('\n')
}

/** Numbers the numbered items 1, 2, 3 ... in order (after one is added or removed). */
export function renumber(items: RuleItem[]): RuleItem[] {
  let n = 0
  return items.map((i) => (i.n === null ? i : { ...i, n: ++n }))
}
