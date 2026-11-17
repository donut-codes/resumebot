import { describe, expect, it } from "vitest";
import { makeCard } from "../../state/rowFixtures";
import { buildingProgress, countsFirstHours, fromCard, RAIL, railLabel, searchCounts } from "./model";

// What a row shows is the server's note, when it has one (docs/PRD.md 24.18); the screen adds only how it is coloured, the
// bar of a packet being written and the New tag with its bonus. Everything is made up.

const STAGES = [
  "followup_needed", "landing_zone", "cleansed_data", "first_impression", "second_impression", "ready_to_launch",
  "applied", "active_discussion", "rejected", "gone_quiet", "withdrew", "gated_out",
];

describe("fromCard", () => {
  it("prints the card's own note for every stage, Ready to Launch and Closed included", () => {
    for (const status of STAGES) {
      const card = makeCard({ status, next: `What ${status} says`, ended_words: "Not this one", reason: "Nor this" });
      expect(fromCard(card, false).next, status).toBe(`What ${status} says`);
    }
    expect(fromCard(makeCard({ status: "added_links", origin: "posting", next: null }), false).next).toBeNull();
  });

  it("colours a Ready to Launch sentence by what the owner can do about it and no other", () => {
    const ready = (extra = {}) => fromCard(makeCard({ status: "ready_to_launch", next: "x", ...extra }), false).nextTone;
    const packet = (status: "writing" | "ready" | "held" | "failed" | "replaced") => ({ packet: { id: "p", version: 1, status } });
    expect(ready()).toBe("yours");
    expect(ready(packet("writing"))).toBe("yours");
    expect(ready(packet("ready"))).toBe("yours");
    expect(ready(packet("replaced"))).toBe("yours");
    expect(ready(packet("held"))).toBe("plain");
    expect(ready(packet("failed"))).toBe("closed");
    expect(ready({ opened_at: "2026-10-21T09:10:00+00:00", ...packet("failed") })).toBe("plain");
    expect(ready({ opened_at: "2026-10-21T09:10:00+00:00" })).toBe("plain");
    for (const status of ["second_impression", "applied", "rejected", "gated_out"]) {
      expect(fromCard(makeCard({ status, next: "x" }), false).nextTone, status).toBe("plain");
    }
  });

  it("shows the building bar only while a Ready to Launch role's packet is being written and it is not opened", () => {
    const progress = { step: "checking" as const, label: "Its facts being checked", done: 1, of: 3, fraction: 0.33 };
    const writing = makeCard({ status: "ready_to_launch", packet: { id: "p", version: 1, status: "writing", progress } });
    expect(buildingProgress(writing)).toEqual(progress);
    expect(fromCard(writing, false).progress).toEqual(progress);
    expect(buildingProgress({ ...writing, opened_at: "2026-10-21T09:10:00+00:00" })).toBeNull();
    expect(buildingProgress({ ...writing, status: "applied" })).toBeNull();
    expect(buildingProgress({ ...writing, packet: { id: "p", version: 1, status: "ready", progress } })).toBeNull();
    expect(buildingProgress(makeCard({ status: "ready_to_launch" }))).toBeNull();
  });

  it("opens a role, and not a posting that is no role yet", () => {
    expect(fromCard(makeCard({ origin: "scout" }), false).openable).toBe(true);
    expect(fromCard(makeCard({ origin: "posting", status: "added_links" }), false).openable).toBe(false);
  });
});

describe("the number on a row and its New tag", () => {
  const row = (extra: Parameters<typeof makeCard>[0]) => fromCard(makeCard({ status: "first_impression", ...extra }), false);

  it("is the card's own number, and the New tag says in its number how many points the first hours are", () => {
    // Old: a bare "New", with "Its first hours add 20" in the sentence under it. New (24.18): "New (+20)".
    expect(row({ new: true, score: 90, first_hours_bonus: 20 })).toMatchObject({ fit: 90, newTag: "New (+20)" });
    expect(row({ new: true, score: 115, first_hours_bonus: 35 })).toMatchObject({ fit: 115, newTag: "New (+35)" });
  });

  it("is a bare New when the hours add nothing (no number, a rank of 0, or the owner turned the bonus off), and no tag when not new", () => {
    expect(row({ new: true, score: 0, first_hours_bonus: 0 })).toMatchObject({ fit: 0, newTag: "New" });
    expect(row({ new: true, score: null, fit_pct: 55, first_hours_bonus: 0 })).toMatchObject({ fit: 55, newTag: "New" });
    expect(row({ new: true, score: 70, first_hours_bonus: undefined })).toMatchObject({ newTag: "New" });
    expect(row({ new: false, score: 90, first_hours_bonus: 0 })).toMatchObject({ fit: 90, newTag: null });
  });

  it("counts the first hours in the number by the bonus the server says, not by the tag", () => {
    expect(countsFirstHours(makeCard({ new: true, score: 20, first_hours_bonus: 20 }))).toBe(true);
    expect(countsFirstHours(makeCard({ new: true, score: 20, first_hours_bonus: 0 }))).toBe(false);
    expect(countsFirstHours(makeCard({ new: true, score: null, first_hours_bonus: 0 }))).toBe(false);
  });
});

describe("the pay on a row", () => {
  const pay = (words: string) => fromCard(makeCard({ pay: words }), false).pay;

  it("is the pay when there is one, and nothing when the posting gives none", () => {
    // Old: "Not posted" printed on a row that had no pay. New (24.18): an absent fact is no label.
    expect(pay("$90K–$120K")).toBe("$90K–$120K");
    expect(pay("Not posted")).toBeNull();
    expect(pay("Not stated")).toBeNull();
    expect(pay("")).toBeNull();
  });
});

describe("the check made when a role came into Ready to Launch, on a row", () => {
  const ready = (next: string | null, extra: Parameters<typeof makeCard>[0] = {}) => fromCard(makeCard({ status: "ready_to_launch", next, ...extra }), false);

  // The server says of the check only what is not the normal run (24.16, 24.18): a posting the page did not confirm, and
  // one the browser could not check. Found open, and the browser looking, are not said.
  it.each([
    ["Posting not confirmed when checked today", null, "Posting not confirmed when checked today"],
    ["Posting not confirmed when checked Nov 5", null, "Posting not confirmed when checked Nov 5"],
    ["The browser could not check the posting", null, "The browser could not check the posting"],
    ["The packet failed · Posting not confirmed when checked today", "The packet failed", "Posting not confirmed when checked today"],
    ["The packet is held · The browser could not check the posting", "The packet is held", "The browser could not check the posting"],
    ["The packet failed · Pay mostly below your floor: $70,000–$150,000 · Posting not confirmed when checked today", "The packet failed · Pay mostly below your floor: $70,000–$150,000", "Posting not confirmed when checked today"],
  ])("sets the result apart from the rest of the note: %s", (next, lead, check) => {
    const row = ready(next);
    expect(row.next).toBe(lead);
    expect(row.check).toBe(check);
  });

  it("leaves a note with no check as the server wrote it, and a role Launch opened", () => {
    const plain = "The packet failed · Pay mostly below your floor: $70,000–$150,000";
    expect(ready(plain)).toMatchObject({ next: plain, check: null });
    expect(ready("Opened. Mark Applied when sent", { opened_at: "2026-10-21T09:10:00+00:00" })).toMatchObject({ next: "Opened. Mark Applied when sent", check: null });
    expect(ready(null)).toMatchObject({ next: null, check: null });
  });

  it("is a Ready to Launch role's alone: no other stage's sentence is taken apart", () => {
    const said = "Analyst didn't finish: send it on yourself · Posting not confirmed when checked today";
    expect(fromCard(makeCard({ status: "second_impression", next: said }), false)).toMatchObject({ next: said, check: null });
  });

  it("keeps the sentence's colour: the check changes what is shown, not whether the owner has something to do", () => {
    const said = "Launch in Browser skips it: no login · Posting not confirmed when checked today";
    expect(ready(said).nextTone).toBe("yours");
    expect(ready(said, { packet: { id: "p", version: 1, status: "failed" } }).nextTone).toBe("closed");
  });
});

describe("the rail", () => {
  it("draws the ranked list and work in progress apart, each its own entry with its stages nested under it", () => {
    expect(RAIL.map((g) => g.entries.map((e) => e.key))).toEqual([
      ["favorites", "ready_to_launch", "applied", "active_discussion"],
      ["ranked", "first_impression", "second_impression"],
      ["work", "added_links", "followup_needed", "landing_zone", "cleansed_data"],
      ["closed"],
    ]);
    expect(RAIL.map((g) => g.label)).toEqual([null, null, null, null]);
    expect(railLabel("ranked")).toBe("Ranked list");
    expect(railLabel("work")).toBe("Work in progress");
    for (const group of [RAIL[1], RAIL[2]]) {
      expect(group.entries[0].nested).toBeFalsy();
      expect(group.entries.slice(1).every((e) => e.nested)).toBe(true);
    }
    expect(RAIL[0].entries.some((e) => e.nested)).toBe(false);
  });

  it("keeps Ready to Launch where the owner acts from, with the count marked", () => {
    expect(RAIL[0].entries.find((e) => e.key === "ready_to_launch")?.yours).toBe(true);
  });
});

describe("searchCounts", () => {
  it("counts a search's matches in each group as well as in each stage", () => {
    expect(searchCounts({ first_impression: 2, ready_to_launch: 1, followup_needed: 3, applied: 1, rejected: 2 }, 0)).toEqual({
      first_impression: 2, ready_to_launch: 1, followup_needed: 3, applied: 1, closed: 2, ranked: 3, work: 3,
    });
    expect(searchCounts({ applied: 1 }, 2)).toEqual({ applied: 1, favorites: 2 });
  });
});
