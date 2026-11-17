// The PostToolUse hook that ends a session once its work is accepted (scout.mjs `claudeSettings` puts it in the lane's
// Claude folder). A session's last model turn after its final tool call only writes a closing paragraph nobody reads, and it
// re-reads the whole context to do it When the
// reply of a tool that finishes a session says so, this prints `{"continue": false}`, which makes Claude Code stop with no
// further model call, and leaves a note in the file named by SCOUT_END_MARK so the runner knows the stop is a finish.
//
// It reads the hook's JSON from stdin and never fails: anything it cannot read is "not finished", and the session goes on.
// The tools that finish a session, and what in their reply says it is:
//   analyst  jobs.analysis_submit   taken: true
//   employer researcher  jobs.employer_report  taken: true
//   coach  jobs.coach_submit  taken: true
//   browser  browser.next_item      end_session: true   (the app says stop; a "hand back what you hold first" is not it)

import { writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const FINISHES = {
  mcp__jobsearch__jobs_analysis_submit: (r) => r.taken === true,
  mcp__jobsearch__jobs_employer_report: (r) => r.taken === true,
  mcp__jobsearch__jobs_coach_submit: (r) => r.taken === true,
  "mcp__resumebot-browser__browser_next_item": (r) => r.end_session === true,
};

// The objects a tool's reply holds: the reply may reach a hook as the tool's JSON text, as a list of content blocks whose
// text is that JSON, or as the object itself.
export function repliesIn(response) {
  const found = [];
  const take = (value) => {
    if (typeof value === "string") {
      try { take(JSON.parse(value)); } catch { /* not JSON */ }
    } else if (Array.isArray(value)) {
      value.forEach(take);
    } else if (value && typeof value === "object") {
      found.push(value);
      if (value.text !== undefined) take(value.text);
      if (value.content !== undefined) take(value.content);
      if (value.structuredContent !== undefined) take(value.structuredContent);
      if (value.result !== undefined) take(value.result);
    }
  };
  take(response);
  return found;
}

// Whether this hook input is a call that finishes its session: the tool's name when it is, else null.
export function finishedBy(input) {
  const name = String(input?.tool_name || "");
  const finishes = FINISHES[name];
  if (!finishes) return null;
  return repliesIn(input.tool_response).some((r) => { try { return finishes(r); } catch { return false; } }) ? name : null;
}

function main() {
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (c) => { raw += c; });
  process.stdin.on("end", () => {
    let input = {};
    try { input = JSON.parse(raw); } catch { /* nothing to read: the session goes on */ }
    const name = finishedBy(input);
    if (!name) return;
    if (process.env.SCOUT_END_MARK) {
      try { writeFileSync(process.env.SCOUT_END_MARK, `${name}\n`); } catch { /* the stop still works without the note */ }
    }
    process.stdout.write(JSON.stringify({ continue: false, stopReason: `its work was accepted (${name})` }));
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
