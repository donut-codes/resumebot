import type { SiteState } from "./where";

// --- employers (GET/PATCH /api/v1/employers)-----------------------------------------------------------------------

export interface Employer {
  id: string;
  name: string;
  aliases: string[];
  website: string | null;
  careers_url: string | null;
  /** Its page of employee reviews, when research recorded one (an https address). */
  reviews_url: string | null;
  headquarters: string | null;
  offices: string[];
  local: boolean | null;
  work_style: "remote" | "hybrid" | "onsite" | "mixed" | "unknown";
  employees: number | null;
  founded: number | null;
  industry: string | null;
  sectors: string[];
  ownership: "public" | "private" | "pe" | "vc" | "nonprofit" | "government" | "unknown";
  ai_native: boolean | null;
  events: { kind: string; date: string; note?: string; source?: string }[];
  /** Roles from this employer are gated out, set here by the owner. */
  gated?: boolean;
  gated_reason?: string | null;
  gated_at?: string | null;
  /** The one line gating or ungating it said of what it changed, in the answer to that edit only (docs/PRD.md 39.14). */
  said?: string;
  group: string | null;
  notes: string | null;
  sources: Record<string, { source: string; at: string; by: string }>;
  /** The job system it posts on (docs/PRD.md 28.1), how the app can read it, and its board. */
  ats?: string | null;
  ats_label?: string;
  board_url?: string | null;
  ats_confirmed?: boolean | null;
  fetched_at?: string | null;
  fetch_ok?: boolean | null;
  fetch_note?: string | null;
  open_roles?: number | null;
  /** How fast it is hiring, counted from its board (the app's own count). */
  hiring?: { open: number; opened: number; closed: number; days: number; since: string | null } | null;
  /** Its own site (docs/PRD.md 29.1): the watch switch, set on this page. Null when no address is known. `status` is
   * Where to look's words for it, and says "Off: <system> is switched off" when the owner switched its job system off.
   * `watched_for` is `in_play` while the watch is on because the app switched it on for a role in play (29.9). */
  site?: {
    id: string; url: string; on: boolean; state: SiteState; needs: string | null; status: string;
    watched_for?: "in_play" | null;
  } | null;
  roles_total?: number;
  roles_open?: number;
  /** How many of its roles are in Ready to Launch or later (the list's chip is washed when any are). */
  roles_ahead?: number;
  /** What happened to it, newest first (its page only): merges, splits, hard passes, research. */
  history?: { at: string; sentence: string }[];
  roles?: { id: string; title: string; status: string; fit: number | null; found: string | null }[];
}

/** The list's views (GET /employers?view=). */
export type EmployerView = "all" | "open" | "watched" | "hard_pass" | "local";

/** How many employers each view holds, of every one (the rail's counts). */
export type EmployerCounts = Record<EmployerView, number>;

/** What GET /employers asks for: a view, a name, an industry, where the page starts and how many. */
export interface EmployerQuery {
  view?: EmployerView;
  q?: string;
  industry?: string;
  cursor?: string | null;
  limit?: number;
}

/** A page of the employers in a view (GET /employers): `next` is where the page after it starts (null at the last),
 * `total` how many the view holds so narrowed. */
export interface EmployerPage {
  employers: Employer[];
  next: string | null;
  total: number;
  counts: EmployerCounts;
  industries: string[];
}

/** An employer as the live stream tells of it (the fields its list row shows that changed), or removed. */
export type LiveEmployer = (Partial<Employer> & { id: string }) | { id: string; deleted: true };

/** What merging an employer into another would move, counted (GET /employers/{id}/merge?into=): nothing is changed.
 * `keep` is the employer that stays and `away` the one in the address; `name` is what the one employer left would be
 * called and `names` the other names it would also go by; `folded` is how many postings were read under both (counted
 * once); `to_gated_out` is how many roles not yet applied to the hard pass would send to Gated out. */
export interface EmployerMergePreview {
  keep: { id: string; name: string; roles: number; postings: number; sites: number };
  away: { id: string; name: string; roles: number; postings: number; sites: number };
  name: string;
  names: string[];
  roles: number;
  postings: number;
  folded: number;
  sites: number;
  hard_pass: boolean;
  to_gated_out: number;
}

/** The answer to a merge: the employer that is left (`merged` is false when the other was already gone). */
export interface EmployerMerged {
  merged: boolean;
  employer: Employer;
  /** The list row of the one left, the one that went, and each view's count now. */
  rows: Employer[];
  removed: string[];
  counts: EmployerCounts;
}

/** What splitting roles out of an employer would move (GET /employers/{id}/split?roles=&name=): nothing is changed. */
export interface EmployerSplitPreview {
  from: { id: string; name: string };
  to: { id: string | null; name: string; exists: boolean };
  roles: number;
  postings: number;
  sites: number;
  names: string[];
  /** How many of the roles that move are in Gated out for the old employer's hard pass; they stay there. */
  still_gated_out: number;
}

/** The answer to a split: the employer the roles went to. */
export interface EmployerSplit {
  roles: number;
  postings: number;
  names: string[];
  still_gated_out: number;
  employer: Employer;
  /** The list rows of both employers, and each view's count now. */
  rows: Employer[];
  counts: EmployerCounts;
}
