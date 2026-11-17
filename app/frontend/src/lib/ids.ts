// Ids for the lines the owner adds.

/** A new item's id: a slug of its name or words, unique among its siblings. */
export function newId(item: { name?: unknown; says?: unknown; kind?: unknown }, siblings: readonly { id?: unknown }[]): string {
  const source = String(item.name ?? item.says ?? item.kind ?? "item");
  const base = source.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "item";
  const taken = new Set(siblings.map((s) => String(s.id)));
  let id = base;
  for (let n = 2; taken.has(id); n++) id = `${base}-${n}`;
  return id;
}
