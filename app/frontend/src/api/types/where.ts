// --- where to look (GET/POST /api/v1/sites, PATCH and DELETE /api/v1/sites/{id}) -----------------------------------

/** Where a site stands: off (not watched), waiting (switched on; it starts in tonight's mission), learning (finding
 * how to read it), needs_you (stuck on something only the owner can do), working (read every night), stopped, browser
 * (switched on and read in the owner's own browser). */
export type SiteState = "off" | "waiting" | "learning" | "needs_you" | "working" | "stopped" | "browser";
/** The owner's two lists: a site to search, or a site to avoid (never read; a posting that links to one is stopped). */
export type SiteList = "search" | "avoid";
/** How a site is read: tested by resumebot (the server tries it once; if that doesn't work, the owner's browser reads
 * it), the app on the server, or the owner's own browser (a site a server cannot read). */
export type SiteReadBy = "auto" | "app" | "browser";
/** What the test of a site left to resumebot found. */
export type SiteFound = "app" | "browser";

/** A site on one of the owner's lists. Only the ones to search that the owner switched on are watched; a site to avoid
 * is never on. An employer's site is listed only while it is switched on. The page lists only the sites read on their
 * own: an employer read through a job system is under that system instead. */
export interface Site {
  id: string;
  url: string;
  label: string;
  on: boolean;
  /** The employer record this site belongs to (an employer's careers page), or null. */
  employer_id: string | null;
  state: SiteState;
  /** What is true of it now, in plain words, from its state and the missions behind it ("Read nightly at 12:30 AM",
   * "Waits: the scouting mission is paused"); empty when it is off, except a site the app switched off because it can't
   * be read: "Off: can't be read (its listings only load in a browser)". */
  status: string;
  /** "app" when resumebot switched the site off itself because it can't be read (the owner did not); null otherwise. */
  off_by?: "app" | null;
  /** Why the app switched it off, as one plain clause; null otherwise. */
  off_note?: string | null;
  /** When the app switched it off (with `off_by`). */
  off_at?: string | null;
  /** On and read by code alone: a stored recipe, a job system's adapter or a feed (listed under API). */
  by_code?: boolean;
  skill: { version: number; kind: string } | null;
  /** What it needs from the owner, or why it stopped. */
  needs: string | null;
  /** Roles found there that reached Found: 0 when there are none. */
  roles: number;
  list: SiteList;
  read_by: SiteReadBy;
  /** For a site left to resumebot ("auto"): what its test found, or null before it has been tested. */
  read_found: SiteFound | null;
  /** Why, in one plain sentence ("It asks for a sign-in."). */
  read_note: string | null;
  /** When it was added to its list. */
  created_at: string;
  /** The one line adding it to the sites to avoid said of what it changed, in the answer to that add only (docs/PRD.md 39.14). */
  said?: string;
}

/** A job system employers post on (an applicant tracking system), as Where to look lists it. One the app has a reader for has a
 * switch (`can_read`); the night's read skips the employers on one that is off. `status` is the server's words. */
export interface JobSystem {
  key: string;
  name: string;
  can_read: boolean;
  on: boolean;
  status: string;
  /** The hosts it serves employers' pages on (a host and its subdomains), to match against the known site terms. */
  domains: string[];
  /** Roles found at it that reached Found: 0 when there are none. */
  roles: number;
  /** The employers the owner watches that post on it, by name (each links to its own page). */
  employer_names: { id: string; name: string }[];
}

/** A known site whose terms forbid automated tools: the hosts it covers (and their subdomains) and its terms page
 * (docs/PRD.md 55.10). The page shows one warning line for a match; nothing is blocked. */
export interface TermsEntry {
  hosts: string[];
  terms_url: string;
}

/** One role a site or a job system brought in: just enough to name it, say where it stands and link to it. */
export interface SiteRole {
  id: string;
  title: string;
  company: string;
  /** Its status (a stage's word: "first_impression", "applied", "gated_out"...). */
  status: string;
  /** Its fit as stored, and that as a share of what the owner's asks allow (what the Pipeline's chip shows); each null
   * before it is scored. A site's and a job system's lists carry them; the finds for a target role do not (yet). */
  fit?: number | null;
  fit_pct?: number | null;
  /** When it reached Found. */
  found_at: string;
}

/** A page of the roles found at one place that reached Found, newest first. `total` counts all of them. */
export interface SiteRolesPage {
  total: number;
  items: SiteRole[];
}

/** A target role's finds over the last days: `total` is how many reached Found. */
export interface RoleTrend {
  total: number;
}
