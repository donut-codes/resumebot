// Hard pass by tab, as plain functions. The document's top level is the All tab (its switches, work location, lowest pay
// and gate lines); `by_role` holds what each role's own tab adds: the same three things, and its own gate lines (those
// carry the role in `roles`). The page writes a role's entry only when it says something, and compares what is saved
// with what is typed through the same `tidyGates`, so a role that was only looked at is never a change. Also here: the
// gates as the page starts from them, the gate lines of a tab, and the pay method's numbers edited with the pay gate.

import type { CompMethod, GateSet, GateWay, Profile, RoleGates, SectionId } from "../../api/types/profile";
import { canon } from "./EditParts";
import { gateSection, inPageOrder, placeGates } from "./profileSpec";
import { ALL_TAB, tabOf } from "./scope";

/** What a role's tab says when nothing is set on it (the server's own defaults): no way of working ticked, no pay amount,
 * every part on. */
function defaultRoleGates(): RoleGates {
  const way = (): GateWay => ({ on: false, locations: [] });
  return {
    sections_off: [],
    location: { remote: way(), hybrid: way(), in_person: way() },
    compensation: { amount: null, against: "maximum", wide_band_ratio: 1.5 },
  };
}

/** A role's tab as the page works with it: every field present, whatever the stored entry left out (a role with no entry
 * is the default). Anything else stored on the entry goes back as it came. */
function roleGatesOf(gates: GateSet, id: string): RoleGates {
  const own: Partial<RoleGates> = gates.by_role?.[id] ?? {};
  const base = defaultRoleGates();
  return {
    ...own,
    sections_off: inPageOrder(own.sections_off ?? []),
    location: {
      remote: { ...base.location.remote, ...own.location?.remote },
      hybrid: { ...base.location.hybrid, ...own.location?.hybrid },
      in_person: { ...base.location.in_person, ...own.location?.in_person },
    },
    compensation: { ...base.compensation, ...own.compensation },
  };
}

/** What a tab holds besides its gate lines: All's is the document's own top level, a role's its entry in `by_role`. */
export function scopeView(gates: GateSet, scope: string): RoleGates {
  if (scope !== ALL_TAB) return roleGatesOf(gates, scope);
  return { sections_off: gates.sections_off, location: gates.location, compensation: gates.compensation };
}

/** The document with some of a tab's own fields changed. */
export function withScope(gates: GateSet, scope: string, patch: Partial<RoleGates>): GateSet {
  if (scope === ALL_TAB) return { ...gates, ...patch };
  return { ...gates, by_role: { ...gates.by_role, [scope]: { ...roleGatesOf(gates, scope), ...patch } } };
}

/** Only what the page needs saved: sentences with no words are dropped, the switches are in the page's order, and a role's
 * entry is there only when it differs from the default. An entry for a role the page does not have (it was deleted) goes
 * back exactly as it came, so nothing a role left behind is lost. The rest goes back as it was. */
export function tidyGates(gates: GateSet, roleIds: ReadonlySet<string>): GateSet {
  const blank = canon(defaultRoleGates());
  const by_role: Record<string, RoleGates> = {};
  for (const [id, entry] of Object.entries(gates.by_role ?? {})) {
    if (!roleIds.has(id)) {
      by_role[id] = entry;
      continue;
    }
    const own = roleGatesOf(gates, id);
    if (canon(own) !== blank) by_role[id] = own;
  }
  return {
    ...gates,
    sections_off: inPageOrder(gates.sections_off ?? []),
    other: gates.other.filter((x) => x.kind !== "rule" || x.text.trim() !== ""),
    by_role,
  };
}

/** The pay method's numbers live in Settings; they are edited here, with the pay gate (docs/PRD.md 26.6, 30.4). */
const COMP_KEYS: (keyof CompMethod)[] = [
  "comp_basis", "comp_assumptions", "comp_tiers", "comp_transferable_weight", "comp_stance_clamp",
  "comp_stance_min_siblings", "comp_likely_low", "comp_likely_high", "comp_round_to", "comp_range_width",
];

export function compOf(settings: CompMethod): CompMethod {
  return Object.fromEntries(COMP_KEYS.map((k) => [k, settings[k]])) as unknown as CompMethod;
}

/** The profile's gates as the page works with them: every gate in a part of a posting (one saved without a part is put in
 * its kind's home), nothing guessed from its words, and a `by_role` there whether or not the server sent one. */
export const gatesOf = (profile: Profile): GateSet =>
  placeGates({
    ...structuredClone(profile.gates),
    sections_off: inPageOrder(profile.gates.sections_off ?? []),
    by_role: structuredClone(profile.gates.by_role ?? {}),
  });

/** The gate lines of a tab (and of one part of it, when `section` is given). A gate on no tab is in none. */
export const linesIn = (gates: GateSet, scope: string, roleIds: ReadonlySet<string>, section?: SectionId) =>
  gates.other.filter((g) => tabOf(g, roleIds) === scope && (section === undefined || gateSection(g) === section));
