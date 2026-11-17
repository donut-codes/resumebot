// The product knows no host of ours: a LAN address or a path under the root user's home in the UI source would only
// ever be right for one install. (Places, employers and pay figures from one person's search are checked for the whole
// repository by scripts/check_no_one.py, with the words kept in a file outside it; this suite holds no list of them.)
const files = import.meta.glob("./**/*.{ts,tsx,css}", { query: "?raw", import: "default", eager: true }) as Record<
  string,
  string
>;

const LAN = /\b192\.168\.\d{1,3}\.\d{1,3}\b/;
const ROOT = /(^|[^\w.])\/root\//;

describe("no hard-coded hosts in the UI", () => {
  it("scans the source", () => {
    expect(Object.keys(files).length).toBeGreaterThan(20);
  });

  it("holds no LAN address and no path under the root user's home", () => {
    const hits: string[] = [];
    for (const [path, text] of Object.entries(files)) {
      text.split("\n").forEach((line, i) => {
        if (LAN.test(line) || ROOT.test(line)) hits.push(`${path}:${i + 1}: ${line.trim().slice(0, 100)}`);
      });
    }
    expect(hits).toEqual([]);
  });
});
