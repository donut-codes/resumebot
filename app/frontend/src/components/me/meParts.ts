// What each Me page is made of: its parts, in order, with the words that name them and the fields of the document each
// one holds (so the save bar can say which parts have changes), the blank a new line starts from, and what is left
// out of a save (a line added and never written). The shapes are the backend's (backend/app/jobsearch/profile/documents.py).

import type { About, Resume, Writing } from "../../api/types/me";
import type { WritingPage } from "../../lib/route";
import { newId } from "../../lib/ids";

/** What a Me page is given: its document as saved, the version that is (the one every save of it names, so a page left open
 * cannot write over newer work), and what to do when a save has gone through (with the version now in force). */
export interface TabProps<T> {
  saved: T;
  version: number;
  onSaved: (doc: T, version: number) => void;
}

export interface PartDef {
  id: string;
  title: string;
  /** The one plain line under an open part's heading. Left out when the heading says it all. */
  hint?: string;
  /** The fields of the page's document this part holds: a change to any of them is a change to this part. */
  keys: string[];
}

/** A section longer than this many lines of reading starts folded (PRD path 19.3). */
export const FOLD_OVER_LINES = 30;
const CHARS_PER_LINE = 80;

/** About how many lines of reading some text takes: a line for each break, and one for each 80 characters. Nothing
 * written takes none. */
export function linesOf(...texts: string[]): number {
  return texts.reduce(
    (total, text) => (text === "" ? total : total + text.split("\n").reduce((n, line) => n + Math.max(1, Math.ceil(line.length / CHARS_PER_LINE)), 0)),
    0,
  );
}

/** A new line's id: a slug of its first words, unique among its siblings (the backend wants lowercase words and hyphens). */
function newItemId(words: string, siblings: { id?: string }[]): string {
  return newId({ name: words.split(/\s+/).slice(0, 5).join(" ") }, siblings);
}

const filled = (...texts: string[]) => texts.some((t) => t.trim() !== "");

// A tilde, which a slug never holds: a saved line whose words begin "New ..." (id "new-product-launch") is not mistaken for one just added.
const NEW = "~new-";
let added = 0;
/** The id of a line that was just added: a placeholder, made a slug of its words when the page is saved (`settle`). */
export const freshId = () => `${NEW}${++added}`;

/** Gives the lines added since the last save their real ids: a slug of the words they were written with, so the history
 * can name them ("changed rule one-page"), unique among their siblings. Lines that were already saved keep theirs. */
export function settle<T extends { id: string }>(items: T[], words: (item: T) => string): T[] {
  const taken: { id?: string }[] = items.filter((i) => !i.id.startsWith(NEW));
  return items.map((i) => {
    if (!i.id.startsWith(NEW)) return i;
    const id = newItemId(words(i), taken);
    taken.push({ id });
    return { ...i, id };
  });
}

// --- the master resume ------------------------------------------------------------------------------------------

/** The master resume's parts in the order a built resume prints them (Professional Summary, Core Competencies, Selected
 * Achievements, Professional Experience, Education), then the parts the writer draws on but never prints as a section.
 * Each says how it is used. */
const RESUME_PARTS: PartDef[] = [
  { id: "summary", title: "Summary", hint: "Each resume's Professional Summary is written from this, tailored to the posting.", keys: ["summary"] },
  {
    id: "competencies",
    title: "Core competencies",
    hint: "Printed as Core Competencies, with the areas and terms that fit the posting. Separate the terms in an area with semicolons.",
    keys: ["competencies"],
  },
  {
    id: "achievements",
    title: "Selected achievements",
    hint: "Printed as Selected Achievements: the ones that fit the posting.",
    keys: ["achievements"],
  },
  {
    id: "roles",
    title: "Experience",
    hint: "Printed as Professional Experience: employer, title and dates as written here, with the bullets that fit the posting, most relevant first. A role marked optional is left off unless something in it serves a qualification the posting asks for. Each role's evidence is never printed: the analyst cites it as proof when grading a posting's requirements, and the writer draws on it for detail. The tools used in a role, with its dates, answer the years a form asks for.",
    keys: ["roles"],
  },
  { id: "education", title: "Education", hint: "Printed as Education: each degree and school as written here.", keys: ["education"] },
  {
    id: "awards",
    title: "Awards and recognition",
    hint: "Not a section of the resume: the writer draws on these for detail when tailoring a resume or cover letter.",
    keys: ["awards"],
  },
  {
    id: "projects",
    title: "Personal projects",
    hint: "Not a section of the resume: the writer draws on these for detail when tailoring a resume or cover letter.",
    keys: ["projects"],
  },
  {
    id: "philosophy",
    title: "Leadership philosophy",
    hint: "Not a section of the resume: the writer draws on it for voice and detail when tailoring a resume or cover letter.",
    keys: ["philosophy"],
  },
];

export const BLANK_LINE = () => ({ text: "", note: "" });
export const BLANK_BULLET = () => ({ text: "", group: "", note: "" });
export const BLANK_COMPETENCY = () => ({ area: "", items: [] as string[] });
export const BLANK_ROLE = () => ({
  employer: "", location: "", title: "", start: "", end: "", intro: "", descriptor: "", optional: false, note: "", tools: [] as string[], bullets: [], evidence: [],
});
export const BLANK_DEGREE = () => ({ degree: "", school: "", note: "" });
export const BLANK_EVIDENCE = () => ({ title: "", text: "" });
export const BLANK_RULE = () => ({ says: "" });

/** The resume as it is saved: lines that were added and never written are left out. */
export function pruneResume<T extends Resume>(r: T): T {
  const text = (l: { text: string }) => l.text;
  return {
    ...r,
    achievements: settle(r.achievements.filter((l) => filled(l.text)), text),
    competencies: settle(r.competencies.filter((c) => filled(c.area) || c.items.length > 0), (c) => c.area),
    roles: settle(
      r.roles
        .filter((x) => filled(x.title, x.employer))
        .map((x) => ({
          ...x,
          tools: x.tools.filter((t) => filled(t)),
          bullets: settle(x.bullets.filter((b) => filled(b.text)), text),
          evidence: settle(x.evidence.filter((e) => filled(e.title, e.text)), (e) => e.title || e.text),
        })),
      (x) => `${x.title} ${x.employer}`,
    ),
    education: settle(r.education.filter((e) => filled(e.degree, e.school)), (e) => e.degree || e.school),
    projects: settle(r.projects.filter((l) => filled(l.text)), text),
    awards: settle(r.awards.filter((l) => filled(l.text)), text),
  };
}

// --- the facts forms ask for (the first part of the master resume) ------------------------------------------------

export const FACTS_PART: PartDef = {
  id: "facts",
  title: "Facts forms ask for",
  hint: "Your name and contact details head every resume and cover letter. The rest answers what application forms ask.",
  keys: ["name", "location", "metro_location", "address", "phone", "email", "links", "work_authorization", "sponsorship", "current_company"],
};

/** One fact forms ask for: the field it is in, its name, and the one plain line after the name when it needs one. */
export type ScalarFact = "name" | "location" | "metro_location" | "address" | "phone" | "email" | "work_authorization" | "sponsorship" | "current_company";
export const FACTS: { key: ScalarFact | "links"; label: string; hint?: string }[] = [
  { key: "name", label: "Name" },
  { key: "location", label: "Where you live", hint: "As a resume header gives it" },
  { key: "metro_location", label: "For remote or out-of-town roles", hint: "The place to give when it differs" },
  { key: "address", label: "Street address", hint: "For forms that ask" },
  { key: "phone", label: "Phone" },
  { key: "email", label: "Email" },
  { key: "links", label: "Links", hint: "Separate with semicolons" },
  { key: "work_authorization", label: "Authorized to work" },
  { key: "sponsorship", label: "Needs visa sponsorship" },
  { key: "current_company", label: "Current or most recent company" },
];

/** The facts: the fields of the about document that the master resume page edits. Its `notes` stay as saved. */
export type Facts = Pick<About, ScalarFact | "links">;
const FACT_KEYS = FACTS_PART.keys as (keyof Facts)[];

/** What the master resume page edits as one draft: the resume with the facts beside it. The two documents share no
 * field the page edits, so they sit in one flat object and are told apart again on a save (`splitMaster`). */
export type Master = Resume & Facts;

/** The parts of the master resume page, in order: the facts first, since a resume opens with a name and contact details. */
export const MASTER_PARTS: PartDef[] = [FACTS_PART, ...RESUME_PARTS];

/** Whether a role has something a JSON Resume file would hold besides dates: the server leaves a role with none of these out
 * (`resume_export._work`). */
const roleHasText = (r: Resume["roles"][number]): boolean =>
  [r.employer, r.title, r.location, r.descriptor, r.intro].some((t) => t.trim() !== "") || r.bullets.some((b) => b.text.trim() !== "");

/** Whether a master resume has something to put in a JSON Resume file: a summary or a role with something in it (the server's
 * rule for its 409, `resume_export.nothing_to_export`). Achievements alone do not count: the standard has no place for them,
 * so they stay out. */
export const hasExport = (r: Resume): boolean => r.summary.trim() !== "" || r.roles.some(roleHasText);

export const masterOf = (r: Resume, a: About): Master => {
  const facts = Object.fromEntries(FACT_KEYS.map((k) => [k, a[k]])) as Facts;
  return { ...r, ...facts };
};

/** The two documents a draft of the master resume page is: the resume, and the about document with the facts put in
 * (its notes as they were saved). */
export function splitMaster(m: Master, about: About): { resume: Resume; about: About } {
  const resume: Record<string, unknown> = { ...m };
  const facts: Record<string, unknown> = {};
  for (const k of FACT_KEYS) {
    facts[k] = resume[k];
    delete resume[k];
  }
  return { resume: resume as unknown as Resume, about: { ...about, ...facts } as About };
}

// --- writing rules ----------------------------------------------------------------------------------------------

/** The writing rules are six sections, in this order: rules for your voice, for ATS screening, for interviews,
 * for the resume and for cover letters (Resume Rules and Cover Letter Rules, which have parts of their own below), then
 * settings for QA of documentation. Each is a list of one-point rules, and QA also holds what never appears, the
 * patterns reviewers catch, which of the app's style rules are switched off, the owner's certifications and licences,
 * and the rules for humanizing. */
export const VOICE_PART: PartDef = { id: "voice", title: "Rules for your voice", keys: ["voice_rules"] };
export const ATS_PART: PartDef = { id: "ats", title: "Rules for ATS screening", keys: ["ats_rules"] };
/** Rules for interviews: the owner's own interview habits, handed to interview prep with the voice rules and to no other job. */
export const INTERVIEW_PART: PartDef = {
  id: "interview",
  title: "Rules for interviews",
  hint: "Your own interview habits, one point to a rule: how you like to answer (stories, or a judgement with a one-clause receipt), a question you like to open or close with, the questions to ask in each round, and what you never say. Only interview prep reads these.",
  keys: ["interview_rules"],
};

/** The lists of rules for the sections of the resume, beside `resume_global`. */
export type ResumeSectionKey =
  | "resume_header"
  | "resume_summary"
  | "resume_competencies"
  | "resume_achievements"
  | "resume_experience"
  | "resume_education";

/** The lists of rules for the parts of the cover letter, beside `letter_global`. */
export type LetterSectionKey =
  | "letter_header"
  | "letter_greeting"
  | "letter_opening"
  | "letter_body"
  | "letter_closing"
  | "letter_signoff";

/** One section of a document whose rules come by section (the resume, the cover letter). */
export interface SectionDef<K extends string = ResumeSectionKey | LetterSectionKey> extends PartDef {
  field: K;
  /** The heading the built document prints for it. */
  printed: string;
  /** What it prints, in a line, for the skeleton of the document. */
  shape: string;
}
export type ResumeSectionDef = SectionDef<ResumeSectionKey>;
export type LetterSectionDef = SectionDef<LetterSectionKey>;

/** Resume Rules: the rules for the whole resume (Global), then one part for each section of the resume in the order it
 * prints them. The `hint` of each is its fixed best-practice note: product text, the same for every owner. */
export const GLOBAL_PART: PartDef = {
  id: "global",
  title: "Global",
  hint: "Applies to the whole resume: length, margins, fonts, file type, section order and dates. One to two pages, one common font at 10 to 12 points, one date format throughout, and a Word file or PDF unless the posting asks for another.",
  keys: ["resume_global", "page_margin_inches", "role_rules"],
};
export const RESUME_SECTIONS: ResumeSectionDef[] = [
  {
    id: "header",
    title: "Header",
    field: "resume_header",
    printed: "Name, contact line and headline",
    shape: "Your name, then one contact line, then your headline.",
    hint: "Your name and one contact line: city and state, phone, email and a profile link if you have one. Keep it in the body of the page, not the page header, where screening software skips it.",
    keys: ["resume_header"],
  },
  {
    id: "summary",
    title: "Summary",
    field: "resume_summary",
    printed: "Professional Summary",
    shape: "One short paragraph.",
    hint: "Three or four lines written for this posting, with no \"I\".",
    keys: ["resume_summary"],
  },
  {
    id: "competencies",
    title: "Core Competencies",
    field: "resume_competencies",
    printed: "Core Competencies",
    shape: "One line for each area: its name, then its terms.",
    hint: "The skills the posting names, in its words where they are true of you, grouped in a few short areas instead of one long list.",
    keys: ["resume_competencies"],
  },
  {
    id: "achievements",
    title: "Selected Achievements",
    field: "resume_achievements",
    printed: "Selected Achievements",
    shape: "A few one-line bullets.",
    hint: "A few of your strongest results, strongest first, with a number, a scale or a before and after only where you have a real one.",
    keys: ["resume_achievements"],
  },
  {
    id: "experience",
    title: "Experience",
    field: "resume_experience",
    printed: "Professional Experience",
    shape: "For each role: employer and place, title and dates, a line on the employer, a scope line, then bullets.",
    hint: "Newest role first. Start each bullet with a verb, then say what you did, how, and what came of it. Give a number only where you have a real one. Older roles get fewer bullets.",
    keys: ["resume_experience"],
  },
  {
    id: "education",
    title: "Education",
    field: "resume_education",
    printed: "Education",
    shape: "For each degree: the degree, then the school.",
    hint: "Degree and school, newest first. Add only certifications you actually hold, and only when the posting asks for one.",
    keys: ["resume_education"],
  },
];
export const RESUME_RULE_PARTS: PartDef[] = [GLOBAL_PART, ...RESUME_SECTIONS];

/** Cover Letter Rules: the rules for the whole letter (Global), then one part for each part of the letter in the order it
 * reads. The `hint` of each is its fixed note: product text, the same for every owner. */
export const LETTER_GLOBAL_PART: PartDef = {
  id: "global",
  title: "Global",
  hint: "Applies to the whole letter: length, tone, when a letter is written at all, and file type. One page, in your own voice, and a Word file or PDF unless the posting asks for another.",
  keys: ["letter_global", "letter_role_rules"],
};
export const LETTER_SECTIONS: LetterSectionDef[] = [
  {
    id: "header",
    title: "Header",
    field: "letter_header",
    printed: "Header",
    shape: "Your name, your contact line, the date and the employer, printed by the app.",
    hint: "Your name and contact line, the date and the employer, set at the top. The app prints them from your resume details and the role.",
    keys: ["letter_header"],
  },
  {
    id: "greeting",
    title: "Greeting",
    field: "letter_greeting",
    printed: "Greeting",
    shape: "One line addressed to a person.",
    hint: "A plain greeting by name when the posting or the company gives one. Skip \"To whom it may concern\".",
    keys: ["letter_greeting"],
  },
  {
    id: "opening",
    title: "Opening",
    field: "letter_opening",
    printed: "Opening",
    shape: "A sentence or two: the employer's problem and why this role.",
    hint: "Start with the problem the employer is hiring to solve and why this role, not with your history.",
    keys: ["letter_opening"],
  },
  {
    id: "body",
    title: "Body",
    field: "letter_body",
    printed: "Body",
    shape: "One or two paragraphs of proof against that problem.",
    hint: "One or two things you have done that bear on that problem, with a real number only where you have one. Add what the resume cannot say, not what it already does.",
    keys: ["letter_body"],
  },
  {
    id: "closing",
    title: "Closing",
    field: "letter_closing",
    printed: "Closing",
    shape: "The ask or the next step, in a sentence or two.",
    hint: "Say what you want next, such as a conversation, and be specific about it.",
    keys: ["letter_closing"],
  },
  {
    id: "signoff",
    title: "Sign-off",
    field: "letter_signoff",
    printed: "Sign-off",
    shape: "Closing words, then your name, which the app prints.",
    hint: "A short, plain close, then your name.",
    keys: ["letter_signoff"],
  },
];
export const LETTER_RULE_PARTS: PartDef[] = [LETTER_GLOBAL_PART, ...LETTER_SECTIONS];

/** A document whose rules come by section: Resume Rules and Cover Letter Rules are one page built from one of these. The
 * words are fixed product text, the same for every owner. */
export interface RuleDoc {
  id: "resume" | "letter";
  globalPart: PartDef;
  globalField: "resume_global" | "letter_global";
  /** Where the rules for each target role are kept: a map of its own for each document. */
  roleField: "role_rules" | "letter_role_rules";
  sections: SectionDef[];
  parts: PartDef[];
  /** The crib at the top: what the document is for, in a few short paragraphs, then seven things that help it. */
  crib: { title: string; paragraphs: string[]; practices: string[] };
  /** The skeleton in the side column: its title and line, what the whole document is called, and a line on the margin. */
  skeleton: { title: string; hint: string; whole: string; margin: (inches: number | null) => string };
  /** What is said under "Rules for each role", and of a role since deleted. */
  roleHint: string;
  goneNote: string;
  /** The side column's name. */
  sideLabel: string;
}

const inchesText = (n: number | null) => (n === null ? "the default" : `${n} inches`);

/** What a resume is for, who reads it and how to write one: a fixed crib at the top of Resume Rules. */
export const CRIB_FOR = "A resume has one job: to get you an interview. It is not your whole story, and each one is cut to a single posting.";
export const CRIB_READERS =
  "Screening software reads it first and sorts it by how well it matches the posting. A person then skims what is left for a few seconds, so what matters has to be where the eye lands.";
export const CRIB_PRACTICES = [
  "One to two pages.",
  "Use the posting's own words for skills and duties, where they are true of you.",
  "Show results, with real numbers where you have them, not adjectives.",
  "No tables, columns, text boxes, or headers and footers: they break the software's reading.",
  "Standard section names, such as Summary, Experience and Education.",
  "Newest first, in reverse-chronological order.",
  "Tailor each resume to its posting.",
];

/** What a cover letter is for, when one is worth writing and how to write one: a fixed crib at the top of Cover Letter
 * Rules. */
export const LETTER_CRIB_FOR =
  "A cover letter has one job: to give a person a reason to read your resume closely. It says what the resume cannot: why this employer, why this role, and what you would do about the problem they are hiring to solve.";
export const LETTER_CRIB_WHEN =
  "Many screeners skip it, and many postings never ask for one. It is worth the time when your path needs explaining, as with a change of career or a move to another city, when the role is senior, or when the posting asks for one.";
export const LETTER_CRIB_PRACTICES = [
  "Keep it to one page, and short enough to read in a minute.",
  "Lead with the employer's problem, not your history.",
  "Give one or two proofs that you have solved something like it, with a real number where you have one.",
  "Do not restate the resume; add what it cannot say.",
  "Close with a specific next step, not a vague hope.",
  "Greet a named person when you know who it is, and keep the greeting plain.",
  "Plain words, in your own voice, with no stock phrases.",
];

export const RULE_DOCS: Record<"resume" | "letter", RuleDoc> = {
  resume: {
    id: "resume",
    globalPart: GLOBAL_PART,
    globalField: "resume_global",
    roleField: "role_rules",
    sections: RESUME_SECTIONS,
    parts: RESUME_RULE_PARTS,
    crib: { title: "How a resume works", paragraphs: [CRIB_FOR, CRIB_READERS], practices: CRIB_PRACTICES },
    skeleton: {
      title: "The resume as built",
      hint: "The sections in the order they print, and how many rules each has.",
      whole: "Whole resume",
      margin: (n) => `Page margins: ${inchesText(n)}.`,
    },
    roleHint: "Apply only to a resume tailored for that target role, in addition to the rules above. Target roles are set in Search Settings.",
    goneNote: "This target role is no longer in Search Settings. Its rules are kept here, and go to no resume. Remove them if you no longer need them.",
    sideLabel: "The resume as built",
  },
  letter: {
    id: "letter",
    globalPart: LETTER_GLOBAL_PART,
    globalField: "letter_global",
    roleField: "letter_role_rules",
    sections: LETTER_SECTIONS,
    parts: LETTER_RULE_PARTS,
    crib: { title: "How a cover letter works", paragraphs: [LETTER_CRIB_FOR, LETTER_CRIB_WHEN], practices: LETTER_CRIB_PRACTICES },
    skeleton: {
      title: "The cover letter as built",
      hint: "The parts in the order they read, and how many rules each has.",
      whole: "Whole letter",
      margin: (n) => `Page margins: ${inchesText(n)}, set under Global on Resume Rules.`,
    },
    roleHint: "Apply only to a letter written for that target role, in addition to the rules above. Target roles are set in Search Settings.",
    goneNote: "This target role is no longer in Search Settings. Its rules are kept here, and go to no letter. Remove them if you no longer need them.",
    sideLabel: "The cover letter as built",
  },
};
export const QA_PART: PartDef = {
  id: "qa",
  title: "Settings for QA of documentation",
  keys: ["review_steps", "drift_patterns", "review_flags", "banned", "humanizing", "style_off", "credentials", "legacy_patterns"],
};
/** The writing rules are shown on three pages, each editing and saving only its own sections of the
 * one writing document: Resume Rules and Cover Letter Rules (the whole document, each of its sections and each target role),
 * and Document Creation Rules (voice, ATS, interviews and QA). */
export const WRITING_PAGE_PARTS: Record<WritingPage, PartDef[]> = {
  "resume-rules": RESUME_RULE_PARTS,
  "letter-rules": LETTER_RULE_PARTS,
  "document-rules": [VOICE_PART, ATS_PART, INTERVIEW_PART, QA_PART],
};

/** The line under "Settings for QA of documentation": these steer the writer, not the owner's voice. */
export const QA_LINE = "These steer the writer and its reviewers when they draft and check. They are not your voice.";
/** The line under "Never write", the phrases code checks for. */
export const BANNED_LINE =
  "Words and phrases that must never appear in anything written in your name. Case does not matter, and plurals and endings like -ed and -ing are caught too.";
/** The line under "Checked automatically": the checks the app runs on every draft. */
export const CHECKED_LINE = "The app checks every draft for these, for everyone. They are not yours to edit.";
/** The line under "To review": older entries written as patterns. */
export const REVIEW_LINE =
  "These were written as patterns before Never write became plain words. Every one is still checked exactly as before. Each stays here, still checked, until you remove it.";
/** The line under "Style rules": the rules that ship with the app, each one the owner may switch off. */
export const STYLE_LINE = "These start on. Switch one off and the packet check stops looking for it.";
/** The line under "Certifications and licences": the owner's own names, beside the ones the app knows. */
export const CREDENTIALS_LINE =
  "The app already knows common ones. Add the ones your field uses. A packet that names one your master doesn't hold is refused.";

export const BLANK_BANNED = () => ({ phrase: "", why: "" });
export const BLANK_DRIFT = () => ({ name: "", looks_like: "", fix: "" });
export const BLANK_CREDENTIAL = () => ({ name: "" });

/** The ids of the style rules that are off once the rule named (`id`) is switched on or off. They are listed in the app's order
 * (`rules`), then any id the app does not know (a rule from another version of it), kept as it was. A switch put back leaves exactly
 * what was saved, so there is nothing to save. */
export function withStyleSwitch(off: string[], rules: { id: string }[], id: string, on: boolean): string[] {
  const ids = rules.map((r) => r.id);
  const now = new Set(off);
  if (on) now.delete(id);
  else now.add(id);
  return [...ids.filter((r) => now.has(r)), ...[...now].filter((r) => !ids.includes(r))];
}

export function pruneWriting(w: Writing): Writing {
  const says = (r: { says: string }) => r.says;
  const rules = (list: { id: string; says: string }[]) => settle(list.filter((r) => filled(r.says)), says);
  const forRoles = (byRole: Record<string, { id: string; says: string }[]>) =>
    Object.fromEntries(
      Object.entries(byRole)
        .map(([id, list]): [string, { id: string; says: string }[]] => [id, rules(list)])
        .filter(([, list]) => list.length > 0),
    );
  return {
    ...w,
    voice_rules: rules(w.voice_rules),
    ats_rules: rules(w.ats_rules),
    interview_rules: rules(w.interview_rules),
    resume_global: rules(w.resume_global),
    resume_header: rules(w.resume_header),
    resume_summary: rules(w.resume_summary),
    resume_competencies: rules(w.resume_competencies),
    resume_achievements: rules(w.resume_achievements),
    resume_experience: rules(w.resume_experience),
    resume_education: rules(w.resume_education),
    // A target role with no rules written has no entry; one whose role is gone keeps its rules.
    role_rules: forRoles(w.role_rules),
    letter_global: rules(w.letter_global),
    letter_header: rules(w.letter_header),
    letter_greeting: rules(w.letter_greeting),
    letter_opening: rules(w.letter_opening),
    letter_body: rules(w.letter_body),
    letter_closing: rules(w.letter_closing),
    letter_signoff: rules(w.letter_signoff),
    letter_role_rules: forRoles(w.letter_role_rules),
    review_steps: rules(w.review_steps),
    drift_patterns: settle(w.drift_patterns.filter((d) => filled(d.name, d.looks_like, d.fix)), (d) => d.name || d.looks_like),
    review_flags: rules(w.review_flags),
    banned: settle(w.banned.filter((b) => filled(b.phrase)), (b) => b.phrase),
    humanizing: rules(w.humanizing),
    credentials: settle(w.credentials.filter((c) => filled(c.name)), (c) => c.name),
  };
}

// --- words for a part's count and its folded line ---------------------------------------------------------------

export const wordCount = (text: string) => (text.trim() === "" ? 0 : text.trim().split(/\s+/).length);

/** What is in a part, for its folded line: its first few lines, in one line (the stylesheet cuts it at the edge). */
export function preview(lines: string[], most = 6): string {
  return lines
    .map((l) => l.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .slice(0, most)
    .join(" · ");
}
