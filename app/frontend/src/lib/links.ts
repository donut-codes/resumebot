// An address the app read off the web (a posting, an employer's site) is shown as a link only when it is a web address:
// never `javascript:`, `data:` or a bare path. The server keeps only web addresses too; this is the second check, at the
// link itself.

/** `url` when it is an absolute http(s) address, else `undefined` (so no link is drawn). */
export function safeHref(url: string | null | undefined): string | undefined {
  if (typeof url !== "string") return undefined;
  const text = url.trim();
  try {
    const parsed = new URL(text);
    return (parsed.protocol === "http:" || parsed.protocol === "https:") && parsed.hostname ? text : undefined;
  } catch {
    return undefined;
  }
}

/** An address as a short host ("jobs.example.org"): no "www.", and an address with no scheme is read as https. The text
 * itself when it is not an address. */
export function hostOf(url: string): string {
  try {
    return new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(url) ? url : `https://${url}`).host.replace(/^www\./, "");
  } catch {
    return url;
  }
}
