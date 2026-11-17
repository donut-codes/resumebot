// Small helpers for finding things inside one view's own markup. The package never looks anywhere else on the page:
// two views side by side must not act on each other, and a step id with a quote in it must not break a lookup.

/** `CSS.escape` where the browser has it; the same escaping by hand where it does not (jsdom). */
export function cssEscape(value: string): string {
  if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') return CSS.escape(value)
  let out = ''
  for (let i = 0; i < value.length; i++) {
    const ch = value.charAt(i)
    const code = value.charCodeAt(i)
    const digit = ch >= '0' && ch <= '9'
    if (code === 0) out += '�'
    else if ((code >= 1 && code <= 0x1f) || code === 0x7f || (i === 0 && digit) || (i === 1 && digit && value.charAt(0) === '-')) {
      out += `\\${code.toString(16)} `
    } else if (i === 0 && ch === '-' && value.length === 1) out += `\\${ch}`
    else if (code >= 0x80 || ch === '-' || ch === '_' || digit || (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z')) out += ch
    else out += `\\${ch}`
  }
  return out
}

/** The element with this id inside `root` (not anywhere else on the page). */
export function byIdIn<T extends Element = HTMLElement>(root: ParentNode | null | undefined, id: string): T | null {
  return root ? root.querySelector<T>(`#${cssEscape(id)}`) : null
}

/** The element matching `selector` that carries `attr="value"`, inside `root`, with the value escaped. */
export function byAttrIn<T extends Element = HTMLElement>(
  root: ParentNode | null | undefined, selector: string, attr: string, value: string,
): T | null {
  return root ? root.querySelector<T>(`${selector}[${attr}="${cssEscape(value)}"]`) : null
}
