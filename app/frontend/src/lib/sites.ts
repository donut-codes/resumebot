import type { Site, SiteFound } from "../api/types/where";

/** Who reads a site, by the same rule as the server's `effective_read`: the owner's choice, or, for a site left to
 * resumebot to test, what the test found. `null` before it has been tested, and then nobody reads it. */
export function effectiveRead(site: Pick<Site, "read_by" | "read_found">): SiteFound | null {
  return site.read_by === "auto" ? site.read_found : site.read_by;
}
