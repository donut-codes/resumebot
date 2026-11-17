// The facts an employer has and the owner may set, and the plain functions that turn them into a form and a change. The
// panel (EmployerPanel.tsx) holds the state; this holds the rules.
import type { Employer } from "../../api/types/employers";
import { formatCount } from "../../lib/format";

export const WORK_STYLE_WORDS: Record<Employer["work_style"], string> = {
  remote: "Remote", hybrid: "Hybrid", onsite: "On site", mixed: "Mixed", unknown: "Not known",
};
export const OWNERSHIP_WORDS: Record<Employer["ownership"], string> = {
  public: "Public", private: "Private", pe: "PE-owned", vc: "VC-funded", nonprofit: "Nonprofit", government: "Government",
  unknown: "Not known",
};

export const yesNo = (v: boolean | null) => (v === true ? "Yes" : v === false ? "No" : "–");

export type Field = { key: keyof Employer; label: string; kind: "text" | "number" | "yesno" | "list" | "style" | "ownership" | "long" };

/** The facts shown and edited, in order. (A hard pass on the employer is not one of them: it is set by the switch on the
 * record, the one place.) */
export const FIELDS: Field[] = [
  { key: "name", label: "Name", kind: "text" },
  { key: "aliases", label: "Also called", kind: "list" },
  { key: "website", label: "Website", kind: "text" },
  { key: "careers_url", label: "Careers page", kind: "text" },
  { key: "headquarters", label: "Headquarters", kind: "text" },
  { key: "offices", label: "Offices", kind: "list" },
  { key: "local", label: "Has local office", kind: "yesno" },
  { key: "work_style", label: "Work style", kind: "style" },
  { key: "employees", label: "People", kind: "number" },
  { key: "founded", label: "Founded", kind: "number" },
  { key: "industry", label: "Industry", kind: "text" },
  { key: "sectors", label: "Sectors", kind: "list" },
  { key: "ownership", label: "Ownership", kind: "ownership" },
  { key: "ai_native", label: "AI-native", kind: "yesno" },
  { key: "group", label: "Group", kind: "text" },
  { key: "notes", label: "Notes", kind: "long" },
];

export function shownValue(e: Employer, f: Field): string {
  const v = e[f.key];
  if (f.kind === "list") return (v as string[]).join("; ") || "–";
  if (f.kind === "yesno") return yesNo(v as boolean | null);
  if (f.kind === "style") return WORK_STYLE_WORDS[v as Employer["work_style"]];
  if (f.kind === "ownership") return OWNERSHIP_WORDS[v as Employer["ownership"]];
  if (f.kind === "number" && typeof v === "number") return f.key === "founded" ? String(v) : formatCount(v);
  return v == null || v === "" ? "–" : String(v);
}

/** The facts as the form holds them: every one as text. */
export function draftOf(e: Employer): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of FIELDS) {
    const v = e[f.key];
    out[f.key] = f.kind === "list" ? (v as string[]).join("; ") : f.kind === "yesno" ? (v === true ? "yes" : v === false ? "no" : "") : v == null ? "" : String(v);
  }
  return out;
}

/** Only what the form changed, as the server takes it. */
export function changesOf(e: Employer, draft: Record<string, string>): Partial<Employer> {
  const was = draftOf(e);
  const out: Record<string, unknown> = {};
  for (const f of FIELDS) {
    const v = draft[f.key];
    if (v === was[f.key]) continue;
    // Semicolons, not commas: an office is "City, ST", and a comma would cut it in two.
    if (f.kind === "list") out[f.key] = v.split(";").map((s) => s.trim()).filter(Boolean);
    else if (f.kind === "yesno") out[f.key] = v === "yes" ? true : v === "no" ? false : null;
    else if (f.kind === "number") out[f.key] = v.trim() === "" ? null : Number(v);
    else out[f.key] = v;
  }
  return out as Partial<Employer>;
}
