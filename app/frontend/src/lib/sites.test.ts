import { describe, expect, it } from "vitest";
import type { SiteFound, SiteReadBy } from "../api/types/where";
import { effectiveRead } from "./sites";

describe("effectiveRead", () => {
  const cases: [SiteReadBy, SiteFound | null, SiteFound | null][] = [
    ["app", null, "app"],
    ["browser", null, "browser"],
    // what the owner chose wins over anything left from a time the site was left to resumebot
    ["app", "browser", "app"],
    ["browser", "app", "browser"],
    // left to resumebot: what was found, and nobody until it is known
    ["auto", "app", "app"],
    ["auto", "browser", "browser"],
    ["auto", null, null],
  ];
  it.each(cases)("a site read by %s, found %s, is read by %s", (read_by, read_found, expected) => {
    expect(effectiveRead({ read_by, read_found })).toBe(expected);
  });
});
