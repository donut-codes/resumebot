import type { AgentSummary } from "../../api/types/agents";
import { formatTime } from "../../lib/format";
import { lastSeenWords, nowInk, ribbonRole, slotsWords, standing, stuckWords } from "./agentWords";

function agent(overrides: Partial<AgentSummary> = {}): AgentSummary {
  return {
    connection_id: "a1", name: "The scout", description: null, status: "idle", paused: false, current: null,
    roles: ["scout"], roles_words: "Scout", availability: "Free", session_state: null, checked_in_at: null, last_seen_at: null,
    declared_at: null, model_words: null, capacity: { free: true, busy: 0, slots: 1, words: "Free", since: null }, runners: [],
    ...overrides,
  };
}

const NOW = (role: string) => ({ task_title: "A task", project_title: "Job search", started_at: "2026-10-01T12:00:00Z", role });

describe("ribbonRole: the robot whose ribbon an agent wears", () => {
  it("is the role it is working now, else the first it holds in the order the work goes", () => {
    const crew = ["writer", "scout", "analyst"];
    expect(ribbonRole(agent({ roles: crew }))).toBe("scout");
    expect(ribbonRole(agent({ roles: crew, status: "working", current: NOW("analyst") }))).toBe("analyst");
    expect(ribbonRole(agent({ roles: crew, status: "working", current: NOW("writer") }))).toBe("writer");
  });

  it("falls back to the first role when it is paused, or its work is for a role it does not hold", () => {
    expect(ribbonRole(agent({ roles: ["scout", "writer"], status: "working", paused: true, current: NOW("writer") }))).toBe("scout");
    expect(ribbonRole(agent({ roles: ["scout"], status: "working", current: NOW("writer") }))).toBe("scout");
  });

  it("is nothing for an agent that is none of the three", () => {
    expect(ribbonRole(agent({ roles: [] }))).toBeUndefined();
    expect(ribbonRole(agent({ roles: ["reviewer"] }))).toBeUndefined();
  });
});

describe("standing: where an agent stands, in a word and an ink", () => {
  it("is green on a task, saffron paused, oxblood when it has gone quiet or never checked in, plain when asleep", () => {
    expect(standing(agent({ status: "working", availability: "On a task" }))).toEqual({ word: "On a task", ink: "green" });
    expect(standing(agent({ status: "working", paused: true }))).toEqual({ word: "Paused", ink: "saffron" });
    expect(standing(agent({ session_state: "not_responding" }))).toEqual({ word: "Not responding", ink: "oxblood" });
    expect(standing(agent({ status: "offline", availability: "Offline" }))).toEqual({ word: "Offline", ink: "oxblood" });
    expect(standing(agent())).toEqual({ word: "Free" });
  });
});

describe("nowInk: the band over what an agent is doing now", () => {
  it("is green working, saffron paused, oxblood stuck, graphite when it simply waits", () => {
    expect(nowInk(agent({ status: "working" }))).toBe("green");
    expect(nowInk(agent({ paused: true }))).toBe("saffron");
    expect(nowInk(agent({ status: "working", current: { ...NOW("scout"), stuck_since: "2026-10-01T12:30:00Z" } }))).toBe("oxblood");
    expect(nowInk(agent())).toBe("graphite");
  });
});

describe("the roster's columns", () => {
  it("counts the busy slots", () => {
    const capacity = { free: false, busy: 1, slots: 2, words: "Busy: 1 of 2 slots", since: null };
    expect(slotsWords(agent({ capacity }))).toBe("1 of 2");
    expect(slotsWords(agent({ capacity: { ...capacity, slots: null } }))).toBe("1");
  });

  it("counts the runners at work out of the runners that are there", () => {
    const capacity = { free: true, busy: 3, slots: 5, words: "3 of 5 at work", since: null };
    expect(slotsWords(agent({ capacity }))).toBe("3 of 5");
  });

  it("says when it was last seen, or Never", () => {
    expect(lastSeenWords(agent({ last_seen_at: null }))).toBe("Never");
    expect(lastSeenWords(agent({ last_seen_at: new Date(Date.now() - 3 * 3_600_000).toISOString() }))).toBe("3 hours ago");
  });
});

describe("stuckWords: since when, on the owner's clock", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("gives the time of day for a moment that is today for the owner, and the owner's day for an earlier one", () => {
    // One moment for the test and for the page's own read of today: 1:30 PM on Oct 4 for an owner at UTC+14, far from
    // their midnight. With the real clock, a midnight between the two reads made "now" a day that was no longer today.
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-03T23:30:00Z"));
    const now = new Date();
    expect(stuckWords(now.toISOString())).toBe(`Stuck since ${formatTime(now)}`);
    // 16:56 UTC on Oct 1 is Oct 2 at 06:56 for an owner at UTC+14: the day, not a time of day.
    expect(stuckWords("2026-10-01T16:56:00Z")).toMatch(/^Stuck since October 2(, 2026)?$/);
  });
});
