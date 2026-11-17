// Tests for end-session.mjs, the hook that ends a session once its work is accepted: which tool replies finish a session, in
// each shape a hook may be handed a reply in, and that the hook itself prints the stop and leaves its note.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { finishedBy, repliesIn } from "./end-session.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const call = (tool_name, reply, how = "text") => ({
  tool_name,
  tool_response: how === "text" ? [{ type: "text", text: JSON.stringify(reply) }]
    : how === "string" ? JSON.stringify(reply)
      : how === "object" ? reply
        : { content: [{ type: "text", text: JSON.stringify(reply) }] },
});
const ANALYST = "mcp__jobsearch__jobs_analysis_submit";

describe("which replies finish a session", () => {
  for (const how of ["text", "string", "object", "content"]) {
    it(`reads a reply handed over as ${how}`, () => {
      assert.equal(finishedBy(call(ANALYST, { ok: true, taken: true, stage: "needs_drafted" }, how)), ANALYST);
      assert.equal(finishedBy(call(ANALYST, { ok: false, taken: false, reason: "not taken; fix these" }, how)), null);
    });
  }

  it("ends the analyst, the employer researcher and the coach on a submission that was taken, and not on one that was not", () => {
    for (const tool of [ANALYST, "mcp__jobsearch__jobs_employer_report", "mcp__jobsearch__jobs_coach_submit"]) {
      assert.equal(finishedBy(call(tool, { ok: true, taken: true })), tool);
      assert.equal(finishedBy(call(tool, { ok: false, taken: false, tries_left: 2 })), null, tool);
      assert.equal(finishedBy(call(tool, { ok: false, reason: "the app failed on its side" })), null, tool);
    }
  });

  it("ends a browser worker only on the app's last word, never on 'hand back what you hold first'", () => {
    const tool = "mcp__resumebot-browser__browser_next_item";
    assert.equal(finishedBy(call(tool, { ok: true, stop: true, end_session: true, why: "finished" })), tool);
    assert.equal(finishedBy(call(tool, { ok: true, stop: true, why: "paused", next: "Hand back the item you hold first, then stop." })), null);
    assert.equal(finishedBy(call(tool, { ok: true, item: { kind: "source" } })), null);
    assert.equal(finishedBy(call(tool, { ok: true, wait_seconds: 9 })), null);
  });

  it("ends no other tool, and nothing it cannot read", () => {
    assert.equal(finishedBy(call("mcp__jobsearch__jobs_analysis", { ok: true, taken: true })), null, "a read is not a submission");
    assert.equal(finishedBy(call("Bash", { taken: true })), null);
    assert.equal(finishedBy({ tool_name: ANALYST }), null);
    assert.equal(finishedBy({ tool_name: ANALYST, tool_response: "not json at all" }), null);
    assert.equal(finishedBy(null), null);
    assert.deepEqual(repliesIn(undefined), []);
  });
});

describe("the hook as Claude Code runs it", () => {
  const run = (input, env = {}) => spawnSync(process.execPath, [join(HERE, "end-session.mjs")], {
    input, encoding: "utf8", env: { ...process.env, ...env },
  });

  it("prints a stop and leaves its note when the work was accepted", () => {
    const folder = mkdtempSync(join(tmpdir(), "end-session-"));
    try {
      const mark = join(folder, "session.ended");
      const out = run(JSON.stringify(call(ANALYST, { ok: true, taken: true })), { SCOUT_END_MARK: mark });
      assert.equal(out.status, 0);
      const said = JSON.parse(out.stdout);
      assert.equal(said.continue, false);
      assert.match(said.stopReason, /accepted/);
      assert.equal(readFileSync(mark, "utf8").trim(), ANALYST);
    } finally {
      rmSync(folder, { recursive: true, force: true });
    }
  });

  it("prints nothing, leaves no note and exits cleanly when the session goes on, or the input is not JSON", () => {
    const folder = mkdtempSync(join(tmpdir(), "end-session-"));
    try {
      const mark = join(folder, "session.ended");
      for (const input of [JSON.stringify(call(ANALYST, { ok: false, taken: false })), "garbage", ""]) {
        const out = run(input, { SCOUT_END_MARK: mark });
        assert.equal(out.status, 0);
        assert.equal(out.stdout, "");
      }
      assert.ok(!existsSync(mark));
    } finally {
      rmSync(folder, { recursive: true, force: true });
    }
  });

  it("still stops when it has nowhere to leave its note", () => {
    const out = run(JSON.stringify(call(ANALYST, { ok: true, taken: true })), { SCOUT_END_MARK: join(tmpdir(), "no-such-folder-here", "x") });
    assert.equal(out.status, 0);
    assert.equal(JSON.parse(out.stdout).continue, false);
  });
});
