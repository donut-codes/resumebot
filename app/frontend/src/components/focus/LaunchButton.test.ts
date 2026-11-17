import { describe, expect, it } from "vitest";
import type { ApplyGo } from "../../api/types/pipeline";
import { launchWords } from "./LaunchButton";

// A press the page recorded and the engine has not begun reads as a run that goes. Everything is made up.

const run = (phase: ApplyGo["phase"], done = false): ApplyGo => ({
  phase, total: 0, built: 0, opened: [], skipped: [], notes: [], done,
});

describe("launchWords", () => {
  it("says an asked run is being checked", () => {
    expect(launchWords(run("asked"))).toBe("Checking today's roles");
  });
  it("says nothing when no run ever began", () => {
    expect(launchWords(run("idle", true))).toBeNull();
  });
  it("names the role being written and its step with the count", () => {
    const went: ApplyGo = {
      ...run("building"), total: 15, built: 14,
      current: { id: "r1", title: "Widget Planner", company: "Example Co", step: "waiting for the writer" },
    };
    expect(launchWords(went)).toBe("Building packets: 14 of 15 · Widget Planner at Example Co: waiting for the writer");
  });
  it("gives only the count when no role is being written", () => {
    expect(launchWords({ ...run("building"), total: 15, built: 14, current: null })).toBe("Building packets: 14 of 15");
    expect(launchWords({ ...run("building"), total: 15, built: 14 })).toBe("Building packets: 14 of 15");
  });
});
