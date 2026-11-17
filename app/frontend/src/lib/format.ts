// How numbers, dollars and times are written on screen: one place, one policy. The app reads United States postings and
// pay in dollars a year (README, "The app"), so figures are written the United States way ("18,881", "$141,000",
// "9:40 AM") whatever the browser's own language. Values sent to the API stay raw; these only touch what is shown.

import { getOwnerZone } from "./dates";

const LOCALE = "en-US";

/** A count with thousands separators ("18,881"). */
export function formatCount(n: number): string {
  return new Intl.NumberFormat(LOCALE).format(n);
}

/** A whole number of dollars ("$141,000"). */
export function formatDollars(n: number): string {
  return `$${formatCount(Math.round(n))}`;
}

/** A time of day as "9:40 AM", on the owner's clock (lib/dates.ts `setOwnerZone`). */
export function formatTime(at: Date): string {
  return at.toLocaleTimeString(LOCALE, { hour: "numeric", minute: "2-digit", timeZone: getOwnerZone() });
}
