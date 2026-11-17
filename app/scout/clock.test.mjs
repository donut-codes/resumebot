// Tests for the clock readings of the runner: the usage-limit reset time (scout.mjs `limitResets`) and the nightly prune
// hour (lanes-local.mjs `untilNightly`). Every moment is injected; the zones are far from UTC and from each other, so a
// reading in the machine's zone (whatever it is) cannot pass by accident. No sleeps, no network.
// scout.mjs starts a server when it is loaded, so the function is read out of its source, from `RESETS` to `usageLimit`.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { readSettings, untilNightly } from "./lanes-local.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(HERE, "scout.mjs"), "utf8");
const body = source.slice(source.indexOf("const RESETS ="), source.indexOf("function usageLimit("));
const limitResets = new Function(`${body}\nreturn limitResets;`)();

const HOUR = 3_600_000;
const at = (iso) => Date.parse(iso);
const resets = (text, now) => limitResets(text, at(now))?.toISOString() ?? null;

describe("the time a usage limit lifts", () => {
  it("is read in the zone the words name, not the machine's", () => {
    // 3pm in Los Angeles on 6 Nov 2026 (PST, UTC-8) is 23:00Z.
    assert.equal(resets("You've hit your session limit · resets 3pm (America/Los_Angeles)", "2026-11-06T18:00:00Z"), "2026-11-06T23:00:00.000Z");
  });

  it("takes the next such time on that zone's clock, which can be tomorrow there", () => {
    // 18:00Z is 07:00 the next morning in Auckland (NZDT, UTC+13): 3pm is that afternoon, 02:00Z.
    assert.equal(resets("resets 3pm (Pacific/Auckland)", "2026-11-06T18:00:00Z"), "2026-11-07T02:00:00.000Z");
    // 03:00Z is 16:00 in Auckland: 3pm has passed there, so it is the next day's 3pm (02:00Z, the day after).
    assert.equal(resets("resets 3pm (Pacific/Auckland)", "2026-11-06T03:00:00Z"), "2026-11-07T02:00:00.000Z");
  });

  it("reads a date and time in the named zone", () => {
    assert.equal(resets("weekly limit · resets Nov 9, 8:30am (Pacific/Kiritimati)", "2026-11-06T00:00:00Z"), "2026-11-08T18:30:00.000Z");
  });

  it("reads UTC as a zone", () => {
    assert.equal(resets("resets 3pm (UTC)", "2026-11-06T18:00:00Z"), "2026-11-07T15:00:00.000Z");
  });

  it("takes the later pass of the hour that repeats when the clocks go back, when the first has gone", () => {
    // 1 Nov 2026, Los Angeles: 01:00 happens at 08:00Z (PDT) and again at 09:00Z (PST).
    assert.equal(resets("resets 1:30am (America/Los_Angeles)", "2026-11-01T09:20:00Z"), "2026-11-01T09:30:00.000Z");
    assert.equal(resets("resets 1:30am (America/Los_Angeles)", "2026-11-01T07:00:00Z"), "2026-11-01T08:30:00.000Z");
  });

  it("is not thrown by the hour the clocks skip", () => {
    // 8 Mar 2026, Los Angeles: 02:30 does not exist; the answer is a moment within the hour, not null or NaN.
    const got = limitResets("resets 2:30am (America/Los_Angeles)", at("2026-03-08T05:00:00Z"));
    assert.ok(got && got.getTime() > at("2026-03-08T05:00:00Z") && got.getTime() - at("2026-03-08T05:00:00Z") < 6 * HOUR);
  });

  it("falls back to the machine's zone when the words name none or a name this runtime does not know", () => {
    const now = at("2026-11-06T18:00:00Z");
    const own = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const expected = limitResets(`resets 3pm (${own})`, now).toISOString();
    assert.equal(limitResets("resets 3pm", now).toISOString(), expected);
    assert.equal(limitResets("resets 3pm (Mars/Olympus_Mons)", now).toISOString(), expected);
  });

  it("still takes an epoch as it is", () => {
    assert.equal(resets("Claude AI usage limit reached|1793995200", "2026-11-06T18:00:00Z"), "2026-11-06T20:00:00.000Z");
  });
});

describe("the nightly prune hour", () => {
  it("is 3:30 on the owner's clock when a zone is given", () => {
    // 12:00Z is 01:00 on 7 Nov in Auckland (UTC+13): 3:30 is 2.5 hours on.
    assert.equal(untilNightly(new Date("2026-11-06T12:00:00Z"), "Pacific/Auckland"), 2.5 * HOUR);
    // 16:00Z is 05:00 on 7 Nov there: the next 3:30 is on the 8th, 22.5 hours on.
    assert.equal(untilNightly(new Date("2026-11-06T16:00:00Z"), "Pacific/Auckland"), 22.5 * HOUR);
  });

  it("counts the real time across the night the clocks go back", () => {
    // 1 Nov 2026, Los Angeles: from 00:00 PDT (07:00Z) to 3:30 PST (11:30Z) is 4.5 real hours.
    assert.equal(untilNightly(new Date("2026-11-01T07:00:00Z"), "America/Los_Angeles"), 4.5 * HOUR);
  });

  it("uses the machine's clock when there is no zone, or one this runtime does not know", () => {
    const now = new Date(2026, 9, 16, 12, 0);
    assert.equal(untilNightly(now), 15.5 * HOUR);
    assert.equal(untilNightly(now, "Mars/Olympus_Mons"), 15.5 * HOUR);
  });
});

describe("the owner's zone in the runner's settings", () => {
  const noFile = () => { throw Object.assign(new Error("none"), { code: "ENOENT" }); };
  const settingsWith = (env) => readSettings({ env: { RESUMEBOT_DATA_DIR: "/tmp/x", ...env }, platform: "linux", home: "/h", read: noFile });

  it("is JOBSEARCH_TIMEZONE when it names a zone", () => {
    assert.equal(settingsWith({ JOBSEARCH_TIMEZONE: "Pacific/Auckland" }).timezone, "Pacific/Auckland");
  });

  it("is empty when it is unset, blank or not a zone", () => {
    assert.equal(settingsWith({}).timezone, "");
    assert.equal(settingsWith({ JOBSEARCH_TIMEZONE: "  " }).timezone, "");
    assert.equal(settingsWith({ JOBSEARCH_TIMEZONE: "Mars/Olympus_Mons" }).timezone, "");
  });
});
