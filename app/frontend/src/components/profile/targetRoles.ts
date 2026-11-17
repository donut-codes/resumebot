// The target roles of What to look for, as plain functions: a blank role, a fresh id, and what a posting read for a
// role fills in. search/LookForPart.tsx holds the state; this holds the rules.

import type { ParsedRole, TargetRole } from "../../api/types/profile";
import { newId } from "../../lib/ids";
import { CRITERIA_HELP } from "./profileSpec";

/** The text parts of a role, in the order a posting reads, each with the header it has on the page and its one plain
 * line. Description is the core, with the example titles above it (the title check reads both); the rest are read only
 * with the posting's whole text, and sit below it under their own line. (The role's name and example titles come first,
 * above these.) */
export const ROLE_PARTS = [
  { key: "description", group: "core", title: "Description", help: CRITERIA_HELP.description },
  { key: "responsibilities", group: "more", title: "Responsibilities", help: CRITERIA_HELP.responsibilities },
  { key: "qualifications", group: "more", title: "Qualifications", help: CRITERIA_HELP.qualifications },
  { key: "preferred", group: "more", title: "Preferred", help: CRITERIA_HELP.preferred },
  { key: "compensation", group: "more", title: "Benefits / Compensation", help: CRITERIA_HELP.compensation },
] as const;

/** The counts the server holds (`MAX_TITLES` and `MAX_TARGET_ROLES` in profile/schema.py, docs/PRD.md 30.5): the page
 * says them and offers nothing past them, so a save is never refused for them. */
export const MAX_TITLES = 10;
export const MAX_TARGET_ROLES = 20;

/** The count of example titles in what the owner typed into the titles box (separated by semicolons). */
export function titlesTyped(text: string): string[] {
  return text
    .split(";")
    .map((t) => t.trim())
    .filter(Boolean);
}

/** The server's refusal of too many titles or roles, as one plain sentence; any other message comes back as it was. */
export function plainRefusal(error: string | null | undefined): string | null | undefined {
  if (!error) return error;
  const titles = /at most (\d+) example titles; it has (\d+)/.exec(error);
  if (titles) return `A role holds up to ${titles[1]} example titles, and this one has ${titles[2]}. Remove some, then save again.`;
  const roles = /at most (\d+) roles; it has (\d+)/.exec(error);
  if (roles) return `You can have up to ${roles[1]} target roles, and this makes ${roles[2]}. Delete one, then add again.`;
  return error;
}

/** A role with nothing written yet: a new install, or a role just added, starts empty. */
export function blankRole(id: string, name: string): TargetRole {
  return { id, name, titles: [], description: "", responsibilities: "", qualifications: "", preferred: "", compensation: "" };
}

/** A role as the page works with it: every part present, whatever the stored document left out. Anything else stored on
 * the role goes back as it came. */
export function normalizeRole(role: TargetRole): TargetRole {
  return {
    ...role,
    name: role.name ?? "",
    titles: role.titles ?? [],
    description: role.description ?? "",
    responsibilities: role.responsibilities ?? "",
    qualifications: role.qualifications ?? "",
    preferred: role.preferred ?? "",
    compensation: role.compensation ?? "",
  };
}

/** A new role's id: a slug of its name, made unique among the roles already there (-2, -3...). */
export function newRoleId(name: string, taken: { id: string }[]): string {
  return newId({ name }, taken);
}

/** Titles from both lists, the first list's first, each once (the same words in any case count as one), up to the
 * most a role holds. */
function mergeTitles(have: string[], found: string[]): string[] {
  const seen = new Set(have.map((t) => t.toLowerCase()));
  const merged = [...have];
  for (const raw of found) {
    if (merged.length >= MAX_TITLES) break;
    const title = raw.trim();
    if (title && !seen.has(title.toLowerCase())) {
      seen.add(title.toLowerCase());
      merged.push(title);
    }
  }
  return merged;
}

/** A role with what a posting was read for filled in: the name only when the role has none, the example titles added to
 * the ones it has, and each part replaced by what the posting said (a part it said nothing about stays as it was). */
export function mergeParsed(role: TargetRole, fields: Partial<ParsedRole>): TargetRole {
  const next: TargetRole = {
    ...role,
    name: role.name.trim() ? role.name : (fields.name ?? "").trim(),
    titles: mergeTitles(role.titles, fields.titles ?? []),
  };
  for (const { key } of ROLE_PARTS) {
    const said = (fields[key] ?? "").trim();
    if (said) next[key] = said;
  }
  return next;
}
