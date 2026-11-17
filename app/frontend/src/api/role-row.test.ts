// The page's `BoardCard` is the server's `role_row.Row`. The server checks its own row against `role-row.json`
// (tests/jobsearch/test_role_row.py), and this checks the page's type against the same file, so a key added on one side
// only fails a test on that side. The type's keys are written out below as a record, which the compiler holds to the
// type in both directions (a key missing or one too many does not compile), and the run-time check holds to the file.
import { describe, expect, it } from "vitest";
import rowOnFile from "./role-row.json";
import type { BoardCard, PacketProgress } from "./types/pipeline";

const KEYS: Record<keyof BoardCard, true> = {
  id: true, title: true, company: true, status: true, status_label: true, fit_pct: true, score: true, next: true,
  packet: true, opened_at: true, new: true, first_hours_bonus: true, favorite: true, favorited_at: true, work: true, pay: true,
  ask: true, age: true, ended_reason: true, ended_words: true, reason: true, url: true, apply_url: true, origin: true,
  employer_id: true,
};
const WORK: Record<keyof BoardCard["work"], true> = { mode: true, place: true, words: true };
const AGE: Record<keyof BoardCard["age"], true> = { days: true, words: true, from: true };
const PACKET: Record<keyof NonNullable<BoardCard["packet"]>, true> = { id: true, version: true, status: true, progress: true };
const PROGRESS: Record<keyof PacketProgress, true> = { step: true, label: true, done: true, of: true, fraction: true };

const sorted = (o: object) => Object.keys(o).sort();

describe("the row on file", () => {
  it("has the keys of BoardCard, and of its nested parts, and no others", () => {
    expect(sorted(rowOnFile)).toEqual(sorted(KEYS));
    expect(sorted(rowOnFile.work)).toEqual(sorted(WORK));
    expect(sorted(rowOnFile.age)).toEqual(sorted(AGE));
    expect(sorted(rowOnFile.packet)).toEqual(sorted(PACKET));
    expect(sorted(rowOnFile.packet.progress)).toEqual(sorted(PROGRESS));
  });
});
