// Import your resume, added to the master resume: what reading a resume gave is merged into the master the page holds,
// and never takes anything away. Nothing the owner wrote is deleted, replaced or reworded: the app never rewords the
// master, and changes to it come only from the owner (the project rules). Each line read is filed where it
// belongs (a role's bullet under the role it matches by employer, title and dates, or under a new role), a line the
// master already holds is skipped, and a line close to one it holds is kept beside that line, marked as similar, for the
// owner to keep, merge or delete. The plan says what will be added and where, for the owner to confirm before anything
// is put into the page (ImportReview); even then nothing is saved until the page is saved.

import type { ResumeBullet, ResumeLine, ResumeRole } from "../../api/types/me";
import type { ParsedContact, ParsedResume } from "../../api/types/profile";
import { freshId, type Master } from "./meParts";

/** What becomes of one line read from the resume: added, added beside the line it resembles, or skipped as one the master holds. */
export type Fate = "new" | "similar" | "duplicate";

export interface PlannedLine {
  text: string;
  fate: Fate;
  /** The line in the master it repeats or resembles. */
  like?: string;
  /** What the line is, where the group holds more than one kind ("Scope line", "Phone", an area's name). */
  label?: string;
}

/** The lines going into one part of the master resume, or into one role. */
export interface PlanGroup {
  key: string;
  title: string;
  /** Where they go, in words: "New role", "Already in your resume". */
  where?: string;
  lines: PlannedLine[];
  /** What was kept as it was and why: dates that differ, a detail the master already has. */
  notes: string[];
}

export interface ImportPlan {
  groups: PlanGroup[];
  counts: Record<Fate, number>;
  /** Whether any contact detail goes into the facts forms ask for. */
  contact: boolean;
  /** The master resume with every addition in it: what the page takes when the owner confirms. */
  merged: Master;
}

/** The note a line close to one the master holds carries, sitting under that line. */
export const SIMILAR_NOTE = "Similar to the line above (imported): keep one, merge them or keep both";

/** The words of a line for comparing: lower case, punctuation and spacing ignored. */
export const normal = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();

const SMALL = new Set(["the", "and", "for", "with", "from", "into", "that", "this", "our", "was", "were", "are", "its", "of", "to", "in", "on", "a", "an", "at", "by", "as", "or"]);
const words = (s: string) => new Set(normal(s).split(" ").filter((w) => w.length > 1 && !SMALL.has(w)));

/** The fewest words two lines must share to be alike: short plain lines ("set up workflows", "set up projects") share
 * a verb and are still different facts. */
const SHARED_AT_LEAST = 3;

/** How alike two lines are, 0 to 1, by the words they share (the overlap coefficient); 0 when they share fewer than three. */
export function likeness(a: string, b: string): number {
  const x = words(a);
  const y = words(b);
  if (x.size === 0 || y.size === 0) return 0;
  let both = 0;
  for (const w of x) if (y.has(w)) both += 1;
  if (both < SHARED_AT_LEAST) return 0;
  return (2 * both) / (x.size + y.size);
}

/** How alike two lines must be to be marked similar. */
export const SIMILAR_AT = 0.6;

const same = (a: string, b: string) => normal(a) !== "" && normal(a) === normal(b);

/** The line among `texts` most like `text`, at or over SIMILAR_AT: its place, or -1. */
function mostLike(text: string, texts: string[]): number {
  let best = -1;
  let score = SIMILAR_AT;
  texts.forEach((t, i) => {
    const s = likeness(text, t);
    if (s >= score) {
      best = i;
      score = s;
    }
  });
  return best;
}

/** Lines read, added to lines held: a repeat is skipped, a near repeat goes right under the line it resembles (after any
 * already put there) with the similar note, and the rest go at the end. The lines held are never changed or moved. */
function mergeLines<T extends ResumeLine>(held: T[], read: string[], make: (text: string, note: string) => T): { items: T[]; planned: PlannedLine[] } {
  const items = [...held];
  const planned: PlannedLine[] = [];
  for (const text of read) {
    const repeat = items.find((i) => same(i.text, text));
    if (repeat) {
      planned.push({ text, fate: "duplicate", like: repeat.text });
      continue;
    }
    const at = mostLike(text, items.map((i) => i.text));
    if (at >= 0) {
      let after = at + 1;
      while (after < items.length && items[after].note === SIMILAR_NOTE) after += 1;
      items.splice(after, 0, make(text, SIMILAR_NOTE));
      planned.push({ text, fate: "similar", like: items[at].text });
    } else {
      items.push(make(text, ""));
      planned.push({ text, fate: "new" });
    }
  }
  return { items, planned };
}

const line = (text: string, note: string): ResumeLine => ({ id: freshId(), text, note });
const bullet = (text: string, note: string): ResumeBullet => ({ id: freshId(), text, group: "", note });
const dates = (start: string, end: string) => [start, end].filter(Boolean).join(" – ") || "no dates";
const year = (d: string) => /\d{4}/.exec(d)?.[0] ?? "";

type ReadRole = ParsedResume["roles"][number];

/** The role in the master a role read from a resume is: the same employer, and the same title or the same start year. */
function matchRole(roles: ResumeRole[], x: ReadRole): ResumeRole | undefined {
  const here = roles.filter((o) => same(o.employer, x.employer));
  return here.find((o) => same(o.title, x.title)) ?? here.find((o) => year(o.start) !== "" && year(o.start) === year(x.start));
}

/** A short detail of a role or of the facts, kept: filled in when the master has none, left as it is when it has another. */
function fillDetail(held: string, read: string, name: string, planned: PlannedLine[], notes: string[]): string {
  if (!read.trim()) return held;
  if (!held.trim()) {
    planned.push({ text: read, fate: "new", label: name });
    return read;
  }
  if (same(held, read)) planned.push({ text: read, fate: "duplicate", like: held, label: name });
  else notes.push(`${name}: yours is kept ("${held}"); this resume says "${read}".`);
  return held;
}

/** A paragraph read beside one held: filled when empty, skipped when the same, otherwise added after it, never in its place. */
function addParagraph(held: string, read: string, label: string, gap: string, planned: PlannedLine[]): string {
  if (!read.trim()) return held;
  if (!held.trim()) {
    planned.push({ text: read, fate: "new", label });
    return read;
  }
  if (same(held, read)) {
    planned.push({ text: read, fate: "duplicate", like: held, label });
    return held;
  }
  const near = likeness(held, read) >= SIMILAR_AT;
  planned.push({ text: read, fate: near ? "similar" : "new", like: near ? held : undefined, label: `${label}, added after yours` });
  return `${held}${gap}${read}`;
}

function mergeRoles(held: ResumeRole[], read: ReadRole[], groups: PlanGroup[]): ResumeRole[] {
  const roles = [...held];
  // Which held role each role read is, worked out first, so a new role can be put in the resume's own order.
  const matched = read.map((x) => matchRole(held, x));
  read.forEach((x, i) => {
    const planned: PlannedLine[] = [];
    const notes: string[] = [];
    const was = matched[i] ?? matchRole(roles, x); // a role the same resume gave twice is one role
    if (was) {
      const at = roles.findIndex((o) => o.id === was.id);
      const role = { ...roles[at] };
      for (const key of ["start", "end"] as const) {
        if (!role[key].trim() && x[key].trim()) {
          role[key] = x[key];
          notes.push(`${key === "start" ? "Start" : "End"} date filled in: ${x[key]}.`);
        }
      }
      if ((x.start && !same(role.start, x.start)) || (x.end && !same(role.end, x.end))) {
        notes.push(`Dates differ: yours say ${dates(role.start, role.end)}, this resume says ${dates(x.start, x.end)}. Yours are kept.`);
      }
      role.location = fillDetail(role.location, x.location, "Place", planned, notes);
      role.descriptor = fillDetail(role.descriptor, x.descriptor, "What the employer is", planned, notes);
      role.intro = addParagraph(role.intro, x.intro, "Scope line", " ", planned);
      const merged = mergeLines(role.bullets, x.bullets, bullet);
      role.bullets = merged.items;
      planned.push(...merged.planned);
      roles[at] = role;
      groups.push({ key: `role-${was.id}`, title: roleName(role), where: `Already in your resume (${dates(role.start, role.end)})`, lines: planned, notes });
      return;
    }
    const bullets = mergeLines([], x.bullets, bullet);
    const role: ResumeRole = {
      id: freshId(), employer: x.employer, location: x.location, title: x.title, start: x.start, end: x.end, intro: x.intro,
      descriptor: x.descriptor, optional: false, note: "", tools: [], bullets: bullets.items, evidence: [],
    };
    // Before the next role this resume lists that the master already holds, so the order stays the resume's; else last.
    const next = matched.slice(i + 1).find(Boolean);
    const at = next ? roles.findIndex((o) => o.id === next.id) : -1;
    if (at >= 0) roles.splice(at, 0, role);
    else roles.push(role);
    const head: PlannedLine[] = x.intro ? [{ text: x.intro, fate: "new", label: "Scope line" }] : [];
    groups.push({ key: `new-role-${i}`, title: roleName(role), where: `New role (${dates(x.start, x.end)})`, lines: [...head, ...bullets.planned], notes });
  });
  return roles;
}

const roleName = (x: { title: string; employer: string }) => [x.title || "A role", x.employer].filter(Boolean).join(", ");

function mergeCompetencies(held: Master["competencies"], read: ParsedResume["competencies"], groups: PlanGroup[]): Master["competencies"] {
  const out = held.map((c) => ({ ...c, items: [...c.items] }));
  const planned: PlannedLine[] = [];
  for (const c of read) {
    let area = out.find((o) => same(o.area, c.area)) ?? (c.area.trim() === "" ? out[0] : undefined);
    if (!area) {
      area = { id: freshId(), area: c.area, items: [] };
      out.push(area);
    }
    for (const term of c.items) {
      const there = out.find((o) => o.items.some((t) => same(t, term)));
      if (there) planned.push({ text: term, fate: "duplicate", like: there.items.find((t) => same(t, term)), label: there.area });
      else {
        area.items.push(term);
        planned.push({ text: term, fate: "new", label: area.area });
      }
    }
  }
  if (planned.length) groups.push({ key: "competencies", title: "Core competencies", lines: planned, notes: [] });
  return out;
}

function mergeEducation(held: Master["education"], read: ParsedResume["education"], groups: PlanGroup[]): Master["education"] {
  const out = [...held];
  const planned: PlannedLine[] = [];
  const name = (e: { degree: string; school: string }) => [e.degree, e.school].filter(Boolean).join(", ");
  for (const e of read) {
    const repeat = out.find((o) => same(name(o), name(e)));
    if (repeat) {
      planned.push({ text: name(e), fate: "duplicate", like: name(repeat) });
      continue;
    }
    const at = out.findIndex((o) => (o.school.trim() === "" || same(o.school, e.school)) && likeness(o.degree, e.degree) >= SIMILAR_AT);
    const entry = { id: freshId(), degree: e.degree, school: e.school, note: at >= 0 ? SIMILAR_NOTE : "" };
    if (at >= 0) {
      out.splice(at + 1, 0, entry);
      planned.push({ text: name(e), fate: "similar", like: name(out[at]) });
    } else {
      out.push(entry);
      planned.push({ text: name(e), fate: "new" });
    }
  }
  if (planned.length) groups.push({ key: "education", title: "Education", lines: planned, notes: [] });
  return out;
}

function mergeContact(m: Master, c: ParsedContact, groups: PlanGroup[]): Pick<Master, "name" | "location" | "address" | "phone" | "email" | "links"> {
  const planned: PlannedLine[] = [];
  const notes: string[] = [];
  const out = {
    name: fillDetail(m.name, c.name, "Name", planned, notes),
    location: fillDetail(m.location, c.location, "Where you live", planned, notes),
    address: fillDetail(m.address, c.address, "Street address", planned, notes),
    phone: fillDetail(m.phone, c.phone, "Phone", planned, notes),
    email: fillDetail(m.email, c.email, "Email", planned, notes),
    links: [...m.links],
  };
  for (const link of c.links) {
    if (out.links.some((l) => same(l, link))) planned.push({ text: link, fate: "duplicate", like: link, label: "Link" });
    else {
      out.links.push(link);
      planned.push({ text: link, fate: "new", label: "Link" });
    }
  }
  if (planned.length || notes.length) groups.push({ key: "facts", title: "Facts forms ask for", lines: planned, notes });
  return out;
}

/** What adding a resume that was read to the master resume would do, and the master with it done. The master given is
 * never changed: every line it holds is in the result, in its place, with its words, id and note. */
export function planImport(m: Master, p: ParsedResume): ImportPlan {
  const groups: PlanGroup[] = [];
  const partLines = (key: string, title: string, held: ResumeLine[], read: string[]) => {
    const merged = mergeLines(held, read, line);
    if (merged.planned.length) groups.push({ key, title, lines: merged.planned, notes: [] });
    return merged.items;
  };

  // The summary: filled when there is none; a different one is kept as another version of it, never in its place.
  let summary = m.summary;
  let variants = m.summary_variants;
  if (p.summary.trim()) {
    const planned: PlannedLine[] = [];
    if (!summary.trim()) {
      summary = p.summary;
      planned.push({ text: p.summary, fate: "new" });
    } else {
      const repeat = [summary, ...variants.map((v) => v.text)].find((t) => same(t, p.summary));
      if (repeat) planned.push({ text: p.summary, fate: "duplicate", like: repeat });
      else {
        const near = likeness(summary, p.summary) >= SIMILAR_AT;
        variants = [...variants, line(p.summary, near ? SIMILAR_NOTE : "")];
        planned.push({ text: p.summary, fate: near ? "similar" : "new", like: near ? summary : undefined, label: "Kept as another version of your summary" });
      }
    }
    groups.push({ key: "summary", title: "Summary", lines: planned, notes: [] });
  }

  const competencies = mergeCompetencies(m.competencies, p.competencies, groups);
  const achievements = partLines("achievements", "Selected achievements", m.achievements, p.achievements);
  const roles = mergeRoles(m.roles, p.roles, groups);
  const education = mergeEducation(m.education, p.education, groups);
  const awards = partLines("awards", "Awards and recognition", m.awards, p.awards);
  const projects = partLines("projects", "Personal projects", m.projects, p.projects);
  const philosophyLines: PlannedLine[] = [];
  const philosophy = addParagraph(m.philosophy, p.philosophy, "Philosophy", "\n\n", philosophyLines);
  if (philosophyLines.length) groups.push({ key: "philosophy", title: "Leadership philosophy", lines: philosophyLines, notes: [] });
  const before = groups.length;
  const contact = mergeContact(m, p.contact, groups);

  const counts: Record<Fate, number> = { new: 0, similar: 0, duplicate: 0 };
  for (const g of groups) for (const l of g.lines) counts[l.fate] += 1;
  const contactAdded = groups.slice(before).some((g) => g.lines.some((l) => l.fate === "new"));
  return {
    groups,
    counts,
    contact: contactAdded,
    merged: { ...m, summary, summary_variants: variants, competencies, achievements, roles, education, awards, projects, philosophy, ...contact },
  };
}
