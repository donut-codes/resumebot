import { describe, expect, it } from "vitest";
import { termsFor, termsForHosts } from "./termsNote";

// The matching rule is the server's `avoid.covers` (a host is an entry's own or one of its subdomains, on a label
// boundary); the same case list is pinned in backend/tests/sites/test_site_terms.py. Every host is made up.
const CASES: [address: string, listed: string, covered: boolean][] = [
  ["example.test", "example.test", true],
  ["https://www.example.test/jobs?x=1", "example.test", true],
  ["jobs.example.test", "example.test", true],
  ["Example.TEST.", "example.test", true],
  ["https://jobs.example.test:8443/a", "example.test", true],
  ["notexample.test", "example.test", false],
  ["example.test.other.test", "example.test", false],
  ["example.test@other.test", "example.test", false],
  ["other.test", "jobs.example.test", false],
  ["", "example.test", false],
];

describe("termsFor", () => {
  it.each(CASES)("%s against %s covers: %s", (address, listed, covered) => {
    const entry = { hosts: [listed], terms_url: "https://example.test/terms" };
    expect(termsFor(address, [entry])).toBe(covered ? entry : null);
  });

  it("the longest host wins", () => {
    const wide = { hosts: ["example.test"], terms_url: "https://example.test/wide" };
    const narrow = { hosts: ["jobs.example.test"], terms_url: "https://example.test/narrow" };
    expect(termsFor("a.jobs.example.test", [wide, narrow])).toBe(narrow);
    expect(termsFor("other.example.test", [wide, narrow])).toBe(wide);
  });

  it("a job system matches on any of its domains, and no list means no entry", () => {
    const entry = { hosts: ["b.example.test"], terms_url: "https://b.example.test/terms" };
    expect(termsForHosts(["a.example.test", "x.b.example.test"], [entry])).toBe(entry);
    expect(termsForHosts(["a.example.test"], undefined)).toBeNull();
  });
});
