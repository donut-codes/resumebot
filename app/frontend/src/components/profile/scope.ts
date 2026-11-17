// The tabs of Hard pass and Prioritization, as plain functions. Each page has a first tab, All, and then the target roles'
// tabs, the same ones What to look for has (docs/PRD.md 31.5). A gate or an ask is for every role (All) or for one role's
// tab; what is on a role's tab adds to what is on All, never replaces it.

/** The id of the first tab, All. A role's id is a lowercase slug (`^[a-z0-9][a-z0-9-]*$`), so this cannot be one. */
export const ALL_TAB = "__all__";

/** The target roles (their ids) a gate or an ask names; none: every role. */
const rolesOf = (item: { roles?: string[] }): string[] => item.roles ?? [];

/** The tab a gate or an ask is on: All when it names no role, a role's own tab when it names one role the page has, and
 * none (null) otherwise: a role that is gone, or more than one (the server splits those, so it never sends them). An item
 * on no tab is shown nowhere and goes back as it came. */
export function tabOf(item: { roles?: string[] }, roleIds: ReadonlySet<string>): string | null {
  const roles = rolesOf(item);
  if (roles.length === 0) return ALL_TAB;
  return roles.length === 1 && roleIds.has(roles[0]) ? roles[0] : null;
}

/** What a new gate or ask on a tab names: every role on All, that role on its own tab. */
export const rolesFor = (scope: string): string[] => (scope === ALL_TAB ? [] : [scope]);

/** What a role's tab says: its name, or "Unnamed role" while it has none (the same as on What to look for). */
export const roleLabel = (name: string | undefined): string => (name ?? "").trim() || "Unnamed role";
