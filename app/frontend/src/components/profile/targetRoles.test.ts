import type { TargetRole } from "../../api/types/profile";
import { blankRole, MAX_TITLES, mergeParsed, newRoleId, normalizeRole, plainRefusal, ROLE_PARTS, titlesTyped } from "./targetRoles";

const ROLE: TargetRole = {
  ...blankRole("lead", "Lead"),
  titles: ["Team Lead"],
  description: "Leads a team.",
};

describe("target roles", () => {
  it("a new role starts empty, and its parts come in the order a posting reads", () => {
    expect(blankRole("x", "X")).toEqual({
      id: "x", name: "X", titles: [], description: "", responsibilities: "", qualifications: "", preferred: "", compensation: "",
    });
    expect(ROLE_PARTS.map((p) => p.title)).toEqual([
      "Description", "Responsibilities", "Qualifications", "Preferred", "Benefits / Compensation",
    ]);
  });

  it("a new id is a slug of the name, made unique with -2, -3", () => {
    expect(newRoleId("Project Lead!", [])).toBe("project-lead");
    expect(newRoleId("Project Lead", [{ id: "project-lead" }])).toBe("project-lead-2");
    expect(newRoleId("Project Lead", [{ id: "project-lead" }, { id: "project-lead-2" }])).toBe("project-lead-3");
  });

  it("fills in what a stored role leaves out, and keeps anything else stored on it", () => {
    const stored = { id: "a", name: "A", extra: "kept" } as unknown as TargetRole;
    expect(normalizeRole(stored)).toMatchObject({ id: "a", name: "A", titles: [], description: "", compensation: "", extra: "kept" });
  });

  it("a posting fills the parts it spoke of, adds its titles once, and names the role only when it has no name", () => {
    const fields = { name: "Other", titles: ["team lead", " Manager ", ""], description: "New.", preferred: "  " };
    expect(mergeParsed(ROLE, fields)).toEqual({ ...ROLE, titles: ["Team Lead", "Manager"], description: "New." });
    expect(mergeParsed({ ...ROLE, name: " " }, fields).name).toBe("Other");
    expect(mergeParsed(ROLE, {})).toEqual(ROLE);
  });

  it("keeps what a posting says whole: a part has no limit on its length", () => {
    expect(mergeParsed(ROLE, { description: "x".repeat(1500) }).description).toHaveLength(1500);
  });

  it("the parts fall in two groups: Description is the core pair with the example titles, the rest are more detail", () => {
    expect(ROLE_PARTS.map((p) => [p.title, p.group])).toEqual([
      ["Description", "core"],
      ["Responsibilities", "more"],
      ["Qualifications", "more"],
      ["Preferred", "more"],
      ["Benefits / Compensation", "more"],
    ]);
  });
});

describe("the counts a role and the list hold", () => {
  it("says a refused count of titles in one plain sentence", () => {
    const raw = "the criteria don't fit: target_roles.0.titles: Value error, a target role holds at most 10 example titles; it has 11";
    expect(plainRefusal(raw)).toBe("A role holds up to 10 example titles, and this one has 11. Remove some, then save again.");
  });

  it("says a refused count of roles in one plain sentence, and leaves any other message as it was", () => {
    expect(plainRefusal("the criteria don't fit: target_roles: Value error, target_roles holds at most 20 roles; it has 21")).toBe(
      "You can have up to 20 target roles, and this makes 21. Delete one, then add again.",
    );
    expect(plainRefusal("Couldn't save that.")).toBe("Couldn't save that.");
    expect(plainRefusal(null)).toBeNull();
  });

  it("adds no title from a posting past the most a role holds", () => {
    const have = Array.from({ length: MAX_TITLES - 1 }, (_, i) => `Title ${i}`);
    expect(mergeParsed({ ...ROLE, titles: have }, { titles: ["Extra One", "Extra Two"] }).titles).toEqual([...have, "Extra One"]);
  });

  it("splits typed titles at semicolons, trimmed and without blanks", () => {
    expect(titlesTyped(" A ; ;B;")).toEqual(["A", "B"]);
  });
});
