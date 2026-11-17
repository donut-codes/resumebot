import type { HarnessProcess } from "../../api/types/agents";

/** The tabs open, top-level first: the process the path names (else the first), then under each the
 * sub-process the path names next. A name the files don't have ends the chain there. */
export function chainFor(all: HarnessProcess[], path: string[]): HarnessProcess[] {
  const byId = new Map(all.map((p) => [p.id, p]));
  const tops = all.filter((p) => p.top_level);
  // A process named first that is not top-level (a mission's playbook can be a sub-process) still opens alone.
  const named = tops.find((p) => p.slug === path[0]) ?? (path[0] ? all.find((p) => p.slug === path[0]) : undefined);
  const first = named ?? tops[0];
  if (!first) return [];
  const chain = [first];
  for (const slug of named ? path.slice(1) : []) {
    const next = chain[chain.length - 1].children.map((id) => byId.get(id)).find((p) => p?.slug === slug);
    if (!next || chain.includes(next)) break;
    chain.push(next);
  }
  return chain;
}
