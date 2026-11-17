import { describe, expect, it } from "vitest";
import { safeHref } from "./links";

describe("safeHref", () => {
  it("keeps an http or https address", () => {
    expect(safeHref("https://jobs.example.test/1")).toBe("https://jobs.example.test/1");
    expect(safeHref("  http://jobs.example.test/a?b=1#c ")).toBe("http://jobs.example.test/a?b=1#c");
    expect(safeHref("HTTPS://Jobs.Example.test/")).toBe("HTTPS://Jobs.Example.test/");
  });

  it.each([
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    " javascript:alert(1)",
    "java\nscript:alert(1)",
    "data:text/html,x",
    "file:///etc/passwd",
    "mailto:a@example.test",
    "//jobs.example.test/1",
    "/careers/1",
    "jobs.example.test",
    "https://",
    "",
    "   ",
  ])("draws no link for %j", (value) => {
    expect(safeHref(value)).toBeUndefined();
  });

  it("draws no link for nothing", () => {
    expect(safeHref(null)).toBeUndefined();
    expect(safeHref(undefined)).toBeUndefined();
  });
});
