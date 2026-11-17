// Dates in words near the present, absolute beyond two weeks (the ui spec
// "Copy"). Plain dates ("2026-10-03") are calendar days and carry no zone. An instant (a full ISO datetime) is shown in
// the OWNER's zone -- the install's IANA zone, which the page learns from GET /me (setOwnerZone) -- never the browser's,
// so a trip or a shared install shows one clock time for one moment, the same one the server writes in its own words.

import { plural } from "./text";

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

let ownerZone: string | undefined;

/** Tells the page the owner's IANA zone ("Pacific/Auckland"), from /me. An empty or unknown name leaves the browser's own zone. */
export function setOwnerZone(zone: string | null | undefined): void {
  if (zone) {
    try {
      new Intl.DateTimeFormat("en-US", { timeZone: zone });
      ownerZone = zone;
      return;
    } catch {
      // not a zone this browser knows: fall through
    }
  }
  ownerZone = undefined;
}

/** The owner's zone as set, or undefined when the page is using the browser's. */
export function getOwnerZone(): string | undefined {
  return ownerZone;
}

interface Parts { year: number; month: number; day: number; hour: number; minute: number; second: number }

/** The calendar fields of an instant on the owner's wall clock. */
function partsOf(d: Date): Parts {
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: ownerZone, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric",
    hour: "numeric", minute: "numeric", second: "numeric",
  });
  const get: Record<string, string> = {};
  for (const p of fmt.formatToParts(d)) get[p.type] = p.value;
  return {
    year: Number(get.year), month: Number(get.month), day: Number(get.day),
    hour: Number(get.hour) % 24, minute: Number(get.minute), second: Number(get.second),
  };
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** What a datetime-local box holds for an instant: the owner's date and time, "2026-10-03T21:13". */
export function toOwnerLocalValue(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = partsOf(d);
  return `${p.year}-${pad2(p.month)}-${pad2(p.day)}T${pad2(p.hour)}:${pad2(p.minute)}`;
}

/** The instant (UTC ISO text) for a datetime-local value typed as the owner's wall-clock time; null when it is not one. */
export function fromOwnerLocalValue(value: string): string | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value);
  if (!m) return null;
  const [y, mo, d, h, mi] = m.slice(1).map(Number);
  const wall = Date.UTC(y, mo - 1, d, h, mi);
  // Start from the wall time read as UTC, then move by the zone's offset there, settling again across a clock change.
  let guess = wall;
  for (let i = 0; i < 3; i++) {
    const p = partsOf(new Date(guess));
    const shown = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute);
    const next = guess - (shown - wall);
    if (next === guess) break;
    guess = next;
  }
  const out = new Date(guess);
  return Number.isNaN(out.getTime()) ? null : out.toISOString();
}

/** A short name for the owner's zone ("CST", "GMT+13"), to label a box where a time is typed. */
export function ownerZoneLabel(at: Date = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: ownerZone, timeZoneName: "short" }).formatToParts(at);
  return parts.find((p) => p.type === "timeZoneName")?.value ?? "";
}

/** Milliseconds from `now` to the next midnight on the owner's clock (at least one second, so a timer cannot spin). */
export function msUntilOwnerMidnight(now: Date = new Date()): number {
  const p = partsOf(now);
  const sinceMidnight = ((p.hour * 60 + p.minute) * 60 + p.second) * 1000 + (now.getTime() % 1000);
  return Math.max(1000, 86_400_000 - sinceMidnight);
}

/** Today on the owner's clock, "2026-10-03". */
export function ownerToday(now: Date = new Date()): string {
  const p = partsOf(now);
  return `${p.year}-${pad2(p.month)}-${pad2(p.day)}`;
}

/** Newest first, by instant (never by text: offsets and fractions differ). A missing time sorts last. */
export function byNewest(a: string | null | undefined, b: string | null | undefined): number {
  const x = a ? parseInstant(a).getTime() : -Infinity;
  const y = b ? parseInstant(b).getTime() : -Infinity;
  return x === y ? 0 : y > x ? 1 : -1;
}

/** Whether instant `a` is earlier than instant `b`. */
export function isBefore(a: string, b: string): boolean {
  return parseInstant(a).getTime() < parseInstant(b).getTime();
}

function parseISODate(iso: string): Date {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d);
}

function absolute(iso: string): string {
  const d = parseISODate(iso);
  return `${MONTHS[d.getMonth()]} ${d.getDate()}`;
}

/** Days from `todayIso` to `iso` (negative if `iso` is in the past). */
export function daysBetween(todayIso: string, iso: string): number {
  const a = parseISODate(todayIso);
  const b = parseISODate(iso);
  return Math.round((b.getTime() - a.getTime()) / 86_400_000);
}

/**
 * A due date in words. `today` defaults to the real today (ISO, local),
 * but callers should pass the server's `today` when they have one so the
 * board and the card agree.
 */
export function dueInWords(iso: string, todayIso: string = isoToday()): string {
  return dueInWordsFromDays(daysBetween(todayIso, iso), iso);
}

/**
 * Same wording as dueInWords, but from a pre-computed day count (e.g. the
 * server's `days_until_due`) rather than two dates — for screens that only
 * have the offset, not the server's notion of "today".
 */
function dueInWordsFromDays(days: number, iso: string): string {
  const abs = absolute(iso);
  if (days === 0) return `today, ${abs}`;
  if (days === 1) return `tomorrow, ${abs}`;
  if (days === -1) return `yesterday, ${abs}`;
  if (days > 1 && days <= 14) return `in ${days} days, ${abs}`;
  if (days < -1 && days >= -14) return `${-days} days ago, ${abs}`;
  if (days > 14) return abs;
  return `was due ${abs}`;
}

function isoToday(): string {
  return ownerToday();
}

const DATE_ONLY_RE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * For thread/log timestamps (full ISO datetime) or date-only values
 * (snoozed_until, paused_until, chase_at, waiting_since): "Sep 10" or
 * "Sep 10, 2024". A date-only string is parsed as a local calendar date
 * (never as UTC midnight), so it never renders a day early in US time
 * zones.
 */
export function dateInWords(iso: string, todayIso: string = isoToday()): string {
  const day = dayOf(iso);
  const year = parseISODate(day).getFullYear();
  return parseISODate(todayIso).getFullYear() === year ? absolute(day) : `${absolute(day)}, ${year}`;
}

/** A day for a table column: "Oct 7", or "Oct 7, 2025" when it is not this year. The owner's calendar day. */
export function shortDateInWords(iso: string, todayIso: string = isoToday()): string {
  const d = parseISODate(dayOf(iso));
  const text = `${MONTHS[d.getMonth()].slice(0, 3)} ${d.getDate()}`;
  return parseISODate(todayIso).getFullYear() === d.getFullYear() ? text : `${text}, ${d.getFullYear()}`;
}

/** The owner's calendar day of an instant, "2026-10-03": what makes two messages the same day. A plain date is itself. */
export function localDay(iso: string): string {
  return dayOf(iso);
}

function dayOf(iso: string): string {
  if (DATE_ONLY_RE.test(iso)) return iso;
  return ownerToday(parseInstant(iso));
}

function toIso(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** "Tomorrow" snooze preset. */
export function snoozeTomorrow(todayIso: string = isoToday()): string {
  const d = parseISODate(todayIso);
  d.setDate(d.getDate() + 1);
  return toIso(d);
}

const HAS_OFFSET_RE = /(Z|[+-]\d{2}:?\d{2})$/i;

/**
 * An instant from the API. The backend always sends an offset
 * (app/timeutil.py); should a naive datetime slip through, it is UTC (the
 * server stores UTC), never the viewer's local time -- `new Date()` would
 * otherwise read it as local and show the wrong hour. Rendering then uses
 * the owner's zone.
 */
function parseInstant(iso: string): Date {
  if (DATE_ONLY_RE.test(iso)) return parseISODate(iso);
  return new Date(HAS_OFFSET_RE.test(iso) ? iso : `${iso}Z`);
}

/** "9:13 pm" from a full ISO datetime, for the drawer's thread entries. */
export function timeInWords(iso: string): string {
  const p = partsOf(parseInstant(iso));
  let h = p.hour;
  const m = pad2(p.minute);
  const ampm = h >= 12 ? "pm" : "am";
  h = h % 12 || 12;
  return `${h}:${m} ${ampm}`;
}

/** "September 25, 9:13 pm" -- a thread entry's full date + time. */
export function dateTimeInWords(iso: string, todayIso: string = isoToday()): string {
  return `${dateInWords(iso, todayIso)}, ${timeInWords(iso)}`;
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** "Sat, Sep 27" -- used in toasts confirming a snooze date. */
export function dateWithWeekday(iso: string): string {
  const d = parseISODate(iso);
  return `${WEEKDAYS[d.getDay()]}, ${absolute(iso)}`;
}

/** "5 minutes ago" / "2 hours ago" / "3 days ago" -- the project page's
 * "Last activity ..." line: the page must answer "where are we?" at a glance. Falls back to the absolute date beyond 14 days,
 * same horizon as the rest of this file's relative wording. `now` defaults
 * to the real time but callers may pass a fixed one for tests. */
export function timeAgoInWords(iso: string, now: Date = new Date()): string {
  const then = parseInstant(iso);
  const seconds = Math.max(0, Math.round((now.getTime() - then.getTime()) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${plural(minutes, "minute")} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${plural(hours, "hour")} ago`;
  const days = Math.round(hours / 24);
  if (days <= 14) return `${plural(days, "day")} ago`;
  return dateInWords(iso);
}

/** "20 seconds ago" for a heartbeat: seconds under a minute, else `timeAgoInWords`. */
export function secondsAgoInWords(iso: string, now: Date = new Date()): string {
  const seconds = Math.max(0, Math.round((now.getTime() - parseInstant(iso).getTime()) / 1000));
  if (seconds < 60) return `${plural(seconds, "second")} ago`;
  return timeAgoInWords(iso, now);
}

/** "Was due yesterday" / "Was due Sep 24" -- past-tense wording for the
 * "overdue" attention reason (the ui spec), always with a relative word
 * for yesterday/today, otherwise the absolute date. */
export function wasDueInWords(iso: string, todayIso: string = isoToday()): string {
  const days = daysBetween(todayIso, iso);
  if (days === 0) return "Was due today";
  if (days === -1) return "Was due yesterday";
  return `Was due ${absolute(iso)}`;
}
