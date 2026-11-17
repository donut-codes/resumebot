// Tests for scout.mjs, run natively on Linux, macOS and Windows (`node --test` in this folder, or `dev.py check --runner`). Where
// the platforms differ (file modes, signals) a test asserts what the runner does on the one it runs on: see WINDOWS below.
// Starts the real runner with a fake `claude` that records how it was called, and sends it signed wakes.
// Adapted from another project's planner tests (see app/PROVENANCE.md): the planner and Teacher sessions are gone.

import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { execFileSync, spawn } from "node:child_process";
import { chmodSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer as createHttpServer } from "node:http";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { after, before, describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { killTree } from "./lanes-local.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const SECRET = "test-wake-secret";
const BRIDGE_KEY = "b".repeat(64); // the browser bridge's key, in the secrets folder as the launcher makes it
// Windows has no signals and no file modes. There the runner ends a session with `taskkill /T /F` (the session and everything
// it started, at once, with no request to stop first) and limits a file to its own account with icacls, so the tests of those
// two things assert that instead of SIGTERM, SIGKILL and 0o600.
const WINDOWS = process.platform === "win32";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function freePort() {
  return await new Promise((resolve) => {
    const s = createServer().listen(0, () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

let dir, port, runner, mainRun, calls;

// Every runner started here. Each is its own process group (detached; on Windows the tree, by `killTree`, the supervisor's
// own way), so it can be ended with the fake `claude` sessions it started in one go. A test ends its runner that way (`stop` in `runnerFor`) as well as teardown:
//  - a session still writing into `dir` while it was removed made the removal fail now and then with ENOTEMPTY;
//  - a plain SIGTERM to the runner alone let a chain's next session start during the 300 ms the runner takes to exit, and
//    that session, left running, wrote its call into `calls.jsonl` during the NEXT test, after the test had counted the
//    calls it started with (the test that followed an endless chain of sessions found one too many).
const runners = new Set();
// The fake apps' servers of `runnerFor`. A runner that never came up (its start failed the wait) leaves its app listening, and a
// listening server keeps this file's process from ending: the whole `node --test` then hangs. They are closed with the runners.
const apps = new Set();
function endRunner(child) {
  if (WINDOWS) {
    killTree(child);
    return;
  }
  try { process.kill(-child.pid, "SIGKILL"); } catch { try { child.kill("SIGKILL"); } catch { /* already gone */ } }
}
async function endAll() {
  const exits = [];
  for (const child of runners) {
    exits.push(new Promise((resolve) => (child.exitCode !== null || child.signalCode !== null ? resolve() : child.once("exit", resolve))));
    endRunner(child);
  }
  runners.clear();
  for (const app of apps) { app.close(); app.closeAllConnections(); }
  apps.clear();
  await Promise.race([Promise.all(exits), sleep(5000)]);
}

// The calls recorded so far. A session appends its call in one write, but a read that lands in the middle of that write
// (a call's line holds the whole system prompt, several KB) sees the line cut short and JSON.parse throws; a call counts
// from the read that finds its line closed by the newline.
function readCalls() {
  if (!existsSync(calls)) return [];
  const text = readFileSync(calls, "utf8");
  return text.slice(0, text.lastIndexOf("\n") + 1).split("\n").filter(Boolean).map((l) => JSON.parse(l));
}

// A wait is for an event (a runner listening, a session starting, a call arriving), and how long that takes depends on how busy this
// machine is: a process can take longer than any fixed number to start when others compete for it. The deadline only ends a wait
// that will never finish, so it is never shorter than WAIT_FLOOR_MS, whatever a call asks (found under load: a runner not
// listening after 8 s failed a test, and its app, left listening, hung the run). What a test asserts about timing it measures
// itself, from the times the app recorded.
const WAIT_FLOOR_MS = 60_000;
async function waitFor(predicate, ms = 8000) {
  const end = Date.now() + Math.max(ms, WAIT_FLOOR_MS);
  while (Date.now() < end) {
    if (await predicate()) return;
    await sleep(50);
  }
  assert.fail("timed out waiting");
}

async function wake(payload, { secret = SECRET, at = port } = {}) {
  const body = JSON.stringify(payload);
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = createHmac("sha256", secret).update(`${timestamp}.`).update(body).digest("hex");
  return await fetch(`http://127.0.0.1:${at}/wake`, {
    method: "POST",
    headers: { Connection: "close", "Content-Type": "application/json", "X-Webhook-Timestamp": timestamp, "X-Webhook-Signature-V2": signature },
    body,
  });
}

const arg = (call, flag) => call.argv[call.argv.indexOf(flag) + 1];
// What a session was given as its instructions: the text of the file `--append-system-prompt-file` (a seat: `--system-prompt-file`) names, which the fake
// `claude` copies into its record the moment it starts (the runner deletes the file when the session ends).
const systemOf = (call) => call.system;
// What every fake `claude` does first about the instructions and the MCP config: notes the files it was given, how they are
// readable (the mode; on Windows, which accounts icacls says have access) and what the instructions hold. A session that was
// given no instructions has `system: null`.
const SYSTEM_PROBE = `
const argAfter = (flag) => { const at = process.argv.indexOf(flag); return at < 0 ? null : process.argv[at + 1]; };
const systemFile = argAfter("--append-system-prompt-file") || argAfter("--system-prompt-file");
const mcpFile = argAfter("--mcp-config");
const attempt = (f, how) => { try { return how(f); } catch { return null; } };
const system = systemFile && attempt(systemFile, (f) => fs.readFileSync(f, "utf8"));
const access = (f) => f && attempt(f, (g) => process.platform === "win32"
  ? { acl: require("child_process").execFileSync("icacls", [g], { encoding: "utf8" }) }
  : { mode: fs.statSync(g).mode & 0o777 });
const systemAccess = access(systemFile);
const mcpAccess = access(mcpFile);
`;
const SYSTEM_NOTES = "system, systemFile, systemAccess, mcpFile, mcpAccess";

// The accounts that can reach a file, as a fake `claude` saw it (`systemAccess`, `mcpAccess`): on Windows the names icacls
// lists (with the path in front of the first); elsewhere the mode's group and other bits, which must be empty.
const accountsOf = ({ acl }, file) => acl.split(/\r?\n/).filter((l) => l.includes(":(")).map((l) => l.slice(0, l.indexOf(":(")).replace(file, "").trim().toLowerCase());
// Windows' own whoami: another one (Git's) may come first on the PATH, and says the name without its domain.
const ownAccount = () => execFileSync(join(process.env.SystemRoot || "C:\\Windows", "System32", "whoami.exe"), { encoding: "utf8" }).trim().toLowerCase();
function assertOnlyTheRunnerCanRead(access, file, what) {
  assert.ok(access, `${what}: the session could see it`);
  if (WINDOWS) assert.deepEqual(accountsOf(access, file), [ownAccount()], `${what}: nobody but this account can read it`);
  else assert.equal(access.mode & 0o077, 0, `${what}: nobody but the runner can read it`);
}
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch (e) { return e.code === "EPERM"; } };
// A session of `fakeClaude` has stopped: where signals exist it noted the request to stop in `fake.term`; on Windows there is
// no request to note, so what shows it is that the session, and what it started, are gone.
const sessionStopped = (fake) => waitFor(() => WINDOWS
  ? existsSync(fake.pids) && readFileSync(fake.pids, "utf8").split("\n").filter(Boolean).every((pid) => !alive(Number(pid)))
  : existsSync(fake.term));
// The values after `--allowedTools`, up to the next flag.
const allowed = (call) => {
  const rest = call.argv.slice(call.argv.indexOf("--allowedTools") + 1);
  const end = rest.findIndex((a) => a.startsWith("--"));
  return end < 0 ? rest : rest.slice(0, end);
};

// A seat the fake app hands out (`seatReply`): the app's own system text and prompt, the model and effort it names.
const SEAT = { id: "seat-1", model: "opus", effort: "high", label: "Resume for a role", system: "Seat system text.", prompt: "Write the resume." };
// The fake app's answer to a session asking for its seat; anything else is left to the default.
const seatReply = (url) => (url.endsWith("/api/v1/agent/seat/take") ? { json: { seat: SEAT } } : undefined);

const search = { mode: "scout", reason: "search_started", project_id: "p-search" };
// A request of its own connection (`Connection: close`) each time: one sent down a connection the runner is closing at that moment
// (its idle timeout) is reset, and the pool does not know it until it tries (found under load: ECONNRESET).
const health = async (at) => (await fetch(`http://127.0.0.1:${at}/health`, { headers: { Connection: "close" } })).json();
const ended = (runner) => runner.seen.some((s) => s.body?.state === "ended");
// Whether something the app saw (`seen` in `runnerFor`) was the question "which work waits": by its path, whatever its query.
// How far apart two processes' clocks may read on one machine (see the hold test, which compares three of them).
const CLOCK_SLACK_MS = 100;
const askedForWork = (s) => new URL(s.url, "http://app.test").pathname.endsWith("/work");

// A wake with the timestamp and secret of the test's choosing (the `wake` above signs it as the app does).
async function rawWake(at, { timestamp, body = JSON.stringify(search), secret = SECRET }) {
  const signature = createHmac("sha256", secret).update(`${timestamp}.`).update(body).digest("hex");
  return await fetch(`http://127.0.0.1:${at}/wake`, {
    method: "POST",
    headers: { Connection: "close", "Content-Type": "application/json", "X-Webhook-Timestamp": String(timestamp), "X-Webhook-Signature-V2": signature },
    body,
  });
}

// A fake `claude` of another kind than the one made in `before`. It records its call when it starts, and `body`
// says what it then does; it notes in `<bin>.term` the moment it is asked to stop. All of them end by themselves
// after 30 s, so none outlives a test. Each has a folder of its own (made new, whatever its name), so two can never share
// a file: not the program a runner is running, not the `.term` note a test is waiting for.
function fakeClaude(name, body) {
  // `.cjs`: the runner starts a .js, .mjs or .cjs file under Node on every system (Windows cannot run a script by its name).
  const bin = join(mkdtempSync(join(dir, `${name}-`)), `${name}.cjs`);
  const term = `${bin}.term`;
  const pids = `${bin}.pids`;
  writeFileSync(
    bin,
    `#!/usr/bin/env node
const fs = require("fs");
${SYSTEM_PROBE}
// Its own process id, and on Windows a process it started (a session's tools): the runner ends the whole tree there.
const pids = [process.pid];
if (process.platform === "win32") {
  pids.push(require("child_process").spawn(process.execPath, ["-e", "setTimeout(() => {}, 30000)"], { stdio: "ignore", windowsHide: true }).pid);
}
fs.writeFileSync(${JSON.stringify(pids)}, pids.join("\\n"));
fs.appendFileSync(${JSON.stringify(calls)}, JSON.stringify({ argv: process.argv.slice(2), start: Date.now(), cache: process.env.DISABLE_PROMPT_CACHING || null, ${SYSTEM_NOTES} }) + "\\n");
const noteTerm = () => fs.appendFileSync(${JSON.stringify(term)}, "term\\n");
setTimeout(() => process.exit(0), 30000);
${body}
`,
  );
  chmodSync(bin, 0o755);
  return { bin, term, pids };
}
// Takes its time, and ends when asked to.
const sleepy = (name) => fakeClaude(name, `process.on("SIGTERM", () => { noteTerm(); process.exit(143); }); setInterval(() => {}, 1000);`);
// Takes its time, and does not end when asked to: only a kill ends it.
const stubborn = (name) => fakeClaude(name, `process.on("SIGTERM", noteTerm); setInterval(() => {}, 1000);`);
const failing = (name) => fakeClaude(name, `console.error("boom"); process.exit(1);`);
const garbling = (name) => fakeClaude(name, `console.log("not what a finished session prints"); process.exit(0);`);
// Ends the moment it starts, as a session that went well: for tests of what the runner does around a session, which need
// no wait for Claude. Made once, on first use.
let finishingBin;
const finishing = () => (finishingBin ??= fakeClaude("claude-finishing", `console.log(JSON.stringify({ num_turns: 1, is_error: false, result: "done" })); process.exit(0);`).bin);

// The folders a runner is given. In the image its working folder is the test's own; a runner on Windows must be given a working
// folder that is empty and a Claude folder of its own, and refuses to start without them (the owner's own Claude folder is never
// a lane's).
function folders(own) {
  if (!WINDOWS) return { SCOUT_WORKDIR: dir };
  mkdirSync(join(own, "work"));
  return { SCOUT_WORKDIR: join(own, "work"), SCOUT_CLAUDE_CONFIG_DIR: join(own, "claude") };
}

// A runner of its own (every runner here is the real scout.mjs): the common settings, with `env` on top, started and
// waited for until it says it is listening (its first line says `scout (lane any) listening`, so the wait is for
// `listening on :`). A setting given as undefined is left out. A runner that is meant to stop at once (a port that is
// taken) is started with `listening: false` and not waited for.
// Its log and its MCP config are in a folder made new for it (`mkdtempSync`), never named by the tag alone: a tag used
// twice would otherwise read the first runner's "listening" line as the second's, and share one config file.
// A port found free by `freePort` can be taken by another process between that and the runner's bind (the machine hands the same
// ports to the outgoing connections of every process on it; found under load: a runner said "cannot listen" and the wait for
// "listening" ran out). With `retryTaken` that one outcome gives null, for the caller to choose another port; without it, it fails
// at once with the runner's own words.
async function launch(tag, env = {}, { listening = true, retryTaken = false } = {}) {
  const own = mkdtempSync(join(dir, `${tag}-`));
  const logFile = join(own, "runs.log");
  const mcpConfig = join(own, "mcp.json");
  const child = spawn(process.execPath, [join(HERE, "scout.mjs")], {
    env: {
      ...process.env, SCOUT_CHECK_IN_MIN: "20000", SCOUT_MODEL: "sonnet-test", SCOUT_EFFORT: "medium",
      SCOUT_SECRETS_DIR: join(dir, "secrets"), SCOUT_INSTRUCTIONS_DIR: join(dir, "instructions"),
      SCOUT_MCP_CONFIG: mcpConfig, SCOUT_CLAUDE_BIN: join(dir, "claude.cjs"), ...folders(own),
      SCOUT_LOG: logFile, ...env,
    },
    stdio: "ignore",
    detached: !WINDOWS,
    windowsHide: true,
  });
  runners.add(child);
  if (listening) {
    const said = () => (existsSync(logFile) ? readFileSync(logFile, "utf8") : "");
    await waitFor(() => /listening on :|cannot listen on/.test(said())).catch((e) => {
      e.message = `${e.message} for ${tag} to listen; its log: ${JSON.stringify(said())}; exit ${child.exitCode}`;
      throw e;
    });
    if (/cannot listen on/.test(said())) {
      if (retryTaken) {
        runners.delete(child);
        endRunner(child);
        return null;
      }
      assert.fail(said());
    }
  }
  return { child, mcpConfig, log: () => (existsSync(logFile) ? readFileSync(logFile, "utf8") : "") };
}

// A runner against a fake app. The app answers the work question from `answers` (an array, then nothing; or a function
// of the question's number), and everything else with {ok: true} unless `reply(url, body)` returns {status?, json}.
// A session takes two answers (one when it starts, one for the chain after it); `delayMs` holds back each work answer.
// `seen` is everything the app was sent, each with the time it arrived (`at`). The work question carries a query
// (the runner asks `?only=any`): it is the path that says it is the work question.
async function runnerFor(tag, answers, extraEnv = {}, { reply, delayMs = 0 } = {}) {
  const seen = [];
  let asked = 0;
  const app = createHttpServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", async () => {
      const text = Buffer.concat(chunks).toString("utf8");
      const body = text ? JSON.parse(text) : null;
      seen.push({ method: req.method, url: req.url, auth: req.headers.authorization, body, at: Date.now() });
      const isWork = askedForWork({ url: req.url });
      const n = isWork ? asked++ : 0;
      if (isWork && delayMs) await sleep(delayMs);
      const answer = isWork
        ? { json: ((a) => (a && typeof a === "object" ? a : { next: a ?? null }))(typeof answers === "function" ? answers(n) : answers[n]) }
        : (reply?.(req.url, body) ?? { json: { ok: true } });
      res.writeHead(answer.status ?? 200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(answer.json ?? {}));
    });
  });
  await new Promise((resolve) => app.listen(0, "127.0.0.1", resolve));
  apps.add(app);
  let at, started;
  for (let attempt = 1; !started; attempt++) {
    at = await freePort();
    started = await launch(tag, {
      SCOUT_PORT: String(at), RESUMEBOT_MCP_URL: `http://127.0.0.1:${app.address().port}/mcp`, ...extraEnv,
    }, { retryTaken: attempt < 10 });
  }
  return {
    seen, at, child: started.child, log: started.log, mcpConfig: started.mcpConfig,
    works: () => seen.filter(askedForWork).length,
    stop: () => { endRunner(started.child); app.close(); apps.delete(app); },
  };
}

before(async () => {
  dir = mkdtempSync(join(tmpdir(), "scout-test-"));
  calls = join(dir, "calls.jsonl");
  mkdirSync(join(dir, "secrets"));
  for (const [name, value] of [["claude-oauth-token", "tok"], ["agent-token", "ptok"], ["wake-secret", SECRET]]) {
    writeFileSync(join(dir, "secrets", name), value);
  }
  cpSync(join(HERE, "..", "instructions"), join(dir, "instructions"), { recursive: true });
  // A fake `claude`: records its arguments, takes a moment (so overlap would show), and answers like one.
  const fake = join(dir, "claude.cjs");
  writeFileSync(
    fake,
    `#!/usr/bin/env node
const fs = require("fs");
const start = Date.now();
${SYSTEM_PROBE}
setTimeout(() => {
  fs.appendFileSync(${JSON.stringify(calls)}, JSON.stringify({ argv: process.argv.slice(2), start, end: Date.now(),
    ${SYSTEM_NOTES}, hasToken: !!process.env.CLAUDE_CODE_OAUTH_TOKEN, apiKey: process.env.ANTHROPIC_API_KEY || null,
    mcpOutputTokens: process.env.MAX_MCP_OUTPUT_TOKENS || null }) + "\\n");
  console.log(JSON.stringify({ num_turns: 2, is_error: false, result: "done" }));
}, 400);
`,
  );
  chmodSync(fake, 0o755);
  for (let attempt = 1; !mainRun; attempt++) {
    port = await freePort();
    mainRun = await launch("main", {
      ANTHROPIC_API_KEY: "must-not-reach-claude",
      SCOUT_PORT: String(port),
      RESUMEBOT_MCP_URL: "http://127.0.0.1:9/mcp", // nothing listens: a refused heartbeat must not matter
      SCOUT_CLAUDE_BIN: fake,
      SCOUT_MAX_TURNS: "17",
      SCOUT_RUN_TIMEOUT_MIN: "1",
    }, { retryTaken: attempt < 10 });
  }
  runner = mainRun.child;
});

after(async () => {
  await endAll();
  // The retries cover a straggler's last write landing as the folder is emptied.
  // Best effort: Windows can hold a just-ended test's files for seconds under load, and a folder left in the temp directory is no
  // reason to fail a file whose tests passed.
  try { rmSync(dir, { recursive: true, force: true, maxRetries: 20, retryDelay: 200 }); } catch (e) { console.warn(`left ${dir}: ${e.message}`); }
});

describe("the scout runner", () => {
  it("runs a scout session on a scout wake: the scout instructions alone, its four tools and the web", async () => {
    const res = await wake({ mode: "scout", reason: "search_started", project_id: "run-1" });
    assert.equal(res.status, 202);
    await waitFor(() => readCalls().length === 1);
    const call = readCalls()[0];
    const system = systemOf(call);
    assert.match(system, /You are the scout/);
    assert.doesNotMatch(system, /Scoring: answer the owner's asks/, "the scout judges no posting: the scoring paragraph is the analyst's");
    assert.equal(arg(call, "--tools"), "WebSearch,WebFetch", "no shell and no files");
    assert.deepEqual(allowed(call), [
      "mcp__jobsearch__jobs_profile", "mcp__jobsearch__jobs_boards", "mcp__jobsearch__jobs_board_found",
      "mcp__jobsearch__jobs_report", "WebSearch", "WebFetch",
    ]);
    assert.ok(!allowed(call).some((t) => /titles|candidates/.test(t)), "the scout judges no title and no listing");
    assert.ok(!allowed(call).includes("mcp__jobsearch__jobs_seen"), "the scout no longer searches for roles");
    assert.match(arg(call, "-p"), /wake search_started project run-1/);
    assert.equal(arg(call, "--model"), "sonnet-test");
    assert.equal(arg(call, "--effort"), "medium");
    assert.equal(arg(call, "--max-turns"), "17");
    assert.ok(call.argv.includes("--strict-mcp-config"));
    assert.equal(call.hasToken, true);
    assert.equal(call.apiKey, null, "subscription only, never the API");
    assert.equal(call.mcpOutputTokens, "100000", "a reply up to the app's ceiling reaches the session whole");
    const config = JSON.parse(readFileSync(mainRun.mcpConfig, "utf8"));
    assert.deepEqual(Object.keys(config.mcpServers), ["jobsearch"]);
    assert.equal(config.mcpServers.jobsearch.headers.Authorization, "Bearer ptok");
    assert.match(config.mcpServers.jobsearch.headers["X-Session-Id"], /^[a-z]+-[0-9a-f]{12}$/, "the call names its session");
    assert.equal(config.mcpServers.jobsearch.headers["X-Runner"], undefined, "a runner with no name sends none");
  });

  it("runs the analyst in a session of its own, with only its own tools", async () => {
    const before = readCalls().length;
    await wake({ mode: "scout", reason: "analysis_asked", project_id: "role-1" });
    await waitFor(() => readCalls().length === before + 1);
    const [analyst] = readCalls().slice(before);
    assert.match(systemOf(analyst), /You are the analyst/);
    assert.match(systemOf(analyst), /Scoring: answer the owner's asks/);
    assert.deepEqual(allowed(analyst), ["mcp__jobsearch__jobs_analysis", "mcp__jobsearch__jobs_analysis_submit", "WebSearch", "WebFetch"]);
    assert.equal(arg(analyst, "--model"), "sonnet-test", "only the title pass runs on the small model");
    assert.equal(arg(analyst, "--effort"), "medium");
  });

  it("runs a title session on a tag_roles wake when the app can't be asked: the roles to tag are the title pass's", async () => {
    const before = readCalls().length;
    assert.equal((await wake({ mode: "scout", reason: "tag_roles", project_id: "job-1" })).status, 202);
    await waitFor(() => readCalls().length === before + 1);
    const call = readCalls()[before];
    assert.match(systemOf(call), /You judge titles/);
    assert.deepEqual(allowed(call), ["mcp__jobsearch__jobs_titles_next", "mcp__jobsearch__jobs_titles_report"]);
    assert.match(arg(call, "-p"), /wake tag_roles project job-1/);
  });

  it("runs a site session on a site_project wake: the scout's role text, the site tools and the web, no browser", async () => {
    const before = readCalls().length;
    assert.equal((await wake({ mode: "scout", reason: "site_project", project_id: "site-1" })).status, 202);
    await waitFor(() => readCalls().length === before + 1);
    const call = readCalls()[before];
    const system = systemOf(call);
    assert.match(system, /learning to read the owner's sites/);
    assert.doesNotMatch(system, /You are the analyst/);
    assert.equal(arg(call, "--tools"), "WebSearch,WebFetch", "no shell, no files and no browser");
    const names = allowed(call).filter((t) => t.startsWith("mcp__jobsearch__")).map((t) => t.slice("mcp__jobsearch__".length));
    assert.deepEqual(names.sort(), [
      "act_complete", "doc_read", "item_lookup", "lesson_list", "lesson_record", "lesson_retire", "plan_propose",
      "plan_revise", "review_read", "skill_try", "task_complete", "task_escalate", "task_next", "work_next",
    ]);
    assert.deepEqual(allowed(call).slice(-2), ["WebSearch", "WebFetch"]);
    assert.ok(!names.includes("project_create"), "a session starts no project of its own");
    assert.ok(!names.some((n) => /check/.test(n)), "no tool writes a check result");
    assert.ok(!names.some((n) => n.startsWith("jobs_")), "none of the search tools");
    assert.match(arg(call, "-p"), /wake site_project project site-1/);
    assert.match(arg(call, "-p"), /work\.next/);
  });

  it("ignores a wake for anyone else's mode and a wake with a bad signature", async () => {
    const before = readCalls().length;
    assert.equal((await wake({ mode: "other", reason: "x", project_id: "p3" })).status, 202);
    assert.equal((await wake({ mode: "another", reason: "y", project_id: "p3" })).status, 202);
    assert.equal((await wake({ mode: "scout", reason: "x", project_id: "p3" }, { secret: "wrong" })).status, 401);
    await sleep(900);
    assert.equal(readCalls().length, before);
  });

  it("never overlaps sessions: a wake and two more mid-run make one session, then one folded follow-up", async () => {
    const before = readCalls().length;
    await wake({ mode: "scout", reason: "search_started", project_id: "p4" });
    await wake({ mode: "scout", reason: "search_started", project_id: "p5" });
    await wake({ mode: "scout", reason: "search_started", project_id: "p6" });
    await waitFor(() => readCalls().length === before + 2, 12000);
    await sleep(1200);
    const [first, second, ...extra] = readCalls().slice(before);
    assert.equal(extra.length, 0, "the two wakes during the first run folded into one follow-up");
    assert.match(arg(second, "-p"), /follow-up/);
    assert.ok(second.start >= first.end - 20, "the sessions overlapped");
  });
});

describe("Claude's peak hours, blacked out", () => {
  const stopOnAlive = (_url, body) => ({ json: body?.state === "alive" ? { ok: true, stop: true } : { ok: true } });

  it("starts no session when the app says stop", async () => {
    const r = await runnerFor("peak", () => "search", {}, { reply: () => ({ json: { ok: true, stop: true } }) });
    try {
      const before = readCalls().length;
      await wake(search, { at: r.at });
      await waitFor(() => r.log().includes("peak hours are blacked out"));
      await waitFor(() => ended(r));
      assert.equal(readCalls().length, before, "no Claude session started");
    } finally {
      r.stop();
    }
  });

  it("stops a session that is running when a later heartbeat says stop, and starts no chain after it", async () => {
    const fake = sleepy("claude-blackout");
    const before = readCalls().length;
    // The app says stop only once the session has recorded that it started. Saying it on the first beat would end a session
    // that a loaded machine had not yet got as far as writing its call (node's start-up outlasts a 0.2 s beat), and the
    // test would wait for a call that never comes.
    const stopOnceStarted = (url, body) => (readCalls().length > before ? stopOnAlive(url, body) : { json: { ok: true } });
    const r = await runnerFor("blackout", () => "search", { SCOUT_CLAUDE_BIN: fake.bin, SCOUT_HEARTBEAT_SEC: "0.2" }, { reply: stopOnceStarted });
    try {
      await wake(search, { at: r.at });
      await waitFor(() => readCalls().length === before + 1);
      await waitFor(() => r.log().includes("run stopped: Claude's peak hours are blacked out"));
      await sessionStopped(fake);
      await waitFor(() => ended(r));
      assert.match(r.log(), WINDOWS ? /exit=1\b/ : /signal=SIGTERM|exit=143/, "ended by the tree kill (Windows) or by the request to stop");
      await waitFor(async () => (await health(r.at)).running === false);
      await sleep(300);
      assert.equal(readCalls().length, before + 1, "a stopped session is not followed by another");
      assert.equal(r.works(), 1);
    } finally {
      r.stop();
    }
  });
});

describe("site work, as the app says", () => {
  it("starts a site session when the app's work answer is site, as its own kind of session, then asks again", async () => {
    // Three answers are site work (before the first session, after it, before the second), then nothing is waiting.
    const r = await runnerFor("site", (n) => (n < 3 ? "site" : null));
    try {
      const before = readCalls().length;
      // Even a search wake goes by the app's word on what waits.
      await wake({ mode: "scout", reason: "search_started", project_id: "p10" }, { at: r.at });
      await waitFor(() => readCalls().length === before + 2, 12000);
      await waitFor(() => r.seen.filter((s) => s.body?.state === "ended").length === 2);
      const [first, second, ...extra] = readCalls().slice(before);
      assert.equal(extra.length, 0, "the app said nothing waits after the second session");
      for (const call of [first, second]) assert.match(systemOf(call), /learning to read the owner's sites/);
      assert.match(arg(second, "-p"), /more work: site/);
      const started = r.seen.filter((s) => s.body && s.body.state === "started").map((s) => s.body.session_id);
      assert.equal(started.length, 2);
      assert.ok(started.every((id) => /^site-[0-9a-f]{12}$/.test(id)), `site sessions are named site-...: ${started}`);
      assert.ok(r.seen.filter((s) => s.body?.session_id).every((s) => s.body.lane === "any"), "the runner names the pool's lane");
    } finally {
      r.stop();
    }
  });
});

describe("the posting parser (Fill from a posting)", () => {
  it("starts a parser session on a parse_asked wake even when the app can't be asked: Haiku 5.5 at low effort, two tools, no web", async () => {
    const before = readCalls().length;
    assert.equal((await wake({ mode: "scout", reason: "parse_asked", project_id: "job-1" })).status, 202);
    await waitFor(() => readCalls().length === before + 1);
    const call = readCalls()[before];
    assert.equal(arg(call, "--model"), "claude-haiku-5-5", "the parser is a Haiku 5.5 session");
    assert.equal(arg(call, "--effort"), "low");
    const system = systemOf(call);
    assert.match(system, /You split a posting into a role/);
    assert.doesNotMatch(system, /You are the scout|You judge titles|You are the analyst/);
    assert.equal(arg(call, "--tools"), "", "no built-in tools at all: no web, no shell, no files");
    assert.deepEqual(allowed(call), ["mcp__jobsearch__jobs_parse_next", "mcp__jobsearch__jobs_parse_report"]);
    assert.match(arg(call, "-p"), /wake parse_asked project job-1/);
    assert.match(arg(call, "-p"), /jobs\.parse_next/);
    assert.equal(arg(call, "--max-turns"), "17");
  });

  it("gives the same session the resume mode's instructions after the posting's, with the same two tools", async () => {
    const before = readCalls().length;
    assert.equal((await wake({ mode: "scout", reason: "parse_asked", project_id: "job-resume" })).status, 202);
    await waitFor(() => readCalls().length === before + 1);
    const system = systemOf(readCalls()[before]);
    const posting = system.indexOf("You split a posting into a role");
    const resume = system.indexOf("When the text is a resume");
    assert.ok(posting >= 0 && resume > posting, "the posting parser's instructions first, then the resume mode's");
    assert.match(system, /`kind` is `resume`/);
    assert.match(system, /Copy the person's words exactly/);
    assert.doesNotMatch(system, /<!--/, "the instruction files' header comments are not sent");
    assert.deepEqual(allowed(readCalls()[before]), ["mcp__jobsearch__jobs_parse_next", "mcp__jobsearch__jobs_parse_report"]);
    assert.equal(arg(readCalls()[before], "--tools"), "", "still no web, no shell, no files");
  });

  it("starts parser sessions while the app says parser, ahead of the scout, each named parser-...", async () => {
    // A session takes two answers (one when it starts, one for the chain after it): parser, parser, then the scout.
    const runner = await runnerFor("parser", ["parser", "parser", "parser", "search", "search", null]);
    try {
      const before = readCalls().length;
      await wake({ mode: "scout", reason: "search_started", project_id: "run-p" }, { at: runner.at });
      await waitFor(() => readCalls().length === before + 3, 15000);
      const [first, second, third] = readCalls().slice(before);
      for (const call of [first, second]) {
        assert.equal(arg(call, "--model"), "claude-haiku-5-5");
        assert.equal(arg(call, "--effort"), "low");
        assert.match(systemOf(call), /You split a posting into a role/);
        assert.deepEqual(allowed(call), ["mcp__jobsearch__jobs_parse_next", "mcp__jobsearch__jobs_parse_report"]);
      }
      assert.match(arg(second, "-p"), /more work: parser/, "while a posting waits, the next session is the parser again");
      assert.match(systemOf(third), /You are the scout/);
      assert.equal(arg(third, "--model"), "sonnet-test", "the scout's own session is on the main model");
      const started = runner.seen.filter((s) => s.body && s.body.state === "started").map((s) => s.body.session_id);
      assert.deepEqual(started.map((id) => id.split("-")[0]), ["parser", "parser", "scout"]);
      assert.ok(started.slice(0, 2).every((id) => /^parser-[0-9a-f]{12}$/.test(id)), `parser sessions are named parser-...: ${started}`);
    } finally {
      runner.stop();
    }
  });

  it("takes the parser's model and effort from its own settings", async () => {
    const own = await runnerFor("parser2", ["parser", null], { SCOUT_PARSER_MODEL: "parser-test", SCOUT_PARSER_EFFORT: "medium" });
    try {
      const before = readCalls().length;
      await wake({ mode: "scout", reason: "search_started", project_id: "run-p2" }, { at: own.at });
      await waitFor(() => readCalls().length === before + 1, 15000);
      const call = readCalls()[before];
      assert.equal(arg(call, "--model"), "parser-test");
      assert.equal(arg(call, "--effort"), "medium");
    } finally {
      own.stop();
    }
  });
});

describe("the role check (Check my roles)", () => {
  it("starts a checker session on a check_asked wake even when the app can't be asked: Haiku 5.5 at low effort, two tools, no web", async () => {
    const before = readCalls().length;
    assert.equal((await wake({ mode: "scout", reason: "check_asked", project_id: "check-1" })).status, 202);
    await waitFor(() => readCalls().length === before + 1);
    const call = readCalls()[before];
    assert.equal(arg(call, "--model"), "claude-haiku-5-5", "the checker is a Haiku 5.5 session");
    assert.equal(arg(call, "--effort"), "low");
    const system = systemOf(call);
    assert.match(system, /You check the owner's role tabs/);
    assert.doesNotMatch(system, /You are the scout|You judge titles|You split a posting/);
    assert.equal(arg(call, "--tools"), "", "no built-in tools at all: no web, no shell, no files");
    assert.deepEqual(allowed(call), ["mcp__jobsearch__jobs_match_next", "mcp__jobsearch__jobs_match_report"]);
    assert.match(arg(call, "-p"), /wake check_asked project check-1/);
    assert.match(arg(call, "-p"), /jobs\.match_next/);
  });

  it("starts checker sessions while the app says checker, each named checker-...", async () => {
    // A session takes two answers (one when it starts, one for the chain after it): checker, checker, then nothing.
    const runner = await runnerFor("checker", ["checker", "checker", "checker", null]);
    try {
      const before = readCalls().length;
      await wake({ mode: "scout", reason: "search_started", project_id: "run-c" }, { at: runner.at });
      await waitFor(() => readCalls().length === before + 2, 15000);
      const [first, second] = readCalls().slice(before);
      for (const call of [first, second]) {
        assert.equal(arg(call, "--model"), "claude-haiku-5-5");
        assert.match(systemOf(call), /You check the owner's role tabs/);
      }
      assert.match(arg(second, "-p"), /more work: checker/, "while a check waits, the next session is the checker again");
      const started = runner.seen.filter((s) => s.body && s.body.state === "started").map((s) => s.body.session_id);
      assert.ok(started.slice(0, 2).every((id) => /^checker-[0-9a-f]{12}$/.test(id)), `checker sessions are named checker-...: ${started}`);
    } finally {
      runner.stop();
    }
  });

  it("takes the checker's model and effort from its own settings", async () => {
    const own = await runnerFor("checker2", ["checker", null], { SCOUT_CHECKER_MODEL: "checker-test", SCOUT_CHECKER_EFFORT: "medium" });
    try {
      const before = readCalls().length;
      await wake({ mode: "scout", reason: "search_started", project_id: "run-c2" }, { at: own.at });
      await waitFor(() => readCalls().length === before + 1, 15000);
      const call = readCalls()[before];
      assert.equal(arg(call, "--model"), "checker-test");
      assert.equal(arg(call, "--effort"), "medium");
    } finally {
      own.stop();
    }
  });
});

describe("the runner saying it is up", () => {
  it("says idle to the app between sessions, with the scout's token, and not while a session runs", async () => {
    // A session takes two answers: the wake's own, then the chain's (nothing waits).
    const runner = await runnerFor("presence", ["search", null], { SCOUT_PRESENCE_SEC: "0.2" });
    const idles = () => runner.seen.filter((s) => s.body && s.body.state === "idle");
    try {
      await waitFor(() => idles().length > 0);
      const [ping] = idles();
      assert.equal(ping.url, "/api/v1/agent/heartbeat");
      assert.match(ping.body.session_id, /^runner-/);
      // Right after one, so the next is a fifth of a second away: the session starts before it.
      await wake({ mode: "scout", reason: "search_started", project_id: "run-i" }, { at: runner.at });
      await waitFor(() => runner.seen.some((s) => s.body && s.body.state === "ended"), 12000);
      const states = runner.seen.filter((s) => s.body && s.body.state).map((s) => s.body.state);
      const from = states.indexOf("started");
      const to = states.indexOf("ended");
      assert.ok(from >= 0 && to > from, `a session started and ended: ${states}`);
      assert.deepEqual(states.slice(from, to + 1).filter((s) => s === "idle"), [], "no idle ping while a session runs");
      const after = idles().length;
      await waitFor(() => idles().length > after, 5000); // and it goes on afterwards
    } finally {
      runner.stop();
    }
  });

  // The pool checks in every 2 minutes and says it is up every 2 minutes, on the same beat. The check-in
  // is already asking the app for work when the presence beat lands a millisecond later, so `running` was true and the
  // beat was skipped, every time: the app heard no idle word from the pool and its Agents page said Offline.
  it("says idle on a check-in that finds nothing, though its own beat lands while that check-in is asking", async () => {
    const runner = await runnerFor("presence-on-the-beat", [], {
      SCOUT_CHECK_IN_MIN: "0.025", SCOUT_PRESENCE_SEC: "1.5",
    }, { delayMs: 400 }); // each ask for work takes 400 ms, so the beat always lands in the middle of one
    const idles = () => runner.seen.filter((s) => s.body && s.body.state === "idle");
    try {
      await waitFor(() => runner.works() >= 3); // it is asking for work all the while, and nothing waits
      await waitFor(() => idles().length >= 2);
      for (const ping of idles()) assert.equal(ping.body.session_id, "runner-idle");
    } finally {
      runner.stop();
    }
  });
});

describe("the title pass (docs/PRD.md 30.1)", () => {
  it("starts a title session when the app says titles: Haiku 5.5 at low effort, its own instructions, two tools and no web", async () => {
    // A session takes two answers (one when it starts, one for the chain after it): titles, titles, then the search.
    const runner = await runnerFor("titles", ["titles", "titles", "titles", "search", "search", null]);
    try {
      const before = readCalls().length;
      await wake({ mode: "scout", reason: "search_started", project_id: "run-t" }, { at: runner.at });
      await waitFor(() => readCalls().length === before + 3, 15000);
      const [first, second, third] = readCalls().slice(before);
      for (const call of [first, second]) {
        assert.equal(arg(call, "--model"), "claude-haiku-5-5", "the title pass is a Haiku 5.5 session");
        assert.equal(arg(call, "--effort"), "low");
        const system = systemOf(call);
        assert.match(system, /You judge titles/);
        assert.doesNotMatch(system, /You are the scout|You are the analyst/);
        assert.equal(arg(call, "--tools"), "", "no built-in tools at all: no web, no shell, no files");
        assert.deepEqual(allowed(call), ["mcp__jobsearch__jobs_titles_next", "mcp__jobsearch__jobs_titles_report"]);
        assert.match(arg(call, "-p"), /jobs\.titles_next/);
      }
      assert.match(arg(second, "-p"), /more work: titles/, "while titles wait, the next session is the title pass again");
      assert.match(systemOf(third), /You are the scout/, "then the scout's turn");
      const started = runner.seen.filter((s) => s.body && s.body.state === "started").map((s) => s.body.session_id);
      assert.deepEqual(started.map((id) => id.split("-")[0]), ["titles", "titles", "scout"]);
      assert.ok(started.slice(0, 2).every((id) => /^titles-[0-9a-f]{12}$/.test(id)), `title sessions are named titles-...: ${started}`);
    } finally {
      runner.stop();
    }
  });

  it("takes the title pass's model and effort from its own settings", async () => {
    const own = await runnerFor("titles2", ["titles", null], { SCOUT_TITLES_MODEL: "titles-test", SCOUT_TITLES_EFFORT: "medium" });
    try {
      const before = readCalls().length;
      await wake({ mode: "scout", reason: "search_started", project_id: "run-t2" }, { at: own.at });
      await waitFor(() => readCalls().length === before + 1, 15000);
      const [call] = readCalls().slice(before);
      assert.equal(arg(call, "--model"), "titles-test");
      assert.equal(arg(call, "--effort"), "medium");
    } finally {
      own.stop();
    }
  });
});

describe("the employer researcher (docs/PRD.md path 36)", () => {
  it("starts a researcher session on an employers_asked wake even when the app can't be asked: Haiku 5.5 at low effort, its two tools and the public web", async () => {
    const before = readCalls().length;
    assert.equal((await wake({ mode: "scout", reason: "employers_asked", project_id: "employer-1" })).status, 202);
    await waitFor(() => readCalls().length === before + 1);
    const call = readCalls()[before];
    assert.equal(arg(call, "--model"), "claude-haiku-5-5", "the researcher is a small Haiku 5.5 session");
    assert.equal(arg(call, "--effort"), "low");
    const system = systemOf(call);
    assert.match(system, /# Researching an employer/);
    assert.doesNotMatch(system, /You are the scout|You judge titles|You split a posting|# Researching a role/);
    assert.equal(arg(call, "--tools"), "WebSearch,WebFetch", "the public web, read-only: no shell and no files");
    assert.deepEqual(allowed(call), [
      "mcp__jobsearch__jobs_employer", "mcp__jobsearch__jobs_employer_report", "WebSearch", "WebFetch",
    ]);
    assert.match(arg(call, "-p"), /wake employers_asked project employer-1/);
    assert.match(arg(call, "-p"), /jobs\.employer/);
  });

  it("starts researcher sessions while the app says researcher, each named researcher-..., one employer a session", async () => {
    const runner = await runnerFor("researcher", ["researcher", "researcher", "researcher", null]);
    try {
      const before = readCalls().length;
      await wake({ mode: "scout", reason: "search_started", project_id: "run-e" }, { at: runner.at });
      await waitFor(() => readCalls().length === before + 2, 15000);
      const [first, second] = readCalls().slice(before);
      for (const call of [first, second]) assert.match(systemOf(call), /# Researching an employer/);
      assert.match(arg(second, "-p"), /more work: researcher/, "while employers wait, the next session is a researcher's again");
      const started = runner.seen.filter((s) => s.body && s.body.state === "started").map((s) => s.body.session_id);
      assert.ok(started.slice(0, 2).every((id) => /^researcher-[0-9a-f]{12}$/.test(id)), `researcher sessions are named researcher-...: ${started}`);
    } finally {
      runner.stop();
    }
  });

  it("takes the researcher's model and effort from its own settings", async () => {
    const own = await runnerFor("researcher2", ["researcher", null], {
      SCOUT_RESEARCHER_MODEL: "researcher-test", SCOUT_RESEARCHER_EFFORT: "medium",
    });
    try {
      const before = readCalls().length;
      await wake({ mode: "scout", reason: "search_started", project_id: "run-e2" }, { at: own.at });
      await waitFor(() => readCalls().length === before + 1, 15000);
      const call = readCalls()[before];
      assert.equal(arg(call, "--model"), "researcher-test");
      assert.equal(arg(call, "--effort"), "medium");
    } finally {
      own.stop();
    }
  });
});

describe("the mail reader (docs/PRD.md path 14)", () => {
  it("starts a mail session on a mail_asked wake: Haiku 5.5 at low effort, its two tools and no web at all", async () => {
    const before = readCalls().length;
    assert.equal((await wake({ mode: "scout", reason: "mail_asked", project_id: "mail-1" })).status, 202);
    await waitFor(() => readCalls().length === before + 1);
    const call = readCalls()[before];
    assert.equal(arg(call, "--model"), "claude-haiku-5-5", "the mail reader is a small Haiku 5.5 session");
    assert.equal(arg(call, "--effort"), "low");
    const system = systemOf(call);
    assert.match(system, /# Reading the owner's job mail/);
    assert.doesNotMatch(system, /You are the scout|# Researching an employer|# Researching a role/);
    assert.deepEqual(allowed(call), ["mcp__jobsearch__jobs_mail", "mcp__jobsearch__jobs_mail_report"],
      "nothing in a message is opened: no web tools, no shell and no files");
    assert.match(arg(call, "-p"), /wake mail_asked project mail-1/);
    assert.match(arg(call, "-p"), /jobs\.mail/);
  });
});

describe("Find Gaps", () => {
  it("starts a gaps session on a gaps_asked wake even when the app can't be asked: the main model, its three tools and the public web", async () => {
    const before = readCalls().length;
    assert.equal((await wake({ mode: "scout", reason: "gaps_asked", project_id: "gaps-1" })).status, 202);
    await waitFor(() => readCalls().length === before + 1);
    const call = readCalls()[before];
    assert.equal(arg(call, "--model"), "sonnet-test", "Find Gaps grades against the whole master: the main model");
    const system = systemOf(call);
    assert.match(system, /Finding the gaps between a kind of job and the master resume/);
    assert.doesNotMatch(system, /You are the scout|You judge titles|You split a posting/);
    assert.equal(arg(call, "--tools"), "WebSearch,WebFetch", "the public web, read-only: no shell and no files");
    assert.deepEqual(allowed(call), [
      "mcp__jobsearch__jobs_gaps", "mcp__jobsearch__jobs_gaps_progress", "mcp__jobsearch__jobs_gaps_report",
      "WebSearch", "WebFetch",
    ]);
    assert.match(arg(call, "-p"), /wake gaps_asked project gaps-1/);
    assert.match(arg(call, "-p"), /jobs\.gaps/);
  });

  it("starts gaps sessions while the app says gaps, each named gaps-...", async () => {
    const runner = await runnerFor("gaps", ["gaps", "gaps", "gaps", null]);
    try {
      const before = readCalls().length;
      await wake({ mode: "scout", reason: "search_started", project_id: "run-g" }, { at: runner.at });
      await waitFor(() => readCalls().length === before + 2, 15000);
      const [first, second] = readCalls().slice(before);
      for (const call of [first, second]) assert.match(systemOf(call), /Finding the gaps between/);
      assert.match(arg(second, "-p"), /more work: gaps/, "while Find Gaps work waits, the next session is a gaps session again");
      const started = runner.seen.filter((s) => s.body && s.body.state === "started").map((s) => s.body.session_id);
      assert.ok(started.slice(0, 2).every((id) => /^gaps-[0-9a-f]{12}$/.test(id)), `gaps sessions are named gaps-...: ${started}`);
    } finally {
      runner.stop();
    }
  });
});

describe("what the scout declares about itself", () => {
  it("states the machine, runtime, model, engine and that it takes one task at a time, once, on start", async () => {
    const r = await runnerFor("declare", [], { SCOUT_HOST: "test-host", SCOUT_ENGINE: "A test subscription" });
    try {
      await waitFor(() => r.seen.length > 0);
      const [call] = r.seen;
      assert.equal(call.method, "POST");
      assert.equal(call.url, "/api/v1/agent/capabilities");
      assert.equal(call.auth, "Bearer ptok");
      assert.equal(call.body.host, "test-host");
      assert.equal(call.body.runtime, "Claude Code");
      assert.equal(call.body.engine, "A test subscription");
      assert.equal(call.body.model_name, "sonnet-test");
      assert.equal(call.body.hosting, "hosted");
      assert.equal(call.body.concurrency, 1);
      assert.match(call.body.usage_limit, /usage limit/);
      assert.equal("context_window_tokens" in call.body, false, "no window is invented");
      await sleep(300);
      assert.equal(r.seen.length, 1, "declared once, not on every check-in");
    } finally {
      r.stop();
    }
  });

  it("is on Sonnet 5.5 by its id, not the alias, when SCOUT_MODEL is not set: declared, and passed to the main sessions", async () => {
    const r = await runnerFor("default-model", ["analysis", null], { SCOUT_MODEL: undefined });
    try {
      const before = readCalls().length;
      await waitFor(() => r.seen.length > 0);
      assert.equal(r.seen[0].url, "/api/v1/agent/capabilities");
      assert.equal(r.seen[0].body.model_name, "claude-sonnet-5-5");
      await wake({ mode: "scout", reason: "search_started", project_id: "run-dm" }, { at: r.at });
      await waitFor(() => readCalls().length === before + 1, 15000);
      assert.equal(arg(readCalls()[before], "--model"), "claude-sonnet-5-5", "an id the pinned Claude Code accepts, not the alias 'sonnet'");
    } finally {
      r.stop();
    }
  });
});

describe("a recipe to write", () => {
  it("starts a parser session on the main model when the app's answer names sonnet, and an ordinary one on the parser's Haiku", async () => {
    const r = await runnerFor("parser-sonnet", [{ next: "parser", model: "sonnet" }, { next: "parser" }, { next: "parser" }, null]);
    try {
      const before = readCalls().length;
      await wake({ mode: "scout", reason: "search_started", project_id: "run-ps" }, { at: r.at });
      await waitFor(() => readCalls().length >= before + 2, 15000);
      const [recipe, ordinary] = readCalls().slice(before);
      assert.equal(arg(recipe, "--model"), "sonnet-test", "the recipe's session runs on the main model");
      assert.equal(arg(ordinary, "--model"), "claude-haiku-5-5", "any other parse job keeps the parser's Haiku");
      assert.match(systemOf(recipe), /site's recorded traffic/, "the recipe mode's instructions are in its prompt");
    } finally {
      r.stop();
    }
  });
});

describe("the instructions a session starts with (docs/PRD.md 16.1)", () => {
  // A runner against a fake app whose instruction answer is `instructions` (or a refusal when null); one search waits.
  async function sessionWith(tag, instructions) {
    const reply = (url) => (url.startsWith("/api/v1/agent/instructions")
      ? { status: instructions ? 200 : 503, json: instructions ?? {} }
      : undefined);
    const r = await runnerFor(tag, ["search"], {}, { reply });
    try {
      const before = readCalls().length;
      await wake({ mode: "scout", reason: "search_started", project_id: "p-edit" }, { at: r.at });
      await waitFor(() => readCalls().length === before + 1, 12000);
      return { system: systemOf(readCalls()[before]), seen: r.seen.map((s) => s.url) };
    } finally {
      r.stop();
    }
  }

  it("sends the version the owner saved in the app, not the image's file", async () => {
    const { system, seen } = await sessionWith("edited", {
      files: { "jobsearch.scout.instructions.md": { text: "You are the owner's edited scout.", version: 3 } },
    });
    assert.ok(seen.some((u) => u.startsWith("/api/v1/agent/instructions?names=jobsearch.scout.instructions.md")));
    assert.equal(system, "You are the owner's edited scout.");
  });

  // The owner's stable material (the analyst's resume, scoring and response shape) goes at the end of the system prompt,
  // and the session names its digest, so the app's tools leave it out of their replies for this session alone.
  async function analystWith(tag, context) {
    const reply = (url) => (url.startsWith("/api/v1/agent/context")
      ? { status: context ? 200 : 503, json: context ?? {} }
      : undefined);
    const r = await runnerFor(tag, ["analysis"], {}, { reply });
    try {
      const before = readCalls().length;
      await wake({ mode: "scout", reason: "analysis_asked", project_id: "role-ctx" }, { at: r.at });
      await waitFor(() => readCalls().length === before + 1, 12000);
      const config = JSON.parse(readFileSync(r.mcpConfig, "utf8"));
      return { system: systemOf(readCalls()[before]), header: config.mcpServers.jobsearch.headers["X-Context"],
        seen: r.seen.map((s) => s.url) };
    } finally {
      r.stop();
    }
  }

  it("puts the owner's stable material at the end of the analyst's system prompt and names its digest", async () => {
    const { system, header, seen } = await analystWith("owner-context", {
      text: "# Owner context\n\n## master_resume\n\nThe resume.", digest: "abc123",
    });
    assert.ok(seen.some((u) => u.startsWith("/api/v1/agent/context?kind=analysis")));
    assert.match(system, /You are the analyst/);
    assert.ok(system.endsWith("# Owner context\n\n## master_resume\n\nThe resume."), "after the instructions, the cached prefix");
    assert.equal(header, "abc123");
  });

  it("names no digest when the app can't give the material, so its tools send it in their replies", async () => {
    const { system, header } = await analystWith("no-owner-context", null);
    assert.match(system, /You are the analyst/);
    assert.doesNotMatch(system, /Owner context/);
    assert.equal(header, undefined);
  });

  it("falls back to the image's file for a file the app has no text for, and when the app refuses", async () => {
    const missing = await sessionWith("missing", { files: {} });
    assert.match(missing.system, /You are the scout/);
    const refused = await sessionWith("refused", null);
    assert.match(refused.system, /You are the scout/);
    assert.doesNotMatch(refused.system, /edited scout/);
  });
});

// Instruction files have no limit on their size. The kernel refuses one
// command-line argument over 131,072 bytes (E2BIG), so the instructions reach Claude as a file, never as an argument.
describe("the instructions reach Claude as a file, never on the command line", () => {
  // The file the runner writes for a session lies beside its MCP config (`SYSTEM_FILE` in scout.mjs).
  const systemFileOf = (r) => r.mcpConfig.replace(/\.json$/, ".system.md");
  const stripped = (name) => readFileSync(join(dir, "instructions", name), "utf8").replace(/^\s*<!--[\s\S]*?-->\s*/, "").trim();

  it("starts a session with --append-system-prompt-file and no --append-system-prompt; the file holds the joined instructions", async () => {
    const before = readCalls().length;
    await wake({ mode: "scout", reason: "analysis_asked", project_id: "role-by-file" });
    await waitFor(() => readCalls().length === before + 1);
    const call = readCalls()[before];
    assert.ok(call.argv.includes("--append-system-prompt-file"), "the instructions are named by file");
    assert.ok(!call.argv.includes("--append-system-prompt"), "and never given on the command line");
    assert.equal(arg(call, "--append-system-prompt-file"), call.systemFile);
    assert.ok(!call.argv.some((a) => a.includes("You are the analyst")), "no argument holds the instructions themselves");
    assert.equal(
      call.system,
      [stripped("jobsearch.analyst.instructions.md"), stripped("jobsearch.scoring.instructions.md")].join("\n\n"),
      "the kind's own file, then the shared one, joined as they always were",
    );
  });

  it("writes the file for its owner alone, and removes it once the session ended", async () => {
    const r = await runnerFor("system-file-ended", ["analysis"]);
    try {
      const before = readCalls().length;
      await wake({ mode: "scout", reason: "analysis_asked", project_id: "role-ended" }, { at: r.at });
      await waitFor(() => readCalls().length === before + 1);
      const call = readCalls()[before];
      assert.equal(call.systemFile, systemFileOf(r), "the file lies beside the runner's MCP config");
      assertOnlyTheRunnerCanRead(call.systemAccess, call.systemFile, "the instructions file");
      assertOnlyTheRunnerCanRead(call.mcpAccess, call.mcpFile, "the MCP config, which holds the app's token");
      await waitFor(() => ended(r));
      assert.equal(existsSync(call.systemFile), false, "gone once the session ended");
    } finally {
      r.stop();
    }
  });

  it("replaces a file a killed runner left behind, and still writes it for its owner alone", async () => {
    const r = await runnerFor("system-file-left", ["analysis"]);
    try {
      // A file that already exists keeps the access it was made with, so the runner must not write over it.
      writeFileSync(systemFileOf(r), "Text left by a runner that was killed.");
      if (WINDOWS) execFileSync("icacls", [systemFileOf(r), "/grant", "*S-1-1-0:R"], { stdio: "ignore" }); // Everyone, read
      else chmodSync(systemFileOf(r), 0o644);
      const before = readCalls().length;
      await wake({ mode: "scout", reason: "analysis_asked", project_id: "role-left" }, { at: r.at });
      await waitFor(() => readCalls().length === before + 1);
      const call = readCalls()[before];
      assert.match(call.system, /You are the analyst/);
      assert.doesNotMatch(call.system, /left by a runner that was killed/);
      assertOnlyTheRunnerCanRead(call.systemAccess, call.systemFile, "the new file, though the old one was open to all");
      await waitFor(() => ended(r));
      assert.equal(existsSync(call.systemFile), false);
    } finally {
      r.stop();
    }
  });

  it("removes the file when the session failed, when it was stopped at the run limit, and when it could not start", async () => {
    // A session that exits with an error.
    const failed = await runnerFor("system-file-failed", ["search"], { SCOUT_CLAUDE_BIN: failing("claude-file-failed").bin });
    // A session stopped at the run limit.
    const stopped = await runnerFor("system-file-stopped", ["search"], {
      SCOUT_CLAUDE_BIN: sleepy("claude-file-stopped").bin, SCOUT_RUN_TIMEOUT_MIN: "0.02",
    });
    // A claude that is not there at all.
    const missing = await runnerFor("system-file-missing", ["search"], { SCOUT_CLAUDE_BIN: join(dir, "no-such-claude") });
    try {
      const before = readCalls().length;
      await wake(search, { at: failed.at });
      await wake(search, { at: stopped.at });
      await wake(search, { at: missing.at });
      await waitFor(() => readCalls().length === before + 2);
      for (const r of [failed, stopped, missing]) await waitFor(() => ended(r), 12000);
      assert.match(failed.log(), /run failed; it starts no chain/);
      assert.match(stopped.log(), /TIMED OUT/);
      assert.match(missing.log(), /run end .*exit=-1/);
      for (const r of [failed, stopped, missing]) assert.equal(existsSync(systemFileOf(r)), false, r.log());
      for (const call of readCalls().slice(before)) assert.match(call.system, /You are the scout/);
    } finally {
      failed.stop();
      stopped.stop();
      missing.stop();
    }
  });

  it("starts a session whose joined instructions are 300,000 bytes, far over a command line's one-argument limit", async () => {
    const big = `You are the owner's long scout. ${"One more sentence the owner wrote. ".repeat(8600)}`;
    assert.ok(Buffer.byteLength(big) > 300_000 && Buffer.byteLength(big) > 131_072);
    const reply = (url) => (url.startsWith("/api/v1/agent/instructions")
      ? { json: { files: { "jobsearch.scout.instructions.md": { text: big, version: 2 } } } }
      : undefined);
    const r = await runnerFor("system-file-big", ["search"], {}, { reply });
    try {
      const before = readCalls().length;
      await wake(search, { at: r.at });
      await waitFor(() => readCalls().length === before + 1, 12000);
      const call = readCalls()[before];
      assert.equal(call.system, big.trim(), "the whole text reached the session");
      assert.doesNotMatch(r.log(), /E2BIG|Argument list too long|run failed/);
    } finally {
      r.stop();
    }
  });
});

describe("a session that goes wrong", () => {
  it("is stopped at the run limit, and starts no chain of more sessions", async () => {
    const fake = sleepy("claude-limit");
    const r = await runnerFor("limit", () => "search", { SCOUT_CLAUDE_BIN: fake.bin, SCOUT_RUN_TIMEOUT_MIN: "0.02" });
    try {
      const before = readCalls().length;
      await wake(search, { at: r.at });
      await waitFor(() => readCalls().length === before + 1);
      assert.equal((await health(r.at)).running, true, "/health says a session is running");
      await waitFor(() => r.log().includes("run timed out after"));
      await sessionStopped(fake);
      await waitFor(() => ended(r));
      assert.match(r.log(), /TIMED OUT/, "the log says why it ended, not only exit=null");
      assert.match(r.log(), /starts no chain/);
      await waitFor(async () => (await health(r.at)).running === false);
      await sleep(300);
      assert.equal(readCalls().length, before + 1);
      assert.equal(r.works(), 1, "the app was not asked for more work after a failed session");
    } finally {
      r.stop();
    }
  });

  it("is killed when it ignores the request to stop (on Windows: killed with all it started, at once), so the runner is not stuck running", async () => {
    const fake = stubborn("claude-stubborn");
    const r = await runnerFor("stubborn", () => "search", { SCOUT_CLAUDE_BIN: fake.bin, SCOUT_RUN_TIMEOUT_MIN: "0.01" });
    try {
      const before = readCalls().length;
      await wake(search, { at: r.at });
      await waitFor(() => readCalls().length === before + 1);
      if (WINDOWS) {
        // No request to stop to ignore: the session and what it started are ended at once, so nothing is left to be killed later.
        await sessionStopped(fake);
        await waitFor(() => ended(r));
        assert.match(r.log(), /exit=1\b/);
        assert.doesNotMatch(r.log(), /did not stop on SIGTERM/);
      } else {
        await waitFor(() => existsSync(fake.term));
        assert.equal((await health(r.at)).running, true, "still running: it ignored SIGTERM");
        await waitFor(() => r.log().includes("did not stop on SIGTERM; killing it"), 15000);
        await waitFor(() => ended(r));
        assert.match(r.log(), /signal=SIGKILL/);
      }
      await waitFor(async () => (await health(r.at)).running === false);
    } finally {
      r.stop();
    }
  });

  for (const [what, make] of [["exits with an error", failing], ["prints something that is not a result", garbling]]) {
    it(`that ${what} is logged as failed and starts no chain of more sessions`, async () => {
      const fake = make(`claude-bad-${what.split(" ")[0]}`);
      const r = await runnerFor(`bad-${what.split(" ")[0]}`, () => "search", { SCOUT_CLAUDE_BIN: fake.bin });
      try {
        const before = readCalls().length;
        await wake(search, { at: r.at });
        await waitFor(() => ended(r));
        assert.match(r.log(), /run end .*(exit=1|out_len=)/);
        assert.match(r.log(), /run failed; it starts no chain/);
        await waitFor(async () => (await health(r.at)).running === false);
        await sleep(300);
        assert.equal(readCalls().length, before + 1);
        assert.equal(r.works(), 1);
      } finally {
        r.stop();
      }
    });
  }

  it("does not take the runner down when a session cannot start, and keeps taking wakes", async () => {
    // The scout's instruction file is not in the image (and the app can't give it): that run fails, the runner does not.
    mkdirSync(join(dir, "no-instructions"));
    const r = await runnerFor("noinstr", () => "search", { SCOUT_INSTRUCTIONS_DIR: join(dir, "no-instructions") });
    try {
      await wake(search, { at: r.at });
      await waitFor(() => r.log().includes("run failed:"));
      await waitFor(async () => (await health(r.at)).running === false);
      await wake({ ...search, project_id: "again" }, { at: r.at });
      await waitFor(() => r.log().split("run failed:").length === 3);
      assert.equal((await health(r.at)).ok, true, "still up after two failed runs");
    } finally {
      r.stop();
    }
  });

  it("goes on to the wake that came in during a run that found nothing waiting", async () => {
    // The first answer is slow and says nothing waits; a second wake lands in that time and must not be forgotten.
    const r = await runnerFor("drain", [null, "search", null], {}, { delayMs: 300 });
    try {
      const before = readCalls().length;
      await wake(search, { at: r.at });
      await wake({ ...search, project_id: "second" }, { at: r.at });
      await waitFor(() => readCalls().length === before + 1, 12000);
      assert.match(arg(readCalls()[before], "-p"), /follow-up: wake search_started project second/);
    } finally {
      r.stop();
    }
  });

  it("keeps the folded follow-up's reason short however many wakes come in during a run", async () => {
    const r = await runnerFor("fold", ["search", "search", null]);
    try {
      const before = readCalls().length;
      await wake(search, { at: r.at });
      await waitFor(() => r.log().includes("run start"));
      await Promise.all(Array.from({ length: 80 }, (_, i) => wake({ ...search, project_id: `a-project-id-of-some-length-${i}` }, { at: r.at })));
      await waitFor(() => readCalls().length === before + 2, 12000);
      const prompt = arg(readCalls()[before + 1], "-p");
      assert.match(prompt, /follow-up/);
      assert.ok(prompt.length < 1500, `the follow-up's prompt is ${prompt.length} characters`);
    } finally {
      r.stop();
    }
  });

  it("stops the session and exits when told to stop (SIGTERM)", async () => {
    const fake = sleepy("claude-signal");
    const r = await runnerFor("signal", () => "search", { SCOUT_CLAUDE_BIN: fake.bin });
    try {
      const before = readCalls().length;
      await wake(search, { at: r.at });
      await waitFor(() => readCalls().length === before + 1);
      const gone = new Promise((resolve) => r.child.once("exit", resolve));
      killTree(r.child); // how a lane is stopped: SIGTERM; on Windows the runner and all it started
      await Promise.race([gone, sleep(5000).then(() => assert.fail("the runner did not exit"))]);
      await sessionStopped(fake);
    } finally {
      r.stop();
    }
  });
});

// A session that ends at the Claude subscription's usage limit says so to the app, with the time it lifts, so the app can
// wait for it (a research that hits it picks up in the next window, not the next night). The words are Claude Code's own,
// read from the CLI this image pins; the fake `claude` prints them as the CLI does: a result with is_error, exit 1.
describe("a session that ends at Claude's usage limit", () => {
  // A time as the CLI words it (a time of day and the zone's name, or a date and time when it is more than a day off). The
  // zone named has no daylight time, so the words have one reading in every hour of the year: worded in the machine's zone,
  // a time in the hour that repeats when the clocks go back means two instants, and the runner rightly takes the first. Its
  // half-hour offset is no machine's own, so only reading the zone the words name gives the instant.
  const ZONE = "Asia/Kolkata";
  const wording = (at, withDate = false) => {
    const parts = { ...(withDate ? { month: "short", day: "numeric" } : {}), hour: "numeric", minute: "2-digit", hour12: true, timeZone: ZONE };
    const text = at.toLocaleString("en-US", parts).replace(":00", "");
    return `${text.replace(/\s([AP]M)/i, (_d, p) => p.toLowerCase())} (${ZONE})`;
  };
  const inMinutes = (n) => new Date(Math.ceil((Date.now() + n * 60_000) / 60_000) * 60_000);
  const sessionSaying = (name, result, over = {}) => fakeClaude(
    name, `console.log(${JSON.stringify(JSON.stringify({ type: "result", is_error: true, api_error_status: 429, num_turns: 1, result, ...over }))}); process.exit(1);`);

  async function endOf(tag, result, over) {
    const fake = sessionSaying(`claude-limit-${tag}`, result, over);
    const r = await runnerFor(`usage-${tag}`, () => "analysis", { SCOUT_CLAUDE_BIN: fake.bin });
    try {
      const before = readCalls().length;
      await wake({ mode: "scout", reason: "analysis_asked", project_id: "role-1" }, { at: r.at });
      await waitFor(() => ended(r));
      await sleep(300);
      assert.equal(readCalls().length, before + 1, "a session that failed starts no chain");
      assert.equal(r.works(), 1, "and the app is not asked for more work after it");
      return { ended: r.seen.find((s) => s.body?.state === "ended").body, log: r.log() };
    } finally {
      r.stop();
    }
  }

  it("tells the app the limit and the time it lifts, from a time of day", async () => {
    const at = inMinutes(150);
    const { ended: body, log } = await endOf("clock", `You've hit your session limit · resets ${wording(at)}`);
    assert.equal(body.limit.resets_at, at.toISOString());
    assert.match(body.limit.text, /hit your session limit/);
    assert.match(body.session_id, /^analyst-/);
    assert.match(log, /analyst run ended at Claude's usage limit; it lifts at /);
  });

  it("reads a date and a time when the limit lifts more than a day off (the weekly limit)", async () => {
    const at = inMinutes(3 * 24 * 60);
    const { ended: body } = await endOf("weekly", `You've hit your weekly limit · resets ${wording(at, true)}`);
    assert.equal(body.limit.resets_at, at.toISOString());
  });

  it("reads the older form, a time after a bar", async () => {
    const at = new Date(Math.floor(Date.now() / 1000) * 1000 + 3 * 3600_000);
    const { ended: body } = await endOf("epoch", `Claude AI usage limit reached|${Math.floor(at.getTime() / 1000)}`);
    assert.equal(body.limit.resets_at, at.toISOString());
  });

  it("says the limit with no time when the words give none, or a time a week and more away", async () => {
    assert.deepEqual((await endOf("none", "You've hit your weekly limit")).ended.limit.resets_at, null);
    const far = new Date(Date.now() + 20 * 24 * 3600_000);
    assert.equal((await endOf("far", `You've hit your weekly limit · resets ${wording(far, true)}`)).ended.limit.resets_at, null);
  });

  it("is not a usage limit when the service is only limiting requests for a moment, or the session failed for another reason", async () => {
    const moment = await endOf("moment", "Server is temporarily limiting requests (not your usage limit) · try again");
    assert.equal("limit" in moment.ended, false);
    assert.equal("limit" in (await endOf("other", "boom", { api_error_status: 500 })).ended, false);
  });

  it("says nothing of a limit for a session that finished, whatever its answer says", async () => {
    const fake = fakeClaude("claude-limit-finished", `console.log(${JSON.stringify(JSON.stringify({
      type: "result", is_error: false, num_turns: 3, result: "The posting says: you've hit your session limit" }))}); process.exit(0);`);
    const r = await runnerFor("usage-finished", ["analysis", null], { SCOUT_CLAUDE_BIN: fake.bin });
    try {
      await wake({ mode: "scout", reason: "analysis_asked", project_id: "role-1" }, { at: r.at });
      await waitFor(() => ended(r));
      assert.equal("limit" in r.seen.find((s) => s.body?.state === "ended").body, false);
      assert.doesNotMatch(r.log(), /usage limit/);
    } finally {
      r.stop();
    }
  });
});

describe("when the app can't be reached", () => {
  it("a scheduled check-in starts no session, but a wake that says a search started still does", async () => {
    const r = await runnerFor("checkin", () => "search", { RESUMEBOT_MCP_URL: "http://127.0.0.1:9/mcp", SCOUT_CHECK_IN_MIN: "0.01" });
    try {
      const before = readCalls().length;
      await waitFor(() => r.log().includes("nothing waiting (scheduled check-in)"));
      assert.match(r.log(), /the app can't be reached; no session for: scheduled check-in/);
      assert.doesNotMatch(r.log(), /run start/);
      assert.equal(readCalls().length, before);
      await wake(search, { at: r.at });
      await waitFor(() => readCalls().length === before + 1);
    } finally {
      r.stop();
    }
  });
});

describe("who may wake the runner", () => {
  const now = () => Math.floor(Date.now() / 1000);
  const ignored = JSON.stringify({ mode: "other", reason: "x", project_id: "p" });

  it("refuses a wake signed more than five minutes ago, and takes one signed a minute ago", async () => {
    assert.equal((await rawWake(port, { timestamp: now() - 400, body: ignored })).status, 401);
    assert.equal((await rawWake(port, { timestamp: now() + 400, body: ignored })).status, 401);
    assert.equal((await rawWake(port, { timestamp: now() - 60, body: ignored })).status, 202);
  });

  it("refuses a wake whose timestamp is not a number, even when the signature covers it", async () => {
    for (const timestamp of ["NaN", "soon", "0x"]) {
      assert.equal((await rawWake(port, { timestamp, body: ignored })).status, 401, timestamp);
    }
  });

  it("refuses a body over the size limit", async () => {
    const big = JSON.stringify({ mode: "scout", reason: "x".repeat(70 * 1024), project_id: "p" });
    assert.equal((await rawWake(port, { timestamp: now(), body: big })).status, 413);
    assert.equal((await health(port)).ok, true);
  });

  it("answers 401, and stays up, when the wake secret can't be read", async () => {
    mkdirSync(join(dir, "secrets-no-wake"));
    for (const name of ["claude-oauth-token", "agent-token"]) writeFileSync(join(dir, "secrets-no-wake", name), "x");
    const r = await runnerFor("nowake", [], { SCOUT_SECRETS_DIR: join(dir, "secrets-no-wake") });
    try {
      assert.equal((await wake(search, { at: r.at })).status, 401);
      assert.match(r.log(), /wake could not be checked/);
      assert.equal((await health(r.at)).ok, true);
    } finally {
      r.stop();
    }
  });
});

describe("the runner's settings", () => {
  it("listens on 8651 unless told otherwise: the port the app's wake address names", async () => {
    // The test must not need 8651 to be free: on a computer where the app is running its own runner holds it. So the port is held here (when
    // nobody does) and the runner, which then cannot listen, says which port it tried and stops.
    const held = createServer();
    const mine = await new Promise((resolve) => { held.once("error", () => resolve(false)); held.listen({ port: 8651, ...(WINDOWS ? { host: "127.0.0.1" } : {}) }, () => resolve(true)); }); // where the runner listens
    const r = await launch("defaultport", { SCOUT_PORT: undefined, RESUMEBOT_MCP_URL: "http://127.0.0.1:9/mcp" }, { listening: false });
    try {
      const code = await new Promise((resolve) => (r.child.exitCode !== null ? resolve(r.child.exitCode) : r.child.once("exit", resolve)));
      assert.equal(code, 1);
      assert.match(r.log(), /cannot listen on .*:8651: /);
      assert.doesNotMatch(r.log(), /listening on/);
    } finally {
      endRunner(r.child);
      if (mine) held.close();
    }
  });

  it("falls back to its defaults for a setting that is not a number, not to a timer of a millisecond", async () => {
    const r = await runnerFor("nan", [], {
      SCOUT_CHECK_IN_MIN: "soon", SCOUT_PRESENCE_SEC: "often", SCOUT_HEARTBEAT_SEC: "often", SCOUT_RUN_TIMEOUT_MIN: "long",
    });
    try {
      assert.match(r.log(), /check-in every 2 min/);
      await sleep(500);
      assert.deepEqual(r.seen.filter((s) => s.body?.state === "idle"), [], "no stream of idle pings");
      assert.equal(r.works(), 0, "no stream of check-ins asking the app for work");
    } finally {
      r.stop();
    }
  });
});

// A fake `claude` whose session ends as Claude Code 2.1.197 ends one that is out of usage: a result object with
// `is_error` true and Claude's words in `result`, exit 1.
const limited = (name, text = "You've hit your session limit · resets 3pm (America/Los_Angeles)") =>
  fakeClaude(name, `console.log(JSON.stringify({ type: "result", subtype: "success", is_error: true, api_error_status: 429, num_turns: 1, result: ${JSON.stringify(text)} })); process.exit(1);`);

describe("what a session is sent, per kind of session (the token efficiency plan spec, step 1)", () => {
  const configFolder = () => mkdtempSync(join(dir, "claude-folder-"));
  // A fake `claude` that notes what the settings and the environment it was started with said (the tools are named as Claude Code writes them).
  const noting = (name, folder, after = `console.log(JSON.stringify({ num_turns: 1, is_error: false, result: "done" })); process.exit(0);`) => {
    const noted = join(dir, `${name}.env.json`);
    const fake = fakeClaude(name, `
let settings = null;
try { settings = JSON.parse(fs.readFileSync(${JSON.stringify(join(folder, "settings.json"))}, "utf8")); } catch {}
fs.writeFileSync(${JSON.stringify(noted)}, JSON.stringify({ ttl: process.env.CLAUDE_CODE_PROMPT_CACHE_TTL || null,
  mark: process.env.SCOUT_END_MARK || null, settings }));
${after}`);
    return { ...fake, noted: () => JSON.parse(readFileSync(noted, "utf8")) };
  };
  const endedCount = (r) => r.seen.filter((s) => s.body?.state === "ended").length;

  it("keeps a lane's prompt cache for 5 minutes and puts the end-of-work hook in its Claude folder, keeping what was there", async () => {
    const folder = configFolder();
    writeFileSync(join(folder, "settings.json"), JSON.stringify({
      theme: "dark", hooks: { PostToolUse: [{ matcher: "Bash", hooks: [{ type: "command", command: "echo mine" }] }] },
    }));
    const fake = noting("claude-settings", folder);
    const r = await runnerFor("settings", ["analysis", null, "analysis"], { SCOUT_CLAUDE_BIN: fake.bin, SCOUT_CLAUDE_CONFIG_DIR: folder });
    try {
      await wake(search, { at: r.at });
      await waitFor(() => endedCount(r) >= 1);
      const { settings } = fake.noted();
      assert.equal(settings.promptCacheTtl, "5m");
      assert.equal(settings.theme, "dark", "what the folder held stays");
      const [mine, ours] = settings.hooks.PostToolUse;
      assert.equal(mine.hooks[0].command, "echo mine");
      assert.match(ours.matcher, /mcp__jobsearch__jobs_analysis_submit/);
      assert.doesNotMatch(ours.matcher, /jobs_packet/, "no packet tool ends a session any more");
      assert.doesNotMatch(ours.matcher, /jobs_read_next/, "no reader tool ends a session any more");
      assert.match(ours.matcher, /mcp__resumebot-browser__browser_next_item/);
      assert.match(ours.hooks[0].command, /end-session\.mjs"$/);
      assert.ok(!ours.hooks[0].command.includes("\\"), "forward slashes: Claude Code runs the command in a Unix shell");
      // Each session writes it again over the folder's own: one copy of the hook, never two.
      await wake(search, { at: r.at });
      await waitFor(() => endedCount(r) >= 2);
      const again = JSON.parse(readFileSync(join(folder, "settings.json"), "utf8"));
      assert.equal(again.hooks.PostToolUse.filter((e) => JSON.stringify(e).includes("end-session.mjs")).length, 1);
    } finally {
      r.stop();
    }
  });

  it("treats a session its hook ended as a finished one: no failure", async () => {
    const folder = configFolder();
    // What a session whose hook stopped it may leave: a failing exit and an error result. The hook's note says it was a finish.
    const fake = noting("claude-hook-ended", folder, `
fs.writeFileSync(process.env.SCOUT_END_MARK, "mcp__jobsearch__jobs_analysis_submit\\n");
console.log(JSON.stringify({ num_turns: 3, is_error: true, result: "" })); process.exit(1);`);
    const r = await runnerFor("hook-ended", ["analysis"], { SCOUT_CLAUDE_BIN: fake.bin, SCOUT_CLAUDE_CONFIG_DIR: folder });
    try {
      await wake(search, { at: r.at });
      await waitFor(() => endedCount(r) >= 1);
      assert.match(r.log(), /ended by the hook after mcp__jobsearch__jobs_analysis_submit/);
      assert.doesNotMatch(r.log(), /run failed/);
      assert.ok(!existsSync(fake.noted().mark), "the note is removed with the session");
    } finally {
      r.stop();
    }
  });

  it("a session that failed with no note from the hook is still a failure", async () => {
    const folder = configFolder();
    const fake = noting("claude-no-note", folder, `console.error("boom"); process.exit(1);`);
    const r = await runnerFor("no-note", ["analysis"], { SCOUT_CLAUDE_BIN: fake.bin, SCOUT_CLAUDE_CONFIG_DIR: folder });
    try {
      await wake(search, { at: r.at });
      await waitFor(() => /run failed/.test(r.log()));
    } finally {
      r.stop();
    }
  });

  it("names each kind's own tools to the app's door, which lists a session no others", async () => {
    for (const [work, tools] of [
      ["analysis", "jobs_analysis,jobs_analysis_submit"],
      ["titles", "jobs_titles_next,jobs_titles_report"],
    ]) {
      const r = await runnerFor(`x-tools-${work}`, [work], { SCOUT_CLAUDE_BIN: finishing() });
      try {
        await wake(search, { at: r.at });
        await waitFor(() => endedCount(r) >= 1);
        const config = JSON.parse(readFileSync(r.mcpConfig, "utf8"));
        assert.equal(config.mcpServers.jobsearch.headers["X-Tools"], tools, work);
      } finally {
        r.stop();
      }
    }
  });

  it("runs a seat as one turn: the app's system text and prompt, its model and effort, no tools, no MCP door, no cache", async () => {
    const r = await runnerFor("seat-turn", ["seat"], { SCOUT_CLAUDE_BIN: finishing(), SCOUT_OPUS_MODEL: "opus-test" }, { reply: seatReply });
    try {
      const before = readCalls().length;
      await wake(search, { at: r.at });
      await waitFor(() => endedCount(r) >= 1);
      const call = readCalls()[before];
      assert.equal(systemOf(call), "Seat system text.");
      assert.ok(call.argv.includes("--system-prompt-file"));
      assert.ok(!call.argv.includes("--append-system-prompt-file"), "the app's text is the whole system prompt");
      assert.equal(arg(call, "-p"), "Write the resume.");
      assert.equal(arg(call, "--model"), "opus-test");
      assert.equal(arg(call, "--effort"), "high");
      assert.equal(arg(call, "--tools"), "", "no tools at all");
      assert.ok(call.argv.includes("--strict-mcp-config"));
      assert.ok(!call.argv.includes("--mcp-config") && !call.argv.includes("--allowedTools"), "no door to the app");
      assert.equal(arg(call, "--output-format"), "json");
      assert.equal(call.cache, "1", "caching is off");
      assert.equal(call.mcpFile, null);
      assert.equal(existsSync(r.mcpConfig), false, "no MCP config was written for it");
    } finally {
      r.stop();
    }
  });

  it("runs a seat that names sonnet on the runner's own model, and one with no effort without --effort", async () => {
    const plain = { ...SEAT, id: "seat-2", model: "sonnet", effort: null };
    const r = await runnerFor("seat-sonnet", ["seat"], { SCOUT_CLAUDE_BIN: finishing() }, {
      reply: (url) => (url.endsWith("/seat/take") ? { json: { seat: plain } } : undefined),
    });
    try {
      const before = readCalls().length;
      await wake(search, { at: r.at });
      await waitFor(() => endedCount(r) >= 1);
      const call = readCalls()[before];
      assert.equal(arg(call, "--model"), "sonnet-test");
      assert.ok(!call.argv.includes("--effort"));
    } finally {
      r.stop();
    }
  });

  it("takes the seat after saying it started, answers it with Claude's text and its spend, and reports the writer", async () => {
    const fake = fakeClaude("claude-seat-answer", `console.log(JSON.stringify({ num_turns: 1, is_error: false, result: "The resume.", total_cost_usd: 0.5, usage: { input_tokens: 10, output_tokens: 20 } })); process.exit(0);`);
    const r = await runnerFor("seat-answer", ["seat", null], { SCOUT_CLAUDE_BIN: fake.bin }, { reply: seatReply });
    try {
      await wake(search, { at: r.at });
      await waitFor(() => ended(r));
      const paths = r.seen.filter((s) => s.body && !s.url.endsWith("/capabilities")).map((s) => `${s.body.state || ""}${s.url.replace(/^\/api\/v1\/agent\/heartbeat$/, "")}`);
      assert.deepEqual(paths.filter((p) => p !== "alive"), ["started", "/api/v1/agent/seat/take", "/api/v1/agent/seat/seat-1/answer", "ended"]);
      const sessionId = r.seen.find((s) => s.body?.state === "started").body.session_id;
      assert.match(sessionId, /^writer-[0-9a-f]{12}$/);
      const take = r.seen.find((s) => s.url.endsWith("/seat/take"));
      assert.equal(take.method, "POST");
      assert.equal(take.auth, "Bearer ptok");
      assert.deepEqual(take.body, { session: sessionId });
      const answer = r.seen.find((s) => s.url.endsWith("/seat/seat-1/answer"));
      assert.equal(answer.auth, "Bearer ptok");
      assert.deepEqual(answer.body, { session: sessionId, text: "The resume.", spend: { cost_usd: 0.5, input_tokens: 10, output_tokens: 20 } });
      const end = r.seen.find((s) => s.body?.state === "ended");
      assert.deepEqual(end.body.spend, { cost_usd: 0.5, input_tokens: 10, output_tokens: 20 }, "the end carries the spend, as any session's does");
    } finally {
      r.stop();
    }
  });

  it("answers a seat with the reason when Claude failed or printed no result, and chains no more sessions", async () => {
    for (const [name, fake, reason] of [
      ["error", fakeClaude("claude-seat-error", `console.log(JSON.stringify({ is_error: true, result: "Overloaded" })); process.exit(1);`), /Overloaded/],
      ["garbled", garbling("claude-seat-garbled"), /printed no result/],
    ]) {
      const r = await runnerFor(`seat-${name}`, ["seat", "seat"], { SCOUT_CLAUDE_BIN: fake.bin }, { reply: seatReply });
      try {
        await wake(search, { at: r.at });
        await waitFor(() => ended(r));
        const answer = r.seen.find((s) => s.url.endsWith("/seat/seat-1/answer"));
        assert.ok(answer, name);
        assert.match(answer.body.error, reason, name);
        assert.equal(typeof answer.body.text, "string");
        assert.match(r.log(), /run failed; it starts no chain/);
        assert.equal(r.works(), 1, `${name}: the app was not asked for more work`);
      } finally {
        r.stop();
      }
    }
  });

  it("ends quietly, still saying it ended, when no seat is waiting", async () => {
    const r = await runnerFor("seat-none", ["seat", "seat"], { SCOUT_CLAUDE_BIN: finishing() }, {
      reply: (url) => (url.endsWith("/seat/take") ? { json: { seat: null } } : undefined),
    });
    try {
      const before = readCalls().length;
      await wake(search, { at: r.at });
      await waitFor(() => ended(r));
      await waitFor(async () => (await health(r.at)).running === false);
      assert.equal(readCalls().length, before, "Claude was not started");
      assert.match(r.log(), /no seat is waiting/);
      assert.ok(!r.seen.some((s) => /\/answer$/.test(s.url)));
      assert.equal(r.works(), 1, "no chain: nothing came of the session");
    } finally {
      r.stop();
    }
  });

  it("does not fail the run when the seat's answer is refused", async () => {
    const r = await runnerFor("seat-refused", ["seat", null], { SCOUT_CLAUDE_BIN: finishing() }, {
      reply: (url) => (url.endsWith("/answer") ? { status: 409, json: {} } : seatReply(url)),
    });
    try {
      await wake(search, { at: r.at });
      await waitFor(() => ended(r));
      assert.match(r.log(), /the answer to seat seat-1 was refused: 409/);
      assert.doesNotMatch(r.log(), /run failed/);
    } finally {
      r.stop();
    }
  });
});

describe("Claude's usage limit", () => {
  it("is told to the app when a session ends on it, in Claude's words, and starts no chain of more sessions", async () => {
    const fake = limited("claude-limit-hit");
    const r = await runnerFor("usage", () => "search", { SCOUT_CLAUDE_BIN: fake.bin });
    try {
      const before = readCalls().length;
      await wake(search, { at: r.at });
      await waitFor(() => ended(r));
      const end = r.seen.find((s) => s.body?.state === "ended");
      assert.equal(end.body.usage_limit, "You've hit your session limit · resets 3pm (America/Los_Angeles)");
      assert.match(end.body.session_id, /^scout-/);
      assert.match(r.log(), /run stopped: Claude's usage limit/);
      assert.match(r.log(), /run failed; it starts no chain/);
      await waitFor(async () => (await health(r.at)).running === false);
      await sleep(300);
      assert.equal(readCalls().length, before + 1);
      assert.equal(r.works(), 1, "the app was not asked for more work");
    } finally {
      r.stop();
    }
  });

  for (const [what, make, expected] of [
    ["the weekly limit", (name) => limited(name, "You've hit your weekly limit · resets Mon 9am"), "You've hit your weekly limit · resets Mon 9am"],
    ["running out of extra usage", (name) => limited(name, "You're out of extra usage · add funds to continue"), "You're out of extra usage · add funds to continue"],
    ["an older version's words, on the error stream", (name) => fakeClaude(name, `console.error("Claude AI usage limit reached|1760346000"); process.exit(1);`), "usage limit reached|1760346000"],
  ]) {
    it(`is told for ${what}`, async () => {
      const fake = make(`claude-limit-${what.split(" ")[0]}`);
      const r = await runnerFor(`usage-${what.split(" ")[0]}`, () => "titles", { SCOUT_CLAUDE_BIN: fake.bin });
      try {
        await wake(search, { at: r.at });
        await waitFor(() => ended(r));
        assert.equal(r.seen.find((s) => s.body?.state === "ended").body.usage_limit, expected);
      } finally {
        r.stop();
      }
    });
  }

  for (const [what, make] of [
    ["fails for another reason", failing],
    ["is an error of another kind", (name) => limited(name, "API Error: 500 Internal server error")],
    ["finishes well and talks about a limit in its answer", (name) => fakeClaude(name, `console.log(JSON.stringify({ is_error: false, num_turns: 3, result: "The posting says: you've hit your quota; usage limit reached is in its text." })); process.exit(0);`)],
  ]) {
    it(`is not told for a session that ${what}`, async () => {
      const fake = make(`claude-nolimit-${what.split(" ").length}`);
      const r = await runnerFor(`nolimit-${what.split(" ").length}`, () => "search", { SCOUT_CLAUDE_BIN: fake.bin });
      try {
        await wake(search, { at: r.at });
        await waitFor(() => ended(r));
        const end = r.seen.find((s) => s.body?.state === "ended");
        assert.equal(end.body.usage_limit, undefined);
        assert.deepEqual(Object.keys(end.body).sort(), ["lane", "runner", "session_id", "state"]);
      } finally {
        r.stop();
      }
    });
  }
});

describe("a night that takes hundreds of sessions", () => {
  it("goes on to the next session at once while the app names work: no pause", async () => {
    // A session takes two answers (one when it starts, one for the chain after it): four sessions, then nothing waits.
    // A wake starts the first, and no check-in comes (a check-in landing mid-chain would be folded in as a follow-up,
    // and under load the chain takes longer than any interval short enough to start it).
    const r = await runnerFor("chain", (n) => (n < 7 ? "analysis" : null), { SCOUT_CLAUDE_BIN: finishing(), SCOUT_CHECK_IN_MIN: "20000" });
    try {
      const before = readCalls().length;
      assert.equal((await wake({ mode: "scout", reason: "analysis_asked", project_id: "role-1" }, { at: r.at })).status, 202);
      await waitFor(() => readCalls().length === before + 4, 15000);
      await waitFor(() => r.seen.filter((s) => s.body?.state === "ended").length === 4);
      const calls = readCalls().slice(before);
      assert.match(arg(calls[0], "-p"), /started by: wake analysis_asked project role-1/);
      for (const call of calls.slice(1)) assert.match(arg(call, "-p"), /started by: more work: analysis/);
      assert.doesNotMatch(r.log(), /sessions in a row/, "no pace to keep");
    } finally {
      r.stop();
    }
  });
});

// The runner is a session of the app's pool (its one lane, `any`): it takes every kind of work, asks the app for it with
// `?only=any`, looks on its check-in (every 2 minutes unless it is told otherwise), says it is up between sessions, names
// itself and its lane in every heartbeat, and is held at Claude's usage limit. Every runner of this file is one.
describe("the runner, a session of the pool", () => {
  // The runner looks for work on its check-in. Here it does so every 0.3 s (2 minutes unless it is told otherwise).
  const quick = { SCOUT_CHECK_IN_MIN: "0.005" };
  const asks = (r) => r.seen.filter(askedForWork);

  it("asks the app for the pool's work (?only=any), with the scout's token, and a runner named session-<n> says which", async () => {
    const r = await runnerFor("pool-ask", [], quick);
    const named = await runnerFor("pool-ask-named", [null], { SCOUT_RUNNER: "session-3" });
    try {
      await waitFor(() => r.works() >= 1);
      const [ask] = asks(r);
      assert.equal(ask.method, "GET");
      assert.equal(ask.url, "/api/v1/agent/work?only=any");
      assert.equal(ask.auth, "Bearer ptok");
      await wake(search, { at: named.at });
      await waitFor(() => named.works() >= 1);
      assert.equal(asks(named)[0].url, "/api/v1/agent/work?only=any&runner=session-3", "the app counts the session's own limit by its name");
    } finally {
      r.stop();
      named.stop();
    }
  });

  it("says there is nothing waiting once, not on every check-in, until there is work again", async () => {
    const r = await runnerFor("pool-quiet", [], quick);
    try {
      await waitFor(() => r.works() >= 4);
      assert.equal(r.log().split("nothing waiting (scheduled check-in)").length, 2, "said once in four check-ins");
    } finally {
      r.stop();
    }
  });

  it("starts an analysis session when the app says analysis: the analyst's own session, instructions, tools and prompt", async () => {
    // The app says analysis for the first ask (the check-in's) and nothing after it (the chain's, and the later check-ins).
    const r = await runnerFor("pool-run", (n) => (n === 0 ? "analysis" : null), { SCOUT_CHECK_IN_MIN: "0.01" });
    try {
      const before = readCalls().length;
      await waitFor(() => readCalls().length === before + 1, 12000);
      const call = readCalls()[before];
      assert.match(systemOf(call), /You are the analyst/);
      assert.deepEqual(allowed(call), ["mcp__jobsearch__jobs_analysis", "mcp__jobsearch__jobs_analysis_submit", "WebSearch", "WebFetch"]);
      assert.equal(
        arg(call, "-p"),
        "This run was started by: scheduled check-in. Call jobs.analysis for the role waiting for research, research it, and send what you found with jobs.analysis_submit. Then stop.",
        "the analyst's prompt: no role id",
      );
      await waitFor(() => ended(r));
      const session = r.seen.find((s) => s.body?.state === "started").body.session_id;
      assert.match(session, /^analyst-[0-9a-f]{12}$/);
      assert.equal(r.seen.find((s) => s.body?.state === "ended").body.session_id, session, "the heartbeats are the session's own");
      for (const state of ["started", "ended"]) {
        assert.equal(r.seen.find((s) => s.body?.state === state).body.lane, "any", `${state}: says which lane, so the app knows it is not the scout's own session`);
      }
    } finally {
      r.stop();
    }
  });

  it("takes a reply naming a kind it has no session for as nothing waiting, and says so once", async () => {
    const r = await runnerFor("pool-other", () => "dragons", quick);
    try {
      const before = readCalls().length;
      await waitFor(() => r.works() >= 3);
      assert.equal(readCalls().length, before, "no session started");
      assert.equal(r.log().split("the app named dragons work for the any lane").length - 1, 1, "said once, not at every check-in");
    } finally {
      r.stop();
    }
  });

  it("declares itself on start, and says it is up between sessions as the pool's, naming itself", async () => {
    const r = await runnerFor("pool-up", [], { SCOUT_PRESENCE_SEC: "0.1" });
    const idles = () => r.seen.filter((s) => s.body?.state === "idle");
    try {
      await waitFor(() => idles().length >= 2 && r.seen.some((s) => s.url === "/api/v1/agent/capabilities"));
      // The pool's word is the lane's (`lane`, so it is not taken for the scout's own), and it names itself (`runner`).
      for (const ping of idles()) assert.deepEqual(ping.body, { session_id: "runner-idle", state: "idle", lane: "any", runner: "any" });
    } finally {
      r.stop();
    }
  });

  it("names itself in every heartbeat of a session: SCOUT_RUNNER, so two runners are two runners", async () => {
    const r = await runnerFor("pool-named", (n) => (n === 0 ? "analysis" : null), {
      SCOUT_RUNNER: "analysis-2", SCOUT_CHECK_IN_MIN: "0.01", SCOUT_PRESENCE_SEC: "0.1",
    });
    try {
      await waitFor(() => ended(r));
      // A session's own words; the runner's idle word and its report of its start (a named runner says it once) are not.
      const own = r.seen.filter((s) => s.body?.session_id && !["runner-idle", "runner-start"].includes(s.body.session_id));
      assert.ok(own.length >= 2, "started and ended at least");
      for (const beat of own) assert.deepEqual([beat.body.lane, beat.body.runner], ["any", "analysis-2"], beat.body.state);
      for (const ping of r.seen.filter((s) => s.body?.state === "idle")) assert.equal(ping.body.runner, "analysis-2");
    } finally {
      r.stop();
    }
  });

  it("starts no chain after a session that failed: the next check-in asks again", async () => {
    const fake = failing("claude-pool-failing");
    const r = await runnerFor("pool-fail", () => "analysis", { SCOUT_CLAUDE_BIN: fake.bin, SCOUT_CHECK_IN_MIN: "0.02" });
    try {
      await waitFor(() => asks(r).length >= 2, 12000);
      assert.match(r.log(), /run failed; it starts no chain/);
      const [first, second] = asks(r);
      assert.ok(second.at - first.at >= 1000, `the second ask came with the next check-in, not at once: ${second.at - first.at}ms`);
    } finally {
      r.stop();
    }
  });

  // A session that ends at Claude's usage limit, which lifts a second or two on: the app is told, and the runner then asks
  // it for nothing until the limit lifts. The app names analysis for the first ask (the check-in's, every 0.3 s here) and
  // nothing after that.
  it("is held by Claude's usage limit: it asks for nothing until the limit lifts, then asks again on a check-in", async () => {
    // The older words of the limit: a time after a bar.
    const fake = fakeClaude("claude-pool-hold", `
const lifts = Math.floor(Date.now() / 1000) + 2;
console.log(JSON.stringify({ type: "result", is_error: true, api_error_status: 429, num_turns: 1, result: "Claude AI usage limit reached|" + lifts }));
process.exit(1);`);
    const r = await runnerFor("pool-hold", (n) => (n === 0 ? "analysis" : null), { ...quick, SCOUT_CLAUDE_BIN: fake.bin });
    try {
      await waitFor(() => ended(r));
      const started = r.seen.find((s) => s.body?.state === "started");
      const end = r.seen.find((s) => s.body?.state === "ended");
      const lifts = Date.parse(end.body.limit.resets_at);
      assert.ok(lifts > started.at, "the limit had not lifted when the session started");
      assert.match(end.body.usage_limit, /usage limit reached/, "the app is told");
      // Check-ins keep coming every 0.3 s; the first ask after the session's own is the one after the limit lifts.
      await waitFor(() => r.seen.filter(askedForWork).some((a) => a.at >= lifts), 10000);
      // Three clocks meet here: the fake Claude's (it named the instant), the runner's (it holds until then) and the app's (it
      // stamps each ask). Processes on one machine read the clock a few milliseconds apart (V8 keeps its own on Windows), so the
      // first ask after the hold lifts can be stamped just before `lifts`. A runner that was not held asks a check-in (0.3 s)
      // after it ended, a second or more before `lifts`; only an ask earlier than the slack by that much counts as one.
      assert.deepEqual(r.seen.filter(askedForWork).filter((a) => a.at > started.at && a.at < lifts - CLOCK_SLACK_MS), [], "no ask while the runner was held");
      assert.equal(r.log().split("usage limit: holding until").length - 1, 1, "said once");
      assert.ok(r.log().includes(`usage limit: holding until ${new Date(lifts).toISOString()}`));
    } finally {
      r.stop();
    }
  });

  it("holds the runner for half an hour when the limit's words give no time", async () => {
    const fake = fakeClaude("claude-pool-limit-notime", `console.log(JSON.stringify({ type: "result", is_error: true, api_error_status: 429, num_turns: 1, result: "You've hit your weekly limit" })); process.exit(1);`);
    const r = await runnerFor("pool-hold-notime", () => "analysis", { ...quick, SCOUT_CLAUDE_BIN: fake.bin });
    try {
      const before = readCalls().length;
      await waitFor(() => ended(r));
      const end = r.seen.find((s) => s.body?.state === "ended");
      assert.equal(end.body.limit.resets_at, null);
      const until = Date.parse(/usage limit: holding until (\S+)/.exec(r.log())[1]);
      assert.ok(Math.abs(until - (end.at + 30 * 60_000)) < 5000, `held for half an hour: until ${new Date(until).toISOString()}`);
      await sleep(700); // two check-ins and more
      assert.equal(r.works(), 1, "no ask while held");
      assert.equal(readCalls().length, before + 1);
    } finally {
      r.stop();
    }
  });

  it("takes a wake as it takes a check-in: it asks the app with its filter, starts nothing when nothing waits, and the session when the app names analysis", async () => {
    // The first wake finds nothing waiting; the second finds analysis (and the chain after its session, nothing).
    const r = await runnerFor("pool-wake", (n) => (n === 1 ? "analysis" : null));
    try {
      const before = readCalls().length;
      assert.equal((await wake(search, { at: r.at })).status, 202);
      await waitFor(() => r.works() === 1);
      assert.equal(asks(r)[0].url, "/api/v1/agent/work?only=any", "the wake makes it ask, with its filter");
      await waitFor(async () => (await health(r.at)).running === false);
      assert.match(r.log(), /nothing waiting \(wake search_started project p-search\)/);
      assert.equal(readCalls().length, before, "nothing waited for it: no session");
      assert.equal((await wake(search, { at: r.at, secret: "wrong" })).status, 401, "a bad signature is refused as ever");
      await sleep(200);
      assert.equal(r.works(), 1, "and asks nothing");
      assert.equal((await wake({ mode: "scout", reason: "analysis_asked", project_id: "role-1" }, { at: r.at })).status, 202);
      await waitFor(() => readCalls().length === before + 1, 12000);
      const call = readCalls()[before];
      assert.match(systemOf(call), /You are the analyst/);
      assert.match(arg(call, "-p"), /wake analysis_asked project role-1/);
      assert.ok(asks(r).every((a) => a.url === "/api/v1/agent/work?only=any"), "every ask carries its filter");
      assert.doesNotMatch(r.log(), /wake ignored/);
    } finally {
      r.stop();
    }
  });

  it("says its lane on /health and in its first log line, and checks in every 2 minutes unless told otherwise", async () => {
    const r = await runnerFor("pool-health", [], { SCOUT_CHECK_IN_MIN: undefined });
    const own = await runnerFor("pool-health-own", [], { SCOUT_CHECK_IN_MIN: "5" });
    try {
      assert.deepEqual(await health(r.at), { ok: true, running: false, lane: "any" });
      assert.match(r.log(), /scout \(lane any\) listening on :\d+, check-in every 2 min/, "every 2 minutes, the length of a claim lease: a lost command never strands work longer");
      assert.match(own.log(), /check-in every 5 min/);
      assert.equal((await health(port)).lane, "any", "the shared runner of this file too");
      assert.match(mainRun.log(), /scout \(lane any\) listening on :\d+, check-in every 20000 min/);
    } finally {
      r.stop();
      own.stop();
    }
  });
});

// A runner that names itself (SCOUT_RUNNER, a container of its own: `analysis-1`): the app takes back the role a killed
// session was researching as soon as it knows, so every call a session makes says whose it is, and the runner says once
// that it has started (fix/lanes-stalls).
describe("a runner that names itself", () => {
  const named = { SCOUT_RUNNER: "analysis-1", SCOUT_CHECK_IN_MIN: "0.01" };
  const starts = (r) => r.seen.filter((s) => s.body?.state === "boot");

  it("reports its start once, and names itself and the session in the calls the session makes", async () => {
    const r = await runnerFor("named", (n) => (n === 0 ? "analysis" : null), { ...named, SCOUT_CLAUDE_BIN: finishing() });
    try {
      // Until the session has ended, not only started: the runner reports "started" and only then writes the session's MCP
      // config, so a read that followed the report at once could find the file missing or half written.
      await waitFor(() => starts(r).length === 1 && ended(r));
      const [boot] = starts(r);
      assert.equal(boot.method, "POST");
      assert.equal(boot.url, "/api/v1/agent/heartbeat");
      assert.equal(boot.auth, "Bearer ptok");
      assert.equal(boot.body.runner, "analysis-1");
      assert.match(boot.body.boot, /^[0-9a-f]{8}$/, "a number new on every start, so a session of this start is never taken for an earlier one's");
      assert.equal(boot.body.browser_mode, "profile", "which Chrome the browser jobs use: the app's own profile unless the install says own");
      const session = r.seen.find((s) => s.body?.state === "started").body.session_id;
      const headers = JSON.parse(readFileSync(r.mcpConfig, "utf8")).mcpServers.jobsearch.headers;
      assert.equal(headers["X-Session-Id"], session, "the id the heartbeats carry");
      assert.equal(headers["X-Runner"], `analysis-1/${boot.body.boot}`);
      assert.equal(starts(r).length, 1, "said once");
    } finally {
      r.stop();
    }
  });

  it("asks again, a few times, when the app is not up yet", async () => {
    let refused = 0;
    const reply = (_url, body) => (body?.state === "boot" && refused++ < 2 ? { status: 503, json: {} } : undefined);
    const r = await runnerFor("named-retry", [], { ...named, SCOUT_START_REPORT_WAIT_SEC: "0.1" }, { reply });
    try {
      await waitFor(() => starts(r).length === 3);
      await sleep(400);
      assert.equal(starts(r).length, 3, "refused twice, then taken: no more");
      assert.match(r.log(), /start report refused: 503/);
    } finally {
      r.stop();
    }
  });

  it("sends neither a start report nor a runner header when it has no name", async () => {
    const r = await runnerFor("unnamed", (n) => (n === 0 ? "analysis" : null),
      { SCOUT_CHECK_IN_MIN: "0.01", SCOUT_CLAUDE_BIN: finishing() });
    try {
      await waitFor(() => r.seen.some((s) => s.body?.state === "ended"));
      assert.equal(starts(r).length, 0);
      const headers = JSON.parse(readFileSync(r.mcpConfig, "utf8")).mcpServers.jobsearch.headers;
      assert.equal(headers["X-Runner"], undefined);
      assert.match(headers["X-Session-Id"], /^analyst-[0-9a-f]{12}$/);
    } finally {
      r.stop();
    }
  });
});

// A hand-out that carries a claim (docs/PRD.md 47, step 1): the app claimed the exact item for this runner with its answer,
// so the session started for it names the claim in its door's headers and in its start report, and its first prompt names
// the item. An answer with no claim (an older app) starts the session as before.
describe("a session started for a claimed item", () => {
  const lane = { SCOUT_CHECK_IN_MIN: "0.01" };

  it("names the claim to its door and in its start report, and the item in its first prompt", async () => {
    const claimed = { next: "analysis", claim: "0d7a1e2c-1111-4222-8333-444455556666", item: { id: "role-1", ids: ["role-1"] } };
    const r = await runnerFor("claimed", (n) => (n === 0 ? claimed : null), lane);
    try {
      await waitFor(() => r.seen.some((s) => s.body?.state === "ended"));
      const started = r.seen.find((s) => s.body?.state === "started");
      assert.equal(started.body.claim, claimed.claim, "the start report names the claim");
      const headers = JSON.parse(readFileSync(r.mcpConfig, "utf8")).mcpServers.jobsearch.headers;
      assert.equal(headers["X-Claim"], claimed.claim, "every call the session makes names it, so its take-next gets that item");
      assert.ok(readCalls().some((c) => /claimed this for you: role-1/.test(arg(c, "-p"))), "the first prompt names the item");
    } finally {
      r.stop();
    }
  });

  it("starts as before when the answer carries no claim", async () => {
    const r = await runnerFor("unclaimed", (n) => (n === 0 ? "analysis" : null), lane);
    try {
      await waitFor(() => r.seen.some((s) => s.body?.state === "ended"));
      assert.equal(r.seen.find((s) => s.body?.state === "started").body.claim, undefined);
      const headers = JSON.parse(readFileSync(r.mcpConfig, "utf8")).mcpServers.jobsearch.headers;
      assert.equal(headers["X-Claim"], undefined);
    } finally {
      r.stop();
    }
  });
});

// The dispatcher's command (docs/PRD.md 47.1 to 47.3): a signed wake carrying the claim the app made for this runner. The
// runner starts that session without asking the app which work waits; a runner that cannot (a session is going) says 409, so
// the app gives the claim back.
describe("a command from the app's dispatcher", () => {
  const lane = { SCOUT_CHECK_IN_MIN: "20000", SCOUT_RUNNER: "session-1" };
  const command = { mode: "scout", reason: "dispatch", project_id: "0d7a1e2c-1111-4222-8333-444455556666",
    claim: "0d7a1e2c-1111-4222-8333-444455556666", kind: "analysis", item: { id: "role-1", ids: ["role-1"] } };

  it("starts the claimed session without asking the app for work", async () => {
    const r = await runnerFor("command", () => null, lane);
    try {
      assert.equal((await wake(command, { at: r.at })).status, 202);
      await waitFor(() => ended(r));
      const started = r.seen.find((s) => s.body?.state === "started");
      assert.equal(started.body.claim, command.claim, "its start report names the claim");
      assert.equal(JSON.parse(readFileSync(r.mcpConfig, "utf8")).mcpServers.jobsearch.headers["X-Claim"], command.claim);
      assert.ok(readCalls().some((c) => /claimed this for you: role-1/.test(arg(c, "-p"))), "its first prompt names the item");
      const firstAsk = r.seen.findIndex(askedForWork);
      assert.ok(firstAsk === -1 || firstAsk > r.seen.indexOf(started), "no ask for work came before the session started");
    } finally {
      r.stop();
    }
  });

  it("answers 409 while a session is going, so the app takes the claim back, and the session goes on", async () => {
    const r = await runnerFor("command-busy", () => null, lane);
    try {
      assert.equal((await wake(command, { at: r.at })).status, 202);
      await waitFor(() => r.seen.some((s) => s.body?.state === "started"));
      const busy = await wake({ ...command, claim: "11111111-1111-4222-8333-444455556666" }, { at: r.at });
      assert.equal(busy.status, 409);
      assert.deepEqual(await busy.json(), { accepted: false, busy: true });
      await waitFor(() => ended(r));
    } finally {
      r.stop();
    }
  });

  it("refuses one with a wrong signature, as any wake", async () => {
    const r = await runnerFor("command-signed", () => null, lane);
    try {
      assert.equal((await wake(command, { at: r.at, secret: "wrong" })).status, 401);
    } finally {
      r.stop();
    }
  });
});

// The kinds of work the pool takes: every one the runner has a session for. The table is the contract written out again
// here, not read from the runner.
// The name a session of each kind gives itself (its id starts with it).
const SESSION_NAMED = {
  parser: "parser", checker: "checker", gaps: "gaps", search: "scout", titles: "titles",
  analysis: "analyst", researcher: "researcher", coach: "coach", seat: "writer", site: "site",
};
const ALL_KINDS = Object.keys(SESSION_NAMED);
// The ids of the sessions a runner has started, as the app was told.
const startedBy = (r) => r.seen.filter((s) => s.body?.state === "started").map((s) => s.body.session_id);

describe("the pool's kinds of work", () => {
  it("starts a session for each kind of work the app names, and none for a kind it has no session for, whatever the app names", async () => {
    // The app names one kind of work at a time, once: the chain's ask after a session, and the next wake's, find the next.
    const app = { naming: null };
    const r = await runnerFor("kinds", () => { const kind = app.naming; app.naming = null; return kind; }, { SCOUT_CLAUDE_BIN: finishing() }, { reply: seatReply });
    try {
      for (const kind of [...ALL_KINDS, "dragons"]) {
        const asked = r.works();
        const sessions = startedBy(r).length;
        app.naming = kind;
        assert.equal((await wake(search, { at: r.at })).status, 202);
        await waitFor(() => r.works() > asked);
        await waitFor(async () => (await health(r.at)).running === false);
        const started = startedBy(r).slice(sessions);
        if (kind === "dragons") {
          assert.deepEqual(started, [], `${kind}: no session`);
        } else {
          assert.equal(started.length, 1, `${kind}: a session starts`);
          assert.match(started[0], new RegExp(`^${SESSION_NAMED[kind]}-[0-9a-f]{12}$`), `${kind}: and it is that kind's`);
          // After a session that went well comes the chain's ask, which finds nothing: wait for it, so that it does not
          // take the next kind.
          await waitFor(() => r.works() === asked + 2);
        }
      }
      assert.equal(r.log().split("the app named ").length - 1, 1, "a kind it has no session for is said once, not each time");
    } finally {
      r.stop();
    }
  });

  it("goes by a wake's reason, with the app out of reach, for each kind of work", async () => {
    const named = {
      parse_asked: "parser", check_asked: "checker", gaps_asked: "gaps", packet_asked: "seat", analysis_asked: "analysis",
      employers_asked: "researcher", coach_asked: "coach", site_project: "site", tag_roles: "titles",
      search_started: "search",
    };
    const r = await runnerFor("down", [], { SCOUT_CLAUDE_BIN: finishing(), RESUMEBOT_MCP_URL: "http://127.0.0.1:9/mcp" });
    // What a wake of this reason is called in the log, for the project it names.
    const said = (reason) => `wake ${reason} project down-${reason}`;
    try {
      for (const reason of Object.keys(named)) {
        assert.equal((await wake({ mode: "scout", reason, project_id: `down-${reason}` }, { at: r.at })).status, 202);
        await waitFor(async () => (await health(r.at)).running === false && r.log().includes(`run start (${said(reason)})`));
      }
      for (const [reason, kind] of Object.entries(named)) {
        assert.match(r.log(), new RegExp(`${SESSION_NAMED[kind]} run start \\(${said(reason)}\\)`), `${reason}: starts a ${kind} session`);
      }
    } finally {
      r.stop();
    }
  });

  it("names the pool's lane in every session heartbeat", async () => {
    // A session of half a second, with a heartbeat every tenth of one: started, alive, ended.
    const fake = fakeClaude("claude-lane-field", `setTimeout(() => { console.log(JSON.stringify({ num_turns: 1, is_error: false, result: "done" })); process.exit(0); }, 450);`);
    const r = await runnerFor("field", ["search", null], { SCOUT_CLAUDE_BIN: fake.bin, SCOUT_HEARTBEAT_SEC: "0.1" });
    try {
      assert.equal((await wake(search, { at: r.at })).status, 202);
      await waitFor(() => ended(r));
      const beats = r.seen.filter((s) => s.body?.session_id && s.body.session_id !== "runner-idle");
      const states = new Set(beats.map((s) => s.body.state));
      assert.ok(["started", "alive", "ended"].every((state) => states.has(state)), `every state was reported: ${[...states]}`);
      for (const { body } of beats) assert.equal(body.lane, "any", `${body.state} names the pool's lane`);
    } finally {
      r.stop();
    }
  });
});

// What a runner calls itself in its heartbeats (`runner`), which the app tells runners apart by, and the `lane` that says
// its word is the lane's: the idle ping carries both, so the Agents page can list every runner that is up.
describe("a runner naming itself to the app", () => {
  const idles = (r) => r.seen.filter((s) => s.body?.state === "idle");

  for (const [i, [env, expected]] of [
    [{}, { lane: "any", runner: "any" }],
    [{ SCOUT_RUNNER: " scout-a " }, { lane: "any", runner: "scout-a" }],
    [{ SCOUT_RUNNER: "session-2" }, { lane: "any", runner: "session-2" }],
  ].entries()) {
    it(`${env.SCOUT_RUNNER ? `SCOUT_RUNNER=${JSON.stringify(env.SCOUT_RUNNER)}` : "no SCOUT_RUNNER"} says ${JSON.stringify(expected)} when it is up`, async () => {
      const r = await runnerFor(`named-${i}`, [], { SCOUT_PRESENCE_SEC: "0.1", ...env });
      try {
        await waitFor(() => idles(r).length >= 1);
        assert.deepEqual(idles(r)[0].body, { session_id: "runner-idle", state: "idle", ...expected });
      } finally {
        r.stop();
      }
    });
  }
});

describe("the browser's sessions in the pool", () => {
  // The browser worker and the Pathfinder (the owner, 2026-10-15): the app's browser door and the DevTools MCP server, both
  // tools only, the install's profile and server version, and no turn cap. They work the app's queue one
  // item at a time (the browser work spec).
  const browserCalls = (t0) => readCalls().filter((c) => c.start >= t0 && allowed(c).includes("mcp__resumebot-browser__browser_next_item"));
  // The launcher's bridge to the one shared browser (the owner, 2026-10-16), standing in: its DevTools address answers.
  let devtools, devtoolsPort;
  const browserEnv = () => ({
    SCOUT_CLAUDE_BIN: finishing(), SCOUT_BROWSER_MCP: "chrome-devtools-mcp@9.9.9",
    SCOUT_BROWSER_PORT: String(devtoolsPort),
  });
  before(async () => {
    writeFileSync(join(dir, "secrets", "browser-token"), "btok");
    writeFileSync(join(dir, "secrets", "bridge-key"), BRIDGE_KEY);
    devtoolsPort = await freePort();
    // The bridge's stand-in refuses a request without its key, as the real one does (docs/PRD.md path 52).
    devtools = createHttpServer((req, res) => res.writeHead(req.headers["x-bridge-key"] !== BRIDGE_KEY ? 403 : req.url === "/json/version" ? 200 : 404).end("{}"));
    await new Promise((resolve) => devtools.listen(devtoolsPort, "127.0.0.1", resolve));
  });
  after(() => devtools?.close());

  it("names the claim to the browser door too, so the item claimed for the session is the one it is handed first", async () => {
    const claimed = { next: "browser_reader", claim: "0d7a1e2c-1111-4222-8333-444455556666", item: { id: "item-1", ids: ["item-1"] } };
    const r = await runnerFor("browser-claim", (n) => (n === 0 ? claimed : null), browserEnv());
    try {
      assert.equal((await wake(search, { at: r.at })).status, 202);
      await waitFor(() => ended(r));
      const door = JSON.parse(readFileSync(r.mcpConfig, "utf8")).mcpServers["resumebot-browser"];
      assert.equal(door.headers["X-Claim"], claimed.claim);
      assert.match(door.headers["X-Session-Id"], /^browser/);
    } finally {
      r.stop();
    }
  });

  it("runs a browser read with the browser door and the DevTools server alone, attached to the shared browser", async () => {
    const t0 = Date.now();
    const r = await runnerFor("browser-read", ["browser_reader"], browserEnv());
    try {
      assert.equal((await wake(search, { at: r.at })).status, 202);
      await waitFor(() => browserCalls(t0).length === 1 && ended(r));
      const call = browserCalls(t0)[0];
      assert.equal(arg(call, "--tools"), "", "no web tools, no shell, no files");
      assert.deepEqual(allowed(call), [
        "mcp__resumebot-browser__browser_next_item", "mcp__resumebot-browser__browser_hand_back",
        "mcp__resumebot-browser__browser_heartbeat", "mcp__resumebot-browser__browser_page_digest",
        "mcp__resumebot-browser__browser_postings", "mcp__chrome-devtools__*",
      ], "the item loop's tools: no plan, no role lists, no posting cap");
      assert.equal(arg(call, "--model"), "sonnet-test", "Sonnet, as the main model: browser work is judgement on unfamiliar pages");
      assert.ok(!call.argv.includes("--max-turns"), "no turn cap: the app boxes each item's time instead");
      assert.ok(call.argv.includes("--strict-mcp-config"));
      const prompt = arg(call, "-p");
      for (const words of ["browser.next_item", "one at a time", "browser.hand_back", "until it says stop", "browser.heartbeat", "browser.page_digest",
        "never judge whether a posting fits", "Never apply", "close only them",
        "navigate your one page", "never open a new page per posting", "close any extra tab as soon as you're done with it",
        "a cookie notice is declined with its Reject or Decline option, never a block"]) {
        assert.ok(prompt.includes(words), words);
      }
      const { mcpServers } = JSON.parse(readFileSync(r.mcpConfig, "utf8"));
      assert.deepEqual(Object.keys(mcpServers), ["resumebot-browser", "chrome-devtools"]);
      const door = mcpServers["resumebot-browser"];
      assert.match(door.url, /^http:\/\/127\.0\.0\.1:\d+\/mcp-browser\/$/, "the app's browser door");
      assert.equal(door.headers.Authorization, "Bearer btok", "the browser door's own token, never the scout's");
      const server = mcpServers["chrome-devtools"];
      assert.equal(server.command, WINDOWS ? "cmd" : "npx");
      if (WINDOWS) assert.deepEqual(server.args.slice(0, 2), ["/c", "npx"]);
      const args = WINDOWS ? server.args.slice(2) : server.args;
      assert.deepEqual(args.slice(0, 4), ["-y", "chrome-devtools-mcp@9.9.9", "--browserUrl", `http://127.0.0.1:${devtoolsPort}`]);
      assert.ok(!args.includes("--headless") && !args.includes("--userDataDir"), "it attaches to the one browser, never starts its own");
      assert.ok(args.includes("--usageStatistics=false"));
      for (const flag of ["--no-category-performance", "--no-category-emulation", "--no-category-network", "--no-category-memory"]) {
        assert.ok(args.includes(flag), `${flag}: the tools no instruction names are not sent`);
      }
      const started = r.seen.find((s) => s.body?.state === "started");
      assert.match(started.body.session_id, /^browser_reader-[0-9a-f]{12}$/, "the app knows the kind by the session's id");
      assert.equal(door.headers["X-Session-Id"], started.body.session_id, "the door keeps each session's run apart by it");
    } finally {
      r.stop();
    }
  });

  it("through the launcher's bridge a browser session connects to an address that names it, so the bridge knows whose each tab is (docs/PRD.md 52.2)", async () => {
    const t0 = Date.now();
    const r = await runnerFor("browser-bridge", ["browser_reader"], { ...browserEnv(), SCOUT_BROWSER_BRIDGE: "1" });
    try {
      assert.equal((await wake(search, { at: r.at })).status, 202);
      await waitFor(() => browserCalls(t0).length === 1 && ended(r));
      const { mcpServers } = JSON.parse(readFileSync(r.mcpConfig, "utf8"));
      const server = mcpServers["chrome-devtools"];
      const args = WINDOWS ? server.args.slice(2) : server.args;
      const started = r.seen.find((s) => s.body?.state === "started");
      assert.deepEqual(args.slice(0, 4), ["-y", "chrome-devtools-mcp@9.9.9", "--wsEndpoint",
        `ws://127.0.0.1:${devtoolsPort}/devtools/browser/${started.body.session_id}?key=${BRIDGE_KEY}`], "the address carries the bridge's key");
      assert.ok(!args.includes("--browserUrl"));
    } finally {
      r.stop();
    }
  });

  it("runs the Pathfinder on the main model with the learning tools", async () => {
    const t0 = Date.now();
    const r = await runnerFor("pathfinder", ["pathfinder"], browserEnv());
    try {
      assert.equal((await wake(search, { at: r.at })).status, 202);
      await waitFor(() => browserCalls(t0).length === 1 && ended(r));
      const call = browserCalls(t0)[0];
      assert.equal(arg(call, "--model"), "sonnet-test");
      assert.ok(allowed(call).includes("mcp__resumebot-browser__browser_skill"));
      assert.ok(allowed(call).includes("mcp__resumebot-browser__browser_hand_back"));
      assert.ok(allowed(call).includes("mcp__resumebot-browser__browser_page_digest"));
      assert.ok(!call.argv.includes("--max-turns"), "no turn cap: a map takes as long as a site needs");
      assert.match(arg(call, "-p"), /one site at a time/);
    } finally {
      r.stop();
    }
  });

  it("starts no browser session when the browser bridge is not running, and tells the app so", async () => {
    const t0 = Date.now();
    const r = await runnerFor("browser-none", ["browser_reader"], { ...browserEnv(), SCOUT_BROWSER: "profile", SCOUT_BROWSER_PORT: String(await freePort()) });
    try {
      assert.equal((await wake(search, { at: r.at })).status, 202);
      await waitFor(() => /the browser bridge is not running/.test(r.log()));
      assert.equal(browserCalls(t0).length, 0, "no session without the browser");
      await waitFor(() => r.seen.some((s) => s.body?.state === "ended"));
      assert.match(r.seen.find((s) => s.body?.state === "ended").body.browser, /the browser bridge is not running/);
    } finally {
      r.stop();
    }
  });

  // The owner's own Chrome (SCOUT_BROWSER=own, a setting; the app's own profile is the default): the runner starts no Chrome, attaches
  // through the launcher's bridge, and when the bridge says the Chrome cannot be had, starts no session and tells the app why.
  it("in the owner's own Chrome, says so in the prompt and tells the app the browser was had", async () => {
    const t0 = Date.now();
    const r = await runnerFor("browser-own", ["browser_reader"], { ...browserEnv(), SCOUT_BROWSER: "own" });
    try {
      assert.equal((await wake(search, { at: r.at })).status, 202);
      await waitFor(() => browserCalls(t0).length === 1 && ended(r));
      const prompt = arg(browserCalls(t0)[0], "-p");
      for (const words of ["the owner's own Chrome", "open your own tab", "touch nothing else",
        "a cookie notice is declined with its Reject or Decline option, never a block"]) assert.ok(prompt.includes(words), words);
      await waitFor(() => r.seen.some((s) => s.body?.state === "ended"));
      assert.equal(r.seen.find((s) => s.body?.state === "ended").body.browser, "", "the browser was had");
    } finally {
      r.stop();
    }
  });

  it("starts no browser session when the owner's Chrome is closed, and tells the app the one line Wake up says", async () => {
    const t0 = Date.now();
    const port = await freePort();
    const bridge = createHttpServer((req, res) => res.writeHead(req.url === "/json/version" ? 503 : 404, { "Content-Type": "application/json" })
      .end(JSON.stringify({ why: "Open Chrome to let the browser jobs run" })));
    await new Promise((resolve) => bridge.listen(port, "127.0.0.1", resolve));
    const r = await runnerFor("browser-own-closed", ["browser_reader"], { ...browserEnv(), SCOUT_BROWSER: "own", SCOUT_BROWSER_PORT: String(port) });
    try {
      assert.equal((await wake(search, { at: r.at })).status, 202);
      await waitFor(() => r.seen.some((s) => s.body?.state === "ended"));
      assert.equal(browserCalls(t0).length, 0, "no session without the owner's Chrome");
      assert.equal(r.seen.find((s) => s.body?.state === "ended").body.browser, "Open Chrome to let the browser jobs run");
      assert.match(r.log(), /Open Chrome to let the browser jobs run/);
    } finally {
      r.stop();
      bridge.close();
    }
  });

  it("starts the pinned DevTools server when the install names none, and no wall clock stops a browser session", async () => {
    const t0 = Date.now();
    const slow = fakeClaude("claude-browser-slow", `setTimeout(() => { console.log(JSON.stringify({ num_turns: 1, is_error: false, result: "done" })); process.exit(0); }, 1500);`);
    const r = await runnerFor("browser-default", ["browser_reader"], {
      ...browserEnv(), SCOUT_BROWSER_MCP: "", SCOUT_CLAUDE_BIN: slow.bin, SCOUT_RUN_TIMEOUT_MIN: "0.01",
    });
    try {
      assert.equal((await wake(search, { at: r.at })).status, 202);
      await waitFor(() => browserCalls(t0).length === 1 && ended(r));
      const { mcpServers } = JSON.parse(readFileSync(r.mcpConfig, "utf8"));
      assert.ok(mcpServers["chrome-devtools"].args.includes("chrome-devtools-mcp@1.10.1"));
      assert.doesNotMatch(r.log(), /timed out/, "the other kinds' run timeout does not apply");
    } finally {
      r.stop();
    }
  });
});
