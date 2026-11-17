import {
  byNewest,
  dateInWords,
  dateWithWeekday,
  dateTimeInWords,
  dueInWords,
  fromOwnerLocalValue,
  getOwnerZone,
  isBefore,
  localDay,
  msUntilOwnerMidnight,
  ownerToday,
  ownerZoneLabel,
  setOwnerZone,
  shortDateInWords,
  snoozeTomorrow,
  timeAgoInWords,
  timeInWords,
  toOwnerLocalValue,
  wasDueInWords,
} from "./dates";

describe("dueInWords", () => {
  it("says today for a same-day due date", () => {
    expect(dueInWords("2026-09-24", "2026-09-24")).toBe("today, September 24");
  });

  it("says tomorrow", () => {
    expect(dueInWords("2026-09-25", "2026-09-24")).toBe("tomorrow, September 25");
  });

  it("says yesterday", () => {
    expect(dueInWords("2026-09-23", "2026-09-24")).toBe("yesterday, September 23");
  });

  it("says 'in N days' inside two weeks", () => {
    expect(dueInWords("2026-10-06", "2026-09-24")).toBe("in 12 days, October 6");
  });

  it("falls back to an absolute date beyond two weeks", () => {
    expect(dueInWords("2026-11-10", "2026-09-24")).toBe("November 10");
  });
});

describe("timeAgoInWords", () => {
  // A fixed "now" (a "Last activity" line once read 13 hours ago for an activity
  // minutes old) -- a timezone-aware ISO instant compares
  // correctly against `now` regardless of offset, since both resolve to
  // the same absolute instant.
  const now = new Date("2026-09-30T18:30:00.000Z");

  it("says 'just now' under a minute", () => {
    expect(timeAgoInWords("2026-09-30T18:29:45.000Z", now)).toBe("just now");
  });

  it("says minutes ago for a tz-offset instant a few minutes back", () => {
    // 2026-09-30T18:26:00+00:00 == 2026-09-30T14:26:00-04:00 (Eastern) --
    // either spelling of the same instant must read the same relative age.
    expect(timeAgoInWords("2026-09-30T18:26:00+00:00", now)).toBe("4 minutes ago");
    expect(timeAgoInWords("2026-09-30T14:26:00-04:00", now)).toBe("4 minutes ago");
  });

  it("says hours ago, not a timezone-shifted count", () => {
    expect(timeAgoInWords("2026-09-30T16:30:00.000Z", now)).toBe("2 hours ago");
  });

  it("says days ago inside the two-week horizon", () => {
    expect(timeAgoInWords("2026-09-27T18:30:00.000Z", now)).toBe("3 days ago");
  });

  it("falls back to an absolute date beyond 14 days", () => {
    // dateInWords' own fallback formatting (year included or not) depends
    // on the real wall-clock date, not the fixed `now` above -- assert
    // only the part this function controls: it's no longer relative.
    expect(timeAgoInWords("2026-09-10T18:30:00.000Z", now)).toMatch(/^September 11/); // 18:30 UTC is 08:30 the next day on the owner's clock
  });
});

describe("shortDateInWords", () => {
  it("is the short month and the day, without the year in the current year", () => {
    expect(shortDateInWords("2026-09-10T09:12:00", "2026-09-24")).toBe("Sep 10");
  });

  it("includes the year for a different year", () => {
    expect(shortDateInWords("2024-09-10T09:12:00", "2026-09-24")).toBe("Sep 10, 2024");
  });

  it("reads a date-only value as that calendar day", () => {
    expect(shortDateInWords("2026-10-07", "2026-10-09")).toBe("Oct 7");
  });
});

describe("dateInWords", () => {
  it("omits the year for the current year", () => {
    expect(dateInWords("2026-09-10T09:12:00", "2026-09-24")).toBe("September 10");
  });

  it("includes the year for a different year", () => {
    expect(dateInWords("2024-09-10T09:12:00", "2026-09-24")).toBe("September 10, 2024");
  });

  describe("a date-only value, in a non-UTC zone", () => {
    // Scoped to this block (not the whole suite/vite.config) so it doesn't
    // shift full-datetime fixtures elsewhere (e.g. Board.test.tsx's
    // waiting_since/chase_at) that assume the runner's default TZ.
    beforeAll(() => {
      // Any zone behind UTC (UTC-4/5) shows the bug this guards against.
      vi.stubEnv("TZ", "America/New_York");
    });
    afterAll(() => {
      vi.unstubAllEnvs();
    });

    it("parses it as a local calendar date, not UTC midnight", () => {
      // Regression for a bug where `new Date("2026-09-29")` (parsed as UTC
      // midnight) rendered as "September 28" in any zone west of UTC,
      // e.g. snoozed_until/paused_until sent as bare YYYY-MM-DD strings.
      expect(dateInWords("2026-09-29", "2026-09-24")).toBe("September 29");
    });
  });
});

describe("snooze presets", () => {
  // 2026-09-25 is a Friday.
  it("tomorrow is the next calendar day", () => {
    expect(snoozeTomorrow("2026-09-25")).toBe("2026-09-26");
  });
});

describe("dateWithWeekday", () => {
  it("prefixes the abbreviated weekday", () => {
    // 2026-09-26 is a Saturday.
    expect(dateWithWeekday("2026-09-26")).toBe("Sat, September 26");
  });
});

describe("wasDueInWords", () => {
  it("says was due yesterday", () => {
    expect(wasDueInWords("2026-09-24", "2026-09-25")).toBe("Was due yesterday");
  });

  it("says was due today", () => {
    expect(wasDueInWords("2026-09-25", "2026-09-25")).toBe("Was due today");
  });

  it("falls back to the absolute date", () => {
    expect(wasDueInWords("2026-09-01", "2026-09-25")).toBe("Was due September 1");
  });
});

// The browser runs in Pacific/Pago_Pago (UTC-11, vite.config.ts); test-setup.ts gives the owner Pacific/Kiritimati (UTC+14).
// A time written in the browser's zone is 25 hours off the owner's, so it cannot pass by luck.
describe("timestamps render in the owner's zone, not the browser's", () => {
  it("runs with a browser zone that is not the owner's", () => {
    expect(new Date("2026-10-01T16:56:00Z").getTimezoneOffset()).toBe(660);
    expect(getOwnerZone()).toBe("Pacific/Kiritimati");
  });

  it("shows a UTC instant on the owner's clock, on the owner's day", () => {
    for (const iso of ["2026-10-01T16:56:00+00:00", "2026-10-01T16:56:00Z", "2026-10-01T12:56:00-04:00"]) {
      expect(dateTimeInWords(iso, "2026-10-02")).toBe("October 2, 6:56 am");
    }
  });

  it("reads an offset-less timestamp as UTC, not as local time", () => {
    expect(dateTimeInWords("2026-10-01T16:56:00", "2026-10-02")).toBe("October 2, 6:56 am");
  });

  it("names the owner's day for a day column and for grouping", () => {
    expect(shortDateInWords("2026-10-01T16:56:00Z", "2026-10-02")).toBe("Oct 2");
    expect(localDay("2026-10-01T16:56:00Z")).toBe("2026-10-02");
    expect(localDay("2026-10-01")).toBe("2026-10-01");
  });

  it("tells today by the owner's clock", () => {
    expect(ownerToday(new Date("2026-10-01T09:59:00Z"))).toBe("2026-10-01");
    expect(ownerToday(new Date("2026-10-01T10:00:00Z"))).toBe("2026-10-02");
  });

  it("follows a zone it is told, and ignores one it does not know", () => {
    setOwnerZone("Pacific/Pago_Pago");
    expect(timeInWords("2026-10-01T16:56:00Z")).toBe("5:56 am");
    setOwnerZone("Not/AZone");
    expect(getOwnerZone()).toBeUndefined();
  });
});

describe("a time typed in a box is the owner's wall-clock time", () => {
  it("round-trips an instant through the box", () => {
    expect(toOwnerLocalValue("2026-10-01T16:56:00Z")).toBe("2026-10-02T06:56");
    expect(fromOwnerLocalValue("2026-10-02T06:56")).toBe("2026-10-01T16:56:00.000Z");
  });

  it("is correct across a clock change in the owner's zone", () => {
    // Pacific/Auckland puts its clocks forward on 27 September 2026: +12 before, +13 after.
    setOwnerZone("Pacific/Auckland");
    expect(fromOwnerLocalValue("2026-09-26T09:00")).toBe("2026-09-25T21:00:00.000Z");
    expect(fromOwnerLocalValue("2026-09-28T09:00")).toBe("2026-09-27T20:00:00.000Z");
    expect(toOwnerLocalValue("2026-09-27T20:00:00Z")).toBe("2026-09-28T09:00");
  });

  it("refuses text that is not a date and time", () => {
    expect(fromOwnerLocalValue("")).toBeNull();
    expect(fromOwnerLocalValue("soon")).toBeNull();
  });

  it("labels the zone it reads the box in", () => {
    expect(ownerZoneLabel(new Date("2026-10-01T00:00:00Z"))).toMatch(/14/);
  });
});

describe("the owner's midnight", () => {
  it("counts the time left to the owner's midnight, not the browser's", () => {
    // 10:30 UTC is 00:30 on the owner's clock: 23.5 hours to go.
    expect(msUntilOwnerMidnight(new Date("2026-10-01T10:30:00Z"))).toBe(23.5 * 3_600_000);
    expect(msUntilOwnerMidnight(new Date("2026-10-01T09:59:30Z"))).toBe(30_000);
  });
});

describe("timestamps are compared as instants, not as text", () => {
  // As text "2026-10-01T23:00:00-05:00" sorts before "2026-10-02T01:00:00+00:00"; as instants it is three hours after it.
  const west = "2026-10-01T23:00:00-05:00";
  const utc = "2026-10-02T01:00:00+00:00";

  it("orders newest first across offsets", () => {
    expect([utc, west].sort(byNewest)).toEqual([west, utc]);
    expect(byNewest(null, utc)).toBe(1);
    expect(byNewest(west, west)).toBe(0);
  });

  it("says which of two is earlier", () => {
    expect(isBefore(utc, west)).toBe(true);
    expect(isBefore(west, utc)).toBe(false);
  });
});
