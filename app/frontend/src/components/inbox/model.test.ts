import { describe, expect, it } from "vitest";
import type { Task } from "../../api/types/inbox";
import { agentItems, linkTo, matching, needsItems, ordered, shortAge } from "./model";

function task(id: string, extra: Partial<Task> = {}): Task {
  return {
    id, title: `Task ${id}`, kind: "review_roles", state: "open", detail: null, due: null, for: { kind: "owner", name: "You" },
    options: null, recommended: null, answer: null, note: null, link: null, snoozed_until: null, created_at: "2026-10-20T08:00:00Z",
    done_at: null, ...extra,
  };
}

describe("Notifications' rows", () => {
  it("says an age in the fewest letters", () => {
    const now = new Date("2026-10-24T12:00:00Z");
    expect(shortAge("2026-10-24T11:59:30Z", now)).toBe("now");
    expect(shortAge("2026-10-24T11:35:00Z", now)).toBe("25m");
    expect(shortAge("2026-10-24T10:00:00Z", now)).toBe("2h");
    expect(shortAge("2026-10-21T12:00:00Z", now)).toBe("3d");
  });

  it("lists what asks today first and what was put off after, with its kind and date as the meta line", () => {
    const items = needsItems([task("later", { snoozed_until: "2026-10-30" }), task("now", { due: "2026-10-25" })], "2026-10-24");
    expect(items.map((i) => i.id)).toEqual(["now", "later"]);
    expect(items[0].meta).toEqual(["Review roles", "Due tomorrow, October 25"]);
    expect(items[1].quiet).toBe(true);
    expect(items[1].meta[1]).toMatch(/^Put off until/);
  });

  it("puts the suggested option first", () => {
    expect(ordered([{ id: "a", label: "A" }, { id: "b", label: "B" }], "b").map((o) => o.id)).toEqual(["b", "a"]);
  });

  it("links a role to the role, a search or several roles to the Pipeline, a site to Search Settings, and nothing else", () => {
    expect(linkTo({ kind: "opportunity", id: "r1" })).toEqual({ href: "#/board/r1", words: "See the role" });
    expect(linkTo({ kind: "opportunity", id: "r1" }, "review_roles")?.words).toBe("See the roles");
    expect(linkTo({ kind: "search_run", id: "s1" })?.words).toBe("See the search");
    expect(linkTo({ kind: "site", id: "x" })?.href).toBe("#/profile");
    expect(linkTo({ kind: "employer", id: "e" })).toBeNull();
  });

  it("lists the agents' open work first, then what they finished, and finds by every word", () => {
    const items = agentItems([task("open", { for: { kind: "agent", name: "Scout" } })], [
      task("mine", { state: "done", done_at: "2026-10-24T09:00:00Z" }),
      task("theirs", { state: "done", done_at: "2026-10-24T09:00:00Z", for: { kind: "agent", name: "Writer" } }),
    ]);
    expect(items.map((i) => i.id)).toEqual(["open", "theirs"]);
    expect(items[1].meta).toEqual(["Finished by Writer"]);
    expect(matching(items, "writer task").map((i) => i.id)).toEqual(["theirs"]);
  });
});
