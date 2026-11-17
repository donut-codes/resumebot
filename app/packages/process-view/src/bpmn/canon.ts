// Comparing values by what they hold, not by how they were built: two objects with the same keys and values are the
// same value whatever order the keys were added in. (`JSON.stringify` writes keys in the order they were added.)

/** JSON text of a value with every object's keys in sorted order. */
export function canonical(value: unknown): string {
  return JSON.stringify(value, (_key, v: unknown) => {
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      const sorted: Record<string, unknown> = {}
      for (const k of Object.keys(v).sort()) sorted[k] = (v as Record<string, unknown>)[k]
      return sorted
    }
    return v
  })
}

export function sameValue(a: unknown, b: unknown): boolean {
  return canonical(a) === canonical(b)
}
