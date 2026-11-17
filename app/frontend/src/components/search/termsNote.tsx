// The known site terms (docs/PRD.md 55.10): one warning line on a site whose terms forbid automated tools, with a link to
// its terms page. It informs and changes nothing: nothing is blocked, switched off or asked. The list comes with the
// sites (GET /sites) and the match is made here, on what is on screen or typed, so a typed address never leaves the computer.

import type { TermsEntry } from "../../api/types/where";

/** The owner's wording, in the one place it is written. */
export const TERMS_WARNING = "Warning: automated tools on this site may violate its terms of service";

/** An address's host as the server's `avoid.host_of` reads it: lowercase, no "www.", no trailing dot, no port; null when
 * there is none. An address with no scheme is read as https. */
export function hostName(address: string): string | null {
  const text = address.trim();
  if (!text) return null;
  try {
    const host = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(text) ? text : `https://${text}`).hostname;
    return host.replace(/\.$/, "").replace(/^www\./, "") || null;
  } catch {
    return null;
  }
}

const covers = (listed: string, host: string) => host === listed || host.endsWith(`.${listed}`);

/** The known entry that covers any of these hosts (a host is an entry's own or one of its subdomains, on a label
 * boundary, so a look-alike never matches); the one with the longest host wins. Null when none. The same rule as the
 * server's `avoid.covers`. */
export function termsForHosts(hosts: string[], terms: TermsEntry[] | undefined): TermsEntry | null {
  let best: TermsEntry | null = null;
  let length = 0;
  for (const raw of hosts) {
    const host = hostName(raw);
    if (!host) continue;
    for (const entry of terms ?? []) {
      for (const listed of entry.hosts) {
        if (listed.length > length && covers(listed.toLowerCase(), host)) {
          best = entry;
          length = listed.length;
        }
      }
    }
  }
  return best;
}

/** The entry for one address. */
export const termsFor = (address: string, terms: TermsEntry[] | undefined) => termsForHosts([address], terms);

/** The warning and its link, or nothing when there is no entry. */
export function TermsNote({ entry }: { entry: TermsEntry | null }) {
  if (!entry) return null;
  return (
    <span className="fi-ss-quiet">
      {TERMS_WARNING}.{" "}
      <a href={entry.terms_url} target="_blank" rel="noreferrer noopener">
        Terms of service
      </a>
    </span>
  );
}
