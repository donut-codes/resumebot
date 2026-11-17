import { formatCount, formatDollars, formatTime } from "./format";

describe("formatCount", () => {
  it("puts thousands separators in the big ones and leaves the small ones alone", () => {
    expect(formatCount(18881)).toBe("18,881");
    expect(formatCount(143)).toBe("143");
    expect(formatCount(0)).toBe("0");
  });
});

describe("formatDollars", () => {
  it("writes whole dollars with separators", () => {
    expect(formatDollars(141000)).toBe("$141,000");
    expect(formatDollars(141000.4)).toBe("$141,000");
  });
});

describe("the time", () => {
  it("is written the same way whatever the browser's language", () => {
    // 19:40 UTC is 09:40 the next morning for the owner (UTC+14), whatever zone the browser is in.
    expect(formatTime(new Date(Date.UTC(2026, 9, 6, 19, 40)))).toBe("9:40 AM");
  });
});
