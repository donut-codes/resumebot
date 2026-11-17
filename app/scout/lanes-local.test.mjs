// Tests for lanes-local.mjs and for what scout.mjs does on a local install. Unlike
// scout.test.mjs, which starts fake `claude`s by their shebang and so is POSIX-only, these run on Windows as well as on Linux
// and macOS: a fake `claude` here is a Node script (scout.mjs runs a .cjs file under the Node that runs it). What only Windows does
// (finding claude.exe, ending a session's whole process tree) is skipped elsewhere.
//   node --test lanes-local.test.mjs                                  (on your computer, from app/scout; `dev.py check --runner` runs it with the rest)
// This file is this app's own, not copied (app/PROVENANCE.md). It goes with scout.mjs when the lane supervisor replaces it.

import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { createServer as createHttpServer } from "node:http";
import { connect as connectNet, createServer as createNetServer } from "node:net";
import { networkInterfaces, tmpdir } from "node:os";
import path, { dirname, join } from "node:path";
import { after, before, describe, it } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { WINDOW_PAGE_PATH, anchorUrl } from "./own-chrome.mjs";
import { BROWSER_PORT_FILE, LANES, appRegister, bridgeOptions, browserCommand, ensureBridgeKey, readBridgeKey, checkAppUrl, checkStart, dataFolder, drainBrowser, laneEnv, moveStrays, parseEnvFile, pruneTranscripts, readSettings, startBridge, startLanes, untilNightly } from "./lanes-local.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const SCOUT = join(HERE, "scout.mjs");
const LAUNCHER = join(HERE, "lanes-local.mjs");
const INSTRUCTIONS = join(HERE, "..", "instructions");
const WINDOWS = process.platform === "win32";
const SECRET = "test-wake-secret";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const alive = (pid) => {
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === "EPERM"; }
};

// A wait is for an event (a lane listening, a session started or ended, a call arriving), and how long that takes depends on how
// busy this machine is. The deadline only ends a wait that will never finish, so it is never shorter than WAIT_FLOOR_MS, whatever
// a call asks.
const WAIT_FLOOR_MS = 60_000;
async function waitFor(predicate, ms = 15000, what = "something") {
  const end = Date.now() + Math.max(ms, WAIT_FLOOR_MS);
  while (Date.now() < end) {
    if (await predicate()) return;
    await sleep(50);
  }
  assert.fail(`timed out waiting for ${what}`);
}

let root;
let counter = 0;
// A folder of its own for each runner, launcher or layout, so no two tests share a file.
const folder = (tag) => {
  const dir = join(root, `${tag}-${++counter}`);
  mkdirSync(dir, { recursive: true });
  return dir;
};

// Every process a test starts, ended when the test file ends (and a Windows process tree with it: the job object Node puts what
// it starts in does that too, if this process ends first).
const started = new Set();
function endTree(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  if (WINDOWS) {
    const taskkill = join(process.env.SystemRoot || "C:\\Windows", "System32", "taskkill.exe");
    spawnSync(taskkill, ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" });
  } else {
    try { process.kill(-child.pid, "SIGKILL"); } catch { try { child.kill("SIGKILL"); } catch { /* gone */ } }
  }
}
function spawnTracked(file, args, options) {
  // Not detached on Windows (that would leave the job object); detached elsewhere, so the group can be ended in one go.
  const child = spawn(file, args, { windowsHide: true, ...(WINDOWS ? {} : { detached: true }), ...options });
  started.add(child);
  return child;
}
const exited = (child) => new Promise((resolve) => (child.exitCode !== null || child.signalCode !== null ? resolve(child.exitCode) : child.once("exit", resolve)));

// The fake apps' servers, closed when the file ends.
const servers = new Set();

// Preloaded into a runner (NODE_OPTIONS=--import): writes how each process was started (its spawn calls) to SPAWN_SPY.
const SPY = `
import { createRequire } from "node:module";
import { syncBuiltinESMExports } from "node:module";
import { appendFileSync } from "node:fs";
const cp = createRequire(import.meta.url)("node:child_process");
for (const name of ["spawn", "spawnSync"]) {
  const original = cp[name];
  cp[name] = function (file, args, options) {
    if (!Array.isArray(args)) {
      options = args;
      args = [];
    }
    appendFileSync(process.env.SPAWN_SPY, JSON.stringify({ name, file, first: String(args[0] ?? ""), windowsHide: options?.windowsHide ?? null, stdio: options?.stdio ?? null, cwd: options?.cwd ?? null }) + "\\n");
    return original.apply(this, arguments);
  };
}
syncBuiltinESMExports();
`;

before(() => {
  root = mkdtempSync(join(tmpdir(), "lanes-local-test-"));
  writeFileSync(join(root, "fake-claude.cjs"), `(${fakeClaude.toString()})();\n`);
  writeFileSync(join(root, "spy.mjs"), SPY);
});

after(async () => {
  for (const server of servers) server.close();
  for (const child of started) endTree(child);
  await Promise.race([Promise.all([...started].map(exited)), sleep(5000)]);
  // Best effort: Windows can hold a just-ended test's files for seconds under load, and a
  // folder left in the temp directory is no reason to fail a file whose tests passed.
  try { rmSync(root, { recursive: true, force: true, maxRetries: 20, retryDelay: 200 }); } catch (e) { console.warn(`left ${root}: ${e.message}`); }
});

// The stand-in for Claude Code. Records how it was started (its arguments, folders and the environment that matters) in
// FAKE_CALLS, one JSON line, then answers the way a finished session does. It calls the app's door with the headers of its MCP
// config, as a session that submits its work does. FAKE_MODE=hang: it starts a process of its own and then never ends.
function fakeClaude() {
  const fs = require("fs");
  const cp = require("child_process");
  const args = process.argv.slice(2);
  let door = null;
  try { door = JSON.parse(fs.readFileSync(args[args.indexOf("--mcp-config") + 1], "utf8")).mcpServers.jobsearch; } catch { /* no door */ }
  // The session's instructions arrive as a file (`--append-system-prompt-file`, a seat's `--system-prompt-file`) that the runner deletes when the session ends:
  // read it now, the way the real claude does at its start.
  const systemFlag = Math.max(args.indexOf("--append-system-prompt-file"), args.indexOf("--system-prompt-file"));
  const systemFile = systemFlag < 0 ? null : args[systemFlag + 1];
  let system = null;
  try { system = systemFile ? fs.readFileSync(systemFile, "utf8") : null; } catch { /* none to read */ }
  const record = {
    pid: process.pid, cwd: process.cwd(), argv: args, system, systemFile,
    configDir: process.env.CLAUDE_CONFIG_DIR ?? null, token: process.env.CLAUDE_CODE_OAUTH_TOKEN ?? null,
    apiKey: process.env.ANTHROPIC_API_KEY ?? null, authToken: process.env.ANTHROPIC_AUTH_TOKEN ?? null,
    noClaudeMds: process.env.CLAUDE_CODE_DISABLE_CLAUDE_MDS ?? null, noMemory: process.env.CLAUDE_CODE_DISABLE_AUTO_MEMORY ?? null,
    claudeEnv: Object.keys(process.env).filter((name) => /^(?:ANTHROPIC_|CLAUDE_|CLAUDECODE$)/i.test(name)).sort(),
    runner: (door && door.headers["X-Runner"]) || null,
  };
  const note = () => fs.appendFileSync(process.env.FAKE_CALLS, JSON.stringify(record) + "\n");
  if (process.env.FAKE_MODE === "hang") {
    // Detached, so that Node does not put it in a job object that ends with this process: the real claude.exe is no Node, and
    // what it starts outlives it when only it is ended.
    const grandchild = cp.spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], { stdio: "ignore", windowsHide: true, detached: true });
    grandchild.unref();
    record.grandchild = grandchild.pid;
    note();
    setInterval(() => {}, 1000);
  } else if (process.env.FAKE_MODE === "answer-error" || process.env.FAKE_MODE === "garble") {
    // A session that fails and says something: its words must never reach a lane's log.
    note();
    if (process.env.FAKE_MODE === "garble") console.log(process.env.FAKE_WORDS);
    else console.log(JSON.stringify({ num_turns: 1, is_error: true, result: process.env.FAKE_WORDS }));
    console.error(process.env.FAKE_WORDS);
    process.exit(1);
  } else {
    note();
    const done = () => { console.log(JSON.stringify({ num_turns: 1, is_error: false, result: "done" })); process.exit(0); };
    if (!door) done();
    else fetch(door.url, { method: "POST", headers: { ...door.headers, "Content-Type": "application/json" }, body: JSON.stringify({ submit: record.pid }) }).catch(() => {}).finally(done);
  }
}

const spied = (file) => (existsSync(file) ? readFileSync(file, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : []);

// The lines of a file of JSON lines written by processes that may be in the middle of a line: those that are closed.
function lines(file) {
  if (!existsSync(file)) return [];
  const text = readFileSync(file, "utf8");
  return text.slice(0, text.lastIndexOf("\n") + 1).split("\n").filter(Boolean).map((l) => JSON.parse(l));
}

// Nothing listens on the port: a connection is refused. Windows closes the sockets of a process that has ended a moment after it
// has ended (and a bind probe, portIsFree, also says no while the port's finished connections are in TIME_WAIT), so a test that
// has seen the processes gone waits on this, not on the port at one instant.
async function nothingListens(port) {
  return await new Promise((resolve) => {
    const probe = connectNet({ port, host: "127.0.0.1" });
    probe.once("connect", () => { probe.destroy(); resolve(false); });
    probe.once("error", (e) => resolve(e.code === "ECONNREFUSED"));
  });
}

async function portIsFree(port) {
  return await new Promise((resolve) => {
    const probe = createNetServer().once("error", () => resolve(false));
    probe.listen(port, "127.0.0.1", () => probe.close(() => resolve(true)));
  });
}
const freePort = async () => {
  for (;;) {
    const port = 20000 + Math.floor(Math.random() * 20000);
    if (await portIsFree(port)) return port;
  }
};
// The first of `n` ports in a row that are all free.
async function portsInARow(n) {
  for (;;) {
    const base = 20000 + Math.floor(Math.random() * 20000);
    let free = true;
    for (let i = 0; i < n && free; i++) free = await portIsFree(base + i);
    if (free) return base;
  }
}

// An address of this machine that is not loopback, or null.
function outsideAddress() {
  for (const list of Object.values(networkInterfaces())) {
    for (const a of list || []) if (a.family === "IPv4" && !a.internal) return a.address;
  }
  return null;
}

// This process's environment without what would reach a runner from the machine the tests run on.
function cleanEnv() {
  const env = { ...process.env };
  for (const name of Object.keys(env)) {
    if (/^(?:SCOUT_|PLANNER_|RESUMEBOT_|ANTHROPIC_|CLAUDE_|CLAUDECODE$|NODE_OPTIONS$)/i.test(name)) delete env[name];
  }
  return env;
}
const withoutUndefined = (env) => Object.fromEntries(Object.entries(env).filter(([, value]) => value !== undefined));

// A fake app: answers the runners' questions as `answer(path, query, body)` says, and records everything it is sent.
async function fakeApp(answer = () => ({})) {
  const seen = [];
  const server = createHttpServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", async () => {
      const url = new URL(req.url, "http://app.test");
      const text = Buffer.concat(chunks).toString("utf8");
      let body = null;
      try { body = text ? JSON.parse(text) : null; } catch { /* not JSON */ }
      seen.push({ method: req.method, path: url.pathname, query: url.search, headers: req.headers, body });
      const json = (await answer(url.pathname, Object.fromEntries(url.searchParams), body, req)) ?? {};
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(json));
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  servers.add(server);
  return { url: `http://127.0.0.1:${server.address().port}`, seen, close: () => server.close() };
}

// A runner of its own: the real scout.mjs with the folders a lane gets from the launcher, and the stand-in for Claude. `env`
// goes on top (a value of undefined leaves the setting out); `token` is the content of claude-oauth-token, or null for no file.
async function startRunner({ tag = "runner", env = {}, token = "file-token", listening = true, app = null } = {}) {
  const dir = folder(tag);
  const r = {
    dir, work: join(dir, "work"), claude: join(dir, "claude"), secrets: join(dir, "secrets"), log: join(dir, "runs.log"),
    mcp: join(dir, "mcp.json"), calls: join(dir, "calls.jsonl"), spy: join(dir, "spy.jsonl"), port: await freePort(),
  };
  mkdirSync(r.work);
  mkdirSync(r.secrets);
  writeFileSync(join(r.secrets, "agent-token"), "agent-token-value");
  writeFileSync(join(r.secrets, "wake-secret"), SECRET);
  if (token) writeFileSync(join(r.secrets, "claude-oauth-token"), token);
  const environment = withoutUndefined({
    ...cleanEnv(),
    SCOUT_PORT: String(r.port), RESUMEBOT_MCP_URL: `${app ? app.url : "http://127.0.0.1:9"}/mcp`,
    SCOUT_SECRETS_DIR: r.secrets, SCOUT_INSTRUCTIONS_DIR: INSTRUCTIONS, SCOUT_MCP_CONFIG: r.mcp, SCOUT_WORKDIR: r.work,
    SCOUT_CLAUDE_CONFIG_DIR: r.claude, SCOUT_LOG: r.log, SCOUT_CLAUDE_BIN: join(root, "fake-claude.cjs"),
    SCOUT_CHECK_IN_MIN: "20000", SCOUT_PRESENCE_SEC: "3600", SCOUT_MODEL: "model-test", SCOUT_EFFORT: "medium",
    FAKE_CALLS: r.calls, SPAWN_SPY: r.spy, ...env,
  });
  r.child = spawnTracked(process.execPath, [SCOUT], { stdio: "ignore", env: environment });
  // A port found free by `freePort` can be taken by another process before the runner binds it (the machine hands the same
  // ports to every process's outgoing connections; found under load). The runner then says so and ends: it is started again on
  // another port. Only when this test did not name the port itself.
  const startAgainIfTaken = async () => {
    for (let attempt = 1; attempt < 10 && !env.SCOUT_PORT && /cannot listen on/.test(r.text()); attempt++) {
      await exited(r.child);
      rmSync(r.log, { force: true });
      r.port = await freePort();
      environment.SCOUT_PORT = String(r.port);
      r.child = spawnTracked(process.execPath, [SCOUT], { stdio: "ignore", env: environment });
      await waitFor(() => /listening on :|cannot listen on/.test(r.text()), 15000, "the runner to listen");
    }
  };
  r.text = () => (existsSync(r.log) ? readFileSync(r.log, "utf8") : "");
  r.sessions = () => lines(r.calls);
  r.wake = async (reason, project = "p") => {
    const body = JSON.stringify({ mode: "scout", reason, project_id: project });
    const timestamp = String(Math.floor(Date.now() / 1000));
    const signature = createHmac("sha256", SECRET).update(`${timestamp}.`).update(body).digest("hex");
    return await fetch(`http://127.0.0.1:${r.port}/wake`, {
      method: "POST", body, headers: { Connection: "close", "Content-Type": "application/json", "X-Webhook-Timestamp": timestamp, "X-Webhook-Signature-V2": signature },
    });
  };
  r.health = async () => (await fetch(`http://127.0.0.1:${r.port}/health`, { headers: { Connection: "close" } })).json();
  if (listening) {
    await waitFor(() => /listening on :|cannot listen on/.test(r.text()), 15000, "the runner to listen");
    await startAgainIfTaken();
    assert.ok(r.text().includes("listening on :"), r.text());
    // The lines after it (which Claude Code, which sign-in) are written a moment later: wait for the last of them.
    if (environment.SCOUT_CLAUDE_CONFIG_DIR) await waitFor(() => /sign in with the token file|no token file|token file cannot be read/.test(r.text()), 15000, "the startup lines");
  }
  return r;
}

const arg = (call, flag) => call.argv[call.argv.indexOf(flag) + 1];
// The values after `--allowedTools`, up to the next flag.
const allowed = (call) => {
  const rest = call.argv.slice(call.argv.indexOf("--allowedTools") + 1);
  const end = rest.findIndex((a) => a.startsWith("--"));
  return end < 0 ? rest : rest.slice(0, end);
};
const sameFolder = (a, b) => realpathSync.native(a).toLowerCase() === realpathSync.native(b).toLowerCase();
// What an address that nothing listens on does to a request: it is refused, or (behind a firewall) it times out, or the network
// the address belongs to (a VPN's, say) gives up on it its own way: no connection is made, which is what is asked.
const UNREACHABLE = ["ECONNREFUSED", "ECONNABORTED", "ECONNRESET", "EHOSTUNREACH", "ENETUNREACH", "ETIMEDOUT", "UND_ERR_CONNECT_TIMEOUT"];
const unreachable = (e) => UNREACHABLE.includes(e.cause?.code) || e.name === "TimeoutError";

describe("browser instructions", () => {
  it("tell the session never to save snapshots to a file", () => {
    for (const kind of ["reader", "learner"]) {
      const text = readFileSync(join(INSTRUCTIONS, `browser.${kind}.instructions.md`), "utf8");
      assert.match(text, /Never save a snapshot, screenshot or any other output to a file/);
    }
  });
});

describe("the launcher's settings", () => {
  const ENOENT = Object.assign(new Error("no such file"), { code: "ENOENT" });
  const reading = (files) => (file) => {
    if (file in files) return files[file];
    throw ENOENT;
  };

  it("keeps the data folder where each system keeps a user's own", () => {
    assert.equal(dataFolder({ LOCALAPPDATA: "D:\\Apps\\Local" }, "win32", "C:\\h\\me"), "D:\\Apps\\Local\\resumebot");
    assert.equal(dataFolder({}, "win32", "C:\\h\\me"), "C:\\h\\me\\AppData\\Local\\resumebot");
    assert.equal(dataFolder({}, "darwin", "/h/me"), "/h/me/.local/share/resumebot", "the folder `resumebot init` makes on a Mac");
    assert.equal(dataFolder({}, "linux", "/h/me"), "/h/me/.local/share/resumebot");
    assert.equal(dataFolder({ XDG_DATA_HOME: "/x/share" }, "linux", "/h/me"), "/x/share/resumebot");
    // RESUMEBOT_DATA_DIR names the folder, wherever it is: the app reads the same variable and the two agree.
    assert.equal(dataFolder({ RESUMEBOT_DATA_DIR: "D:\\somewhere\\else", LOCALAPPDATA: "D:\\Apps\\Local" }, "win32", "C:\\h\\me"), "D:\\somewhere\\else");
    assert.equal(dataFolder({ RESUMEBOT_DATA_DIR: " /srv/rb ", XDG_DATA_HOME: "/x/share" }, "linux", "/h/me"), "/srv/rb");
    assert.equal(dataFolder({ RESUMEBOT_DATA_DIR: "  " }, "linux", "/h/me"), "/h/me/.local/share/resumebot", "a blank one names nothing");
  });

  it("reads KEY=value lines: comments and blanks skipped, one pair of quotes and a leading export allowed", () => {
    assert.deepEqual(parseEnvFile("# a comment\n\nA=1\r\nexport B = two words \nC=\"quoted # not a comment\"\nD='single'\nE=plain # trailing\nnot a line\n=x\n"), {
      A: "1", B: "two words", C: "quoted # not a comment", D: "single", E: "plain",
    });
  });

  it("takes the app's address only when it is this machine's, as an origin without a path", () => {
    for (const [given, origin] of [
      ["http://127.0.0.1:8430", "http://127.0.0.1:8430"], ["http://localhost:8430/", "http://localhost:8430"],
      ["http://[::1]:8430", "http://[::1]:8430"], ["http://127.1.2.3/some/path", "http://127.1.2.3"],
    ]) assert.equal(checkAppUrl(given), origin);
    assert.throws(() => checkAppUrl("http://192.168.1.20:8430"), /192\.168\.1\.20 is not.*the lanes send it their token/);
    assert.throws(() => checkAppUrl("https://example.com"), /example\.com/);
    assert.throws(() => checkAppUrl("ftp://127.0.0.1"), /http address/);
    assert.throws(() => checkAppUrl("nonsense"), /not an address/);
  });

  it("is set by the data folder's .env under the environment: the app's address, the ports, the secrets, and what the lanes are passed", () => {
    const data = "/x/share/resumebot";
    const file = [
      "RESUMEBOT_APP_URL=http://127.0.0.1:9000", "SCOUT_PORT=9100", "SCOUT_MODEL=from-file", "SCOUT_EFFORT=from-file", "JOBSEARCH_OWNER=not-for-lanes",
    ].join("\n");
    const read = reading({ [`${data}/.env`]: file });
    const settings = readSettings({ env: { XDG_DATA_HOME: "/x/share", SCOUT_EFFORT: "from-env" }, platform: "linux", home: "/h/me", read, here: "/code/app/scout" });
    assert.equal(settings.data, data);
    assert.equal(settings.appUrl, "http://127.0.0.1:9000");
    assert.equal(settings.basePort, 9100);
    assert.equal(settings.secrets, `${data}/secrets`);
    assert.equal(settings.instructions, "/code/app/instructions");
    assert.equal(settings.scout, "/code/app/scout/scout.mjs");
    assert.deepEqual(
      settings.passed,
      { RESUMEBOT_APP_URL: "http://127.0.0.1:9000", SCOUT_PORT: "9100", SCOUT_MODEL: "from-file" },
      "every SCOUT_* and RESUMEBOT_* line of the file the environment does not set; not the environment's own SCOUT_EFFORT, not the app's JOBSEARCH_OWNER",
    );
    // Nothing set: the defaults (the app on 8430, the search lane on 8651); the environment names the folders it wants.
    const bare = readSettings({
      env: { XDG_DATA_HOME: "/x/share", RESUMEBOT_APP_URL: "http://localhost:8431", SCOUT_SECRETS_DIR: "/s", SCOUT_INSTRUCTIONS_DIR: "/i", SCOUT_PORT: "not a port" },
      platform: "linux", home: "/h/me", read: reading({}), here: "/code/app/scout",
    });
    assert.equal(bare.appUrl, "http://localhost:8431");
    assert.equal(bare.basePort, 8651);
    assert.equal(bare.secrets, "/s");
    assert.equal(bare.instructions, "/i");
    assert.equal(readSettings({ env: { XDG_DATA_HOME: "/x" }, platform: "linux", home: "/h", read: reading({}), here: "/c/s" }).appUrl, "http://127.0.0.1:8430");
    assert.throws(
      () => readSettings({ env: { XDG_DATA_HOME: "/x", RESUMEBOT_APP_URL: "http://192.168.1.20:8430" }, platform: "linux", home: "/h", read: reading({}), here: "/c/s" }),
      /on this machine/,
    );
  });

  it("gives each of the five lanes its own port, folders and name, and points them all at the app", () => {
    for (const platform of ["win32", "linux"]) {
      const p = platform === "win32" ? path.win32 : path.posix;
      const home = platform === "win32" ? "C:\\h\\me" : "/h/me";
      const settings = readSettings({ env: { SCOUT_PORT: "9100" }, platform, home, read: reading({}), here: p.join(home, "app", "scout") });
      const envs = LANES.map((lane, index) => laneEnv(settings, lane, index, { env: { KEPT: "yes" } }));
      assert.deepEqual(LANES.map((l) => l.runner), ["session-1", "session-2", "session-3", "session-4", "session-5"]);
      assert.deepEqual(envs.map((e) => e.SCOUT_PORT), ["9100", "9101", "9102", "9103", "9104"]);
      for (const key of ["SCOUT_WORKDIR", "SCOUT_CLAUDE_CONFIG_DIR", "SCOUT_LOG", "SCOUT_MCP_CONFIG"]) {
        assert.equal(new Set(envs.map((e) => e[key])).size, 5, `${key} is each lane's own`);
      }
      const owner = envs[3];
      assert.equal(owner.SCOUT_RUNNER, "session-4");
      assert.equal(owner.SCOUT_WORKDIR, p.join(settings.data, "work", "owner"));
      assert.equal(owner.SCOUT_CLAUDE_CONFIG_DIR, p.join(settings.data, "claude", "owner"));
      assert.equal(owner.SCOUT_LOG, p.join(settings.data, "logs", "runs-owner.log"));
      assert.equal(owner.SCOUT_MCP_CONFIG, p.join(settings.data, "run", "session-4.mcp.json"));
      assert.equal(envs[0].SCOUT_LOG, p.join(settings.data, "logs", "runs.log"), "the first session's log is runs.log, as in the default mode");
      for (const e of envs) {
        assert.equal(e.RESUMEBOT_MCP_URL, "http://127.0.0.1:8430/mcp");
        assert.equal(e.RESUMEBOT_API_URL, "http://127.0.0.1:8430");
        assert.equal(e.SCOUT_BIND, "127.0.0.1");
        assert.equal(e.SCOUT_SECRETS_DIR, settings.secrets);
        assert.equal(e.KEPT, "yes", "the rest of the environment goes along");
      }
      assert.ok(envs.every((e) => !("SCOUT_LANE" in e)), "no lane is named: every runner is the pool's");
      // One browser profile for every session (they share one Chrome): the install's, in the data folder unless set.
      assert.deepEqual([...new Set(envs.map((e) => e.SCOUT_BROWSER_PROFILE))], [p.join(settings.data, "browser-profile")]);
      const set = readSettings({ env: { SCOUT_BROWSER_PROFILE: p.join(home, "profile") }, platform, home, read: reading({}), here: p.join(home, "app", "scout") });
      assert.equal(laneEnv(set, LANES[0], 0, { env: {} }).SCOUT_BROWSER_PROFILE, p.join(home, "profile"));
      // The app's own profile unless the install says the owner's own Chrome (the owner, 2026-10-16).
      assert.deepEqual([...new Set(envs.map((e) => e.SCOUT_BROWSER))], ["profile"]);
      assert.deepEqual([...new Set(envs.map((e) => e.SCOUT_BROWSER_PORT))], ["9333"], "every lane is told the bridge's port");
      assert.deepEqual([...new Set(envs.map((e) => e.SCOUT_BROWSER_BRIDGE))], ["1"], "every session attaches to the launcher's bridge");
      assert.deepEqual([...new Set(envs.map((e) => e.SCOUT_JOB_WRAP))], platform === "win32" ? ["1"] : [undefined],
        "on Windows every session starts in a Job Object (jobwrap.py)");
      const own = readSettings({ env: { SCOUT_BROWSER: "Own" }, platform, home, read: reading({}), here: p.join(home, "app", "scout") });
      assert.equal(laneEnv(own, LANES[0], 0, { env: {} }).SCOUT_BROWSER, "own");
      assert.throws(() => readSettings({ env: { SCOUT_BROWSER: "mine" }, platform, home, read: reading({}), here: p.join(home, "app", "scout") }), /SCOUT_BROWSER must be own or profile/);
    }
  });

  it("opens the shared browser visibly on its profile, its DevTools port on 127.0.0.1 only (open-browser-profile)", () => {
    const settings = readSettings({ env: { XDG_DATA_HOME: "/x" }, platform: "linux", home: "/h", read: reading({}), here: "/c/s" });
    const found = browserCommand(settings, { exists: (place) => place === "/usr/bin/chromium" });
    assert.equal(found.file, "/usr/bin/chromium");
    assert.deepEqual(found.args.slice(0, 3), [
      "--user-data-dir=/x/resumebot/browser-profile", "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1"],
    "Chrome picks the port; the bridge on SCOUT_BROWSER_PORT reads it from the profile");
    assert.ok(!found.args.some((a) => a.startsWith("--headless")), "visible, so the owner can sign in");
    const named = readSettings({ env: { XDG_DATA_HOME: "/x", SCOUT_CHROME_BIN: "/opt/chrome", SCOUT_BROWSER_PORT: "9400" }, platform: "linux", home: "/h", read: reading({}), here: "/c/s" });
    const chosen = browserCommand(named, { exists: () => false });
    assert.equal(chosen.file, "/opt/chrome");
    assert.ok(chosen.args.includes("--remote-debugging-port=0"));
    assert.equal(browserCommand(settings, { exists: () => false }).file, "", "not found: said, never guessed");
  });
});

// The bridge's port.
describe("the browser bridge's port", () => {
  const taken = () => new Promise((resolve) => {
    const s = createNetServer().listen(0, "127.0.0.1", () => resolve(s));
  });
  const fakeBridge = (port) => {
    const server = createNetServer();
    return {
      server,
      get port() { return server.address()?.port ?? port; },
      start: () => new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(port, "127.0.0.1", () => resolve());
      }),
    };
  };

  it("takes a free port when the default one is in use, and the lanes are told the one in use", async () => {
    const holder = await taken();
    const port = holder.address().port;
    const data = mkdtempSync(join(tmpdir(), "rb-port-"));
    const settings = { browserPort: port, browserPortSet: false, data, platform: "linux", passed: {}, appUrl: "http://127.0.0.1:8430", basePort: 8651, secrets: "/s", instructions: "/i", browser: "profile", browserProfile: "/p" };
    const said = [];
    const bridge = await startBridge(settings, fakeBridge, (line) => said.push(line));
    try {
      assert.notEqual(settings.browserPort, port, "another port than the taken one");
      assert.equal(bridge.port, settings.browserPort);
      assert.match(said.join("\n"), /is in use/);
      assert.equal(laneEnv(settings, LANES[0], 0, { env: {} }).SCOUT_BROWSER_PORT, String(settings.browserPort));
      assert.equal(readFileSync(join(data, BROWSER_PORT_FILE), "utf8").trim(), String(settings.browserPort),
        "the app is told the port in use, in the data folder");
    } finally {
      bridge.server.close();
      holder.close();
      rmSync(data, { recursive: true, force: true });
    }
  });

  it("uses only the port the install named, and says it is taken", async () => {
    const holder = await taken();
    const port = holder.address().port;
    try {
      await assert.rejects(startBridge({ browserPort: port, browserPortSet: true }, fakeBridge), (e) => e.code === "EADDRINUSE");
    } finally {
      holder.close();
    }
  });
});

// A stop or a release lets the browser workers finish the item each holds (the browser work spec: up to about three
// minutes), so a restart loses no work.
describe("the browser workers' drain on a stop", () => {
  const appWith = (holding) => {
    const calls = [];
    const fetchImpl = async (url, init) => {
      calls.push({ url, method: init.method, body: init.body, auth: init.headers.Authorization });
      const n = holding.length > 1 ? holding.shift() : holding[0];
      return { ok: true, json: async () => ({ ok: true, holding: n }) };
    };
    return { calls, fetchImpl };
  };

  it("asks the app to drain, then waits until no worker holds an item", async () => {
    const { calls, fetchImpl } = appWith([2, 1, 0]);
    const said = [];
    const done = await drainBrowser({ appUrl: "http://127.0.0.1:8430", token: "tok", everyMs: 1, fetchImpl, say: (l) => said.push(l) });
    assert.equal(done, true);
    assert.equal(calls[0].method, "POST");
    assert.equal(calls[0].url, "http://127.0.0.1:8430/api/v1/agent/browser/drain");
    assert.deepEqual(JSON.parse(calls[0].body), { on: true });
    assert.equal(calls[0].auth, "Bearer tok");
    assert.ok(calls.slice(1).every((c) => c.method === "GET"));
    assert.match(said.join("\n"), /waiting for 2 browser worker/);
  });

  it("stops waiting after the limit, and stops at once when the app does not answer", async () => {
    const { fetchImpl } = appWith([1]);
    assert.equal(await drainBrowser({ appUrl: "http://a", token: "t", waitMs: 20, everyMs: 5, fetchImpl }), false);
    const down = async () => { throw new Error("connection refused"); };
    const t0 = Date.now();
    assert.equal(await drainBrowser({ appUrl: "http://a", token: "t", fetchImpl: down }), false);
    assert.ok(Date.now() - t0 < 1000, "no wait when the app is down");
  });
});

describe("a lane on a PC", () => {
  // Starts a session of some kind (the analyst's: it needs the app for nothing but its own door).
  const writer = (r) => r.wake("analysis_asked", "analysis-1");

  it("listens on 127.0.0.1 alone when told to, and says so; nothing else on the network can reach it", async () => {
    const outside = outsideAddress();
    const r = await startRunner({ tag: "bound", env: { SCOUT_BIND: "127.0.0.1" } });
    assert.equal((await r.health()).ok, true);
    assert.match(r.text(), /listening on :\d+, check-in every 20000 min, only on 127\.0\.0\.1/);
    if (outside) await assert.rejects(fetch(`http://${outside}:${r.port}/health`, { signal: AbortSignal.timeout(3000) }), unreachable);
  });

  it("listens on 127.0.0.1 alone on Windows with nothing said, and on every address elsewhere, as the default mode needs", async () => {
    const outside = outsideAddress();
    const r = await startRunner({ tag: "default-bind" });
    if (WINDOWS) assert.match(r.text(), /listening on :\d+, check-in every 20000 min, only on 127\.0\.0\.1/);
    else assert.doesNotMatch(r.text(), /only on/);
    if (!outside) return;
    if (WINDOWS) await assert.rejects(fetch(`http://${outside}:${r.port}/health`, { signal: AbortSignal.timeout(3000) }), unreachable);
    else assert.equal((await fetch(`http://${outside}:${r.port}/health`, { signal: AbortSignal.timeout(3000) })).ok, true);
  });

  it("starts a session in the lane's own Claude folder and empty working folder, signed in by the token file and by nothing else", async () => {
    const r = await startRunner({
      tag: "session", token: "file-token",
      env: { ANTHROPIC_API_KEY: "must-not-reach-claude", ANTHROPIC_AUTH_TOKEN: "nor-this", CLAUDE_CODE_OAUTH_TOKEN: "nor-the-ambient-one" },
    });
    assert.match(r.text(), /sessions sign in with the token file/);
    assert.equal((await writer(r)).status, 202);
    await waitFor(() => r.sessions().length === 1, 15000, "a session");
    const [call] = r.sessions();
    assert.equal(call.configDir, r.claude, "a Claude folder of its own: the owner's CLAUDE.md, hooks and memory are in the default one");
    assert.ok(sameFolder(call.cwd, r.work), "the working folder is the lane's own");
    assert.equal(call.token, "file-token");
    assert.equal(call.apiKey, null, "subscription only, never the API");
    assert.equal(call.authToken, null);
    assert.equal(call.noClaudeMds, "1", "no CLAUDE.md from anywhere");
    assert.equal(call.noMemory, "1");
    assert.ok(existsSync(r.claude), "the Claude folder is made when a session needs it");
  });

  it("runs on the login in its Claude folder when there is no token file, and takes no token from the environment it was started in", async () => {
    const r = await startRunner({ tag: "no-token", token: null, env: { CLAUDE_CODE_OAUTH_TOKEN: "ambient", ANTHROPIC_API_KEY: "ambient" } });
    assert.match(r.text(), /no token file: sessions run on the login in the Claude folder/);
    await writer(r);
    await waitFor(() => r.sessions().length === 1, 15000, "a session");
    const [call] = r.sessions();
    assert.equal(call.token, null);
    assert.equal(call.apiKey, null);
    assert.equal(call.configDir, r.claude);
  });

  it("takes nothing of Claude Code's or the API's from the environment it was started in, but where Git Bash is: that is the host session's, not the lane's", async () => {
    const r = await startRunner({
      tag: "ambient",
      env: {
        ANTHROPIC_BASE_URL: "http://example.test", CLAUDECODE: "1", CLAUDE_CODE_SIMPLE: "1", CLAUDE_CODE_SESSION_ATTENDED: "1",
        CLAUDE_AGENT_SDK_VERSION: "1", CLAUDE_EFFORT: "max", CLAUDE_CODE_GIT_BASH_PATH: "where-git-bash-is",
      },
    });
    await writer(r);
    await waitFor(() => r.sessions().length === 1, 15000, "a session");
    assert.deepEqual(r.sessions()[0].claudeEnv, [
      "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "CLAUDE_CODE_DISABLE_CLAUDE_MDS", "CLAUDE_CODE_GIT_BASH_PATH", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
    ]);
  });

  it("keeps today's flags: the strict MCP config, the kind's own tool lists, its instructions alone, whole, through a file", async () => {
    const r = await startRunner({ tag: "flags", env: { SCOUT_MAX_TURNS: "17" } });
    await writer(r);
    await waitFor(() => r.sessions().length === 1, 15000, "a session");
    const [call] = r.sessions();
    assert.ok(call.argv.includes("--strict-mcp-config"));
    assert.equal(arg(call, "--mcp-config"), r.mcp);
    assert.equal(arg(call, "--tools"), "WebSearch,WebFetch", "no shell and no files");
    assert.deepEqual(allowed(call), ["mcp__jobsearch__jobs_analysis", "mcp__jobsearch__jobs_analysis_submit", "WebSearch", "WebFetch"]);
    assert.equal(arg(call, "--model"), "model-test");
    assert.equal(arg(call, "--effort"), "medium");
    assert.equal(arg(call, "--max-turns"), "17");
    assert.equal(arg(call, "--output-format"), "json");
    // The instruction file, with its header comment dropped, arrives byte for byte, in the file `--append-system-prompt-file` names
    // (never on the command line, where the length of one argument is limited): several lines, quotes and backslashes in it.
    const instructions = (file) => readFileSync(join(INSTRUCTIONS, file), "utf8").replace(/^\s*<!--[\s\S]*?-->\s*/, "").trim();
    const text = [instructions("jobsearch.analyst.instructions.md"), instructions("jobsearch.scoring.instructions.md")].join("\n\n");
    assert.ok(text.includes("\n") && text.includes('"'), "the file is the kind that could be broken by a command line");
    assert.ok(!call.argv.includes("--append-system-prompt"), "the instructions are not an argument");
    assert.equal(call.system, text);
    assert.equal(call.systemFile, join(dirname(r.mcp), "mcp.system.md"), "beside the MCP config, which a PC lane keeps in its own run folder");
    const config = JSON.parse(readFileSync(r.mcp, "utf8"));
    assert.deepEqual(Object.keys(config.mcpServers), ["jobsearch"]);
    assert.equal(config.mcpServers.jobsearch.headers.Authorization, "Bearer agent-token-value");
    // The longest kind, the analyst (its instructions and the scoring paragraph), has a command line that is short however long
    // its instructions grow: Windows allows 32,767 characters to one (a kind past that would fail to start, "spawn
    // ENAMETOOLONG"), and this is why they go by file.
    const length = call.argv.reduce((n, a) => n + a.length + 3, 0);
    assert.ok(length < 4000, `the analyst's command line is ${length} characters: it holds no instruction text`);
    await waitFor(() => !existsSync(call.systemFile), 15000, "the instructions file to be deleted when the session ends");
  });

  it("moves what is in the working folder to work-leftovers, logs it, and starts the session", async () => {
    const r = await startRunner({ tag: "stray" });
    writeFileSync(join(r.work, "snap1.txt"), "a snapshot");
    await writer(r);
    await waitFor(() => r.sessions().length === 1, 15000, "the session");
    assert.match(r.text(), /moved 1 stray file\(s\) out of .* \(snap1\.txt\) to .*work-leftovers/);
    assert.deepEqual(readdirSync(r.work), []);
    const left = join(dirname(dirname(r.work)), "work-leftovers", path.basename(r.work));
    const [stamp] = readdirSync(left);
    assert.equal(readFileSync(join(left, stamp, "snap1.txt"), "utf8"), "a snapshot");
  });

  it("starts its sessions with no console window and, on Windows, no input at all", async () => {
    const r = await startRunner({ tag: "spawn-options", env: { NODE_OPTIONS: `--import=${pathToFileURL(join(root, "spy.mjs")).href}` } });
    await writer(r);
    await waitFor(() => r.sessions().length === 1, 15000, "a session");
    const spawns = spied(r.spy).filter((s) => s.first === join(root, "fake-claude.cjs"));
    assert.equal(spawns.length, 1);
    assert.equal(spawns[0].windowsHide, true);
    assert.ok(sameFolder(spawns[0].cwd, r.work));
    if (WINDOWS) assert.deepEqual(spawns[0].stdio, ["ignore", "pipe", "pipe"], "Claude Code waits three seconds for piped input before it starts when stdin is an open pipe");
  });

  it("is unchanged in the default mode: no Claude folder of its own, no isolation settings, every address, the token file's login", { skip: WINDOWS }, async () => {
    const r = await startRunner({ tag: "container", env: { SCOUT_CLAUDE_CONFIG_DIR: undefined, CLAUDE_CODE_ENTRYPOINT: "left alone", ANTHROPIC_API_KEY: "never" } });
    assert.doesNotMatch(r.text(), /only on|sign in with|no token file/);
    await writer(r);
    await waitFor(() => r.sessions().length === 1, 15000, "a session");
    const [call] = r.sessions();
    assert.equal(call.configDir, null);
    assert.equal(call.noClaudeMds, null);
    assert.equal(call.noMemory, null);
    assert.equal(call.token, "file-token");
    assert.deepEqual(call.claudeEnv, ["CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_OAUTH_TOKEN"], "the environment goes along as it always did, but for the API key");
  });

  it("runs a seat the app hands out as a single call with no tools: its own system file, no MCP config, the answer posted back", async () => {
    let served = 0;
    const app = await fakeApp((p, q, body) => {
      if (p.endsWith("/work")) return { next: served++ === 0 ? "seat" : null };
      if (p.endsWith("/seat/take")) return { seat: { id: "s-1", model: "sonnet", effort: null, label: "x", system: "The seat's system text.", prompt: "Write it." } };
      return { ok: true };
    });
    const r = await startRunner({ tag: "seat", app });
    await r.wake("packet_asked", "seat-1");
    await waitFor(() => app.seen.some((s) => s.path.endsWith("/seat/s-1/answer")), 15000, "the answer");
    const [call] = r.sessions();
    assert.equal(call.system, "The seat's system text.");
    assert.equal(arg(call, "-p"), "Write it.");
    assert.equal(arg(call, "--model"), "model-test");
    assert.equal(arg(call, "--tools"), "");
    assert.ok(!call.argv.includes("--mcp-config"));
    assert.ok(!existsSync(r.mcp), "no MCP config for a seat");
    const answer = app.seen.find((s) => s.path.endsWith("/seat/s-1/answer"));
    assert.equal(answer.headers.authorization, "Bearer agent-token-value");
    assert.equal(answer.body.text, "done");
  });

  it("will not start on Windows without a Claude folder and a working folder of its own, never in the owner's", { skip: !WINDOWS }, async () => {
    for (const env of [{ SCOUT_CLAUDE_CONFIG_DIR: "" }, { SCOUT_WORKDIR: "" }]) {
      const r = await startRunner({ tag: "refusal", env, listening: false });
      assert.equal(await exited(r.child), 1);
      assert.match(r.text(), /SCOUT_CLAUDE_CONFIG_DIR and SCOUT_WORKDIR must name folders of this runner's own.*not starting/);
    }
  });

  it("says what is wrong, and stops, when it cannot listen", async () => {
    const taken = await freePort();
    const holder = createNetServer();
    await new Promise((resolve) => holder.listen(taken, "127.0.0.1", resolve));
    try {
      const r = await startRunner({ tag: "taken", env: { SCOUT_PORT: String(taken), SCOUT_BIND: "127.0.0.1" }, listening: false });
      assert.equal(await exited(r.child), 1);
      assert.match(r.text(), new RegExp(`cannot listen on 127\\.0\\.0\\.1:${taken}: .*not starting`));
    } finally {
      holder.close();
    }
  });
});

describe("the Claude Code a lane finds on Windows", { skip: !WINDOWS }, () => {
  // A Windows machine's folders for one test: where each place Claude Code may be is, and the environment that names them.
  function machine() {
    const home = folder("machine");
    const m = {
      home,
      appData: join(home, "AppData", "Roaming"),
      localAppData: join(home, "AppData", "Local"),
      pathDir: join(home, "bin"),
      desktop: join(home, "AppData", "Roaming", "Claude", "claude-code"),
      // The same folder as the real one the packaged Claude desktop app keeps it in, which a program outside the app sees.
      packaged: join(home, "AppData", "Local", "Packages", "Claude_abc123", "LocalCache", "Roaming", "Claude", "claude-code"),
      // Where the app has kept its copies since it moved them (2026-10): Claude-Data\claude_<package id>\claude-code.
      claudeData: join(home, "AppData", "Local", "Claude-Data", "claude_pzs8sxrjxfjjc", "claude-code"),
    };
    mkdirSync(m.pathDir, { recursive: true });
    return m;
  }
  const program = (file, content = "MZ not a program") => {
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(file, content);
    return file;
  };
  // A copy in the Claude desktop app's folder (`root`: as the app sees it, or its package's real one); `size` is what its
  // .payload promises (none: no .payload at all).
  const desktopCopy = (m, version, build, { size, content = "MZ a copy of claude", root = m.desktop } = {}) => {
    const exe = program(join(root, version, build, "claude.exe"), content);
    if (size !== undefined) writeFileSync(join(root, version, build, ".payload"), JSON.stringify({ sha256: "x", size }));
    return exe;
  };
  const whole = (content = "MZ a copy of claude") => ({ size: Buffer.byteLength(content), content });
  const lane = (m, env = {}) => startRunner({
    tag: "finds",
    env: {
      SCOUT_CLAUDE_BIN: undefined, USERPROFILE: m.home, APPDATA: m.appData, LOCALAPPDATA: m.localAppData,
      PATH: m.pathDir, ...env,
    },
  });
  const found = (r) => /claude is (.+) \(from (.+)\)/.exec(r.text());

  it("takes the newest version of the Claude desktop app's copy by its number, not its name", async () => {
    const m = machine();
    desktopCopy(m, "2.1.293", "aaaa", whole());
    desktopCopy(m, "2.1.295", "bbbb", whole());
    const newest = desktopCopy(m, "2.1.1000", "cccc", whole());
    desktopCopy(m, "latest", "dddd", whole());
    program(join(m.desktop, ".tmp-download", "claude.exe"));
    const r = await lane(m);
    assert.equal(found(r)?.[1], newest);
    assert.equal(found(r)?.[2], "the Claude desktop app, 2.1.1000");
  });

  it("passes over a copy that is still being written for the version before it, and takes one with no .payload only when no other is there", async () => {
    const m = machine();
    const older = desktopCopy(m, "2.1.290", "aaaa", whole());
    desktopCopy(m, "2.1.295", "bbbb", { size: 999999, content: "MZ half of a copy" });
    desktopCopy(m, "2.1.299", "cccc", { content: "MZ nothing says it is whole" });
    const first = await lane(m);
    assert.equal(found(first)?.[1], older, "the copy that is whole, over a newer one that is not or cannot be told");
    const alone = machine();
    const unproven = desktopCopy(alone, "2.1.299", "cccc", { content: "MZ nothing says it is whole" });
    desktopCopy(alone, "2.1.300", "dddd", { size: 1, content: "MZ half" });
    const second = await lane(alone);
    assert.equal(found(second)?.[1], unproven);
  });

  it("finds the copy in the Claude desktop app's package folder, all that a program started outside the app can see", async () => {
    const m = machine();
    const copy = desktopCopy(m, "2.1.295", "bbbb", { ...whole(), root: m.packaged });
    const r = await lane(m);
    assert.equal(found(r)?.[1], copy);
    assert.equal(found(r)?.[2], "the Claude desktop app, 2.1.295");
  });

  it("finds the copy in the Claude-Data folder the desktop app keeps its copies in now, whatever the case of the folder's name", async () => {
    const m = machine();
    const copy = desktopCopy(m, "2.1.308", "c17a819afb40", { ...whole(), root: m.claudeData });
    const r = await lane(m);
    assert.equal(found(r)?.[1], copy);
    assert.equal(found(r)?.[2], "the Claude desktop app, 2.1.308");
    const upper = machine();
    const other = desktopCopy(upper, "2.1.309", "dddd", { ...whole(), root: join(upper.localAppData, "Claude-Data", "CLAUDE_ABC123", "claude-code") });
    assert.equal(found(await lane(upper))?.[1], other);
  });

  it("takes the newest copy of the two folders, and a whole one over a newer one that cannot be told whole", async () => {
    const m = machine();
    desktopCopy(m, "2.1.293", "aaaa", whole());
    const newest = desktopCopy(m, "2.1.295", "bbbb", { ...whole(), root: m.packaged });
    assert.equal(found(await lane(m))?.[1], newest);
    const n = machine();
    const wholeOlder = desktopCopy(n, "2.1.293", "aaaa", { ...whole(), root: n.packaged });
    desktopCopy(n, "2.1.299", "cccc", { content: "MZ nothing says it is whole" });
    assert.equal(found(await lane(n))?.[1], wholeOlder);
  });

  it("looks in this order: claude.exe on the PATH, the native install's place, WinGet's, then the Claude desktop app's", async () => {
    const m = machine();
    const desktop = desktopCopy(m, "2.1.295", "bbbb", whole());
    assert.equal(found(await lane(m))?.[1], desktop);
    const winget = program(join(m.localAppData, "Microsoft", "WinGet", "Links", "claude.exe"));
    assert.equal(found(await lane(m))?.[1], winget);
    const native = program(join(m.home, ".local", "bin", "claude.exe"));
    assert.equal(found(await lane(m))?.[1], native);
    const onPath = program(join(m.pathDir, "claude.exe"));
    const last = await lane(m);
    assert.equal(found(last)?.[1], onPath);
    assert.equal(found(last)?.[2], "the PATH");
  });

  it("never runs a .cmd shim: one on the PATH is passed over", async () => {
    const m = machine();
    program(join(m.pathDir, "claude.cmd"), "@echo off");
    const desktop = desktopCopy(m, "2.1.295", "bbbb", whole());
    assert.equal(found(await lane(m))?.[1], desktop);
  });

  it("says in plain words that Claude Code was not found, at its start and in the session that could not start", async () => {
    const m = machine();
    const r = await lane(m);
    assert.match(r.text(), /Claude Code was not found on this PC: install it, or open the Claude desktop app once/);
    assert.equal(found(r), null);
    await r.wake("analysis_asked", "no-claude");
    await waitFor(() => /run end .*err="Claude Code was not found/.test(r.text()), 15000, "the session's end");
    await waitFor(() => /run failed; it starts no chain/.test(r.text()), 15000, "the failure to be logged");
  });

  it("looks again whenever a session starts, so a copy the app has updated since is the one that runs, and says so", async () => {
    const m = machine();
    const before = desktopCopy(m, "2.1.293", "aaaa", whole());
    const r = await lane(m);
    assert.equal(found(r)?.[1], before);
    await r.wake("analysis_asked", "one");
    // What the "copy" is (a file that is no program) fails to start; how it fails does not matter here.
    await waitFor(() => /run end|run failed: spawn/.test(r.text()), 15000, "the first session's end");
    const after = desktopCopy(m, "2.1.295", "bbbb", whole());
    rmSync(join(m.desktop, "2.1.293"), { recursive: true, force: true });
    await waitFor(async () => (await r.health()).running === false);
    await r.wake("analysis_asked", "two");
    await waitFor(() => r.text().includes(`claude is ${after} (from the Claude desktop app, 2.1.295)`), 15000, "the new copy");
    assert.equal(r.text().split("claude is ").length - 1, 2, "said at the start and when it changed, not on every session");
  });
});

describe("ending a session on Windows", { skip: !WINDOWS }, () => {
  // A session that starts a process of its own and then idles; the app answers `stop` to the first heartbeat of the session.
  const hanging = async (tag, { env = {}, stop = false } = {}) => {
    let served = 0;
    let r = null;
    const app = await fakeApp((p, q, body) => {
      if (p.endsWith("/work")) return { next: served++ === 0 ? "analysis" : null };
      // The stop comes once the session has recorded its call, which it does after it has started its own process: a stop that
      // came while the session was still starting would end it before that process exists, and the process would outlive it.
      if (p.endsWith("/heartbeat") && stop && body?.state === "alive" && r?.sessions().length === 1) return { ok: true, stop: true };
      return { ok: true };
    });
    r = await startRunner({ tag, app, env: { FAKE_MODE: "hang", SCOUT_HEARTBEAT_SEC: "0.3", ...env } });
    await r.wake("analysis_asked", "hang");
    await waitFor(() => r.sessions().length === 1 && alive(r.sessions()[0].grandchild), 15000, "the session and its process");
    return { app, r, session: r.sessions()[0] };
  };
  const gone = (...pids) => waitFor(() => pids.every((pid) => !alive(pid)), 10000, `processes ${pids.join(", ")} to end`);

  it("ends the whole tree when the app says stop: the session and the process it started, and nothing else", async () => {
    const { r, session } = await hanging("stop-reply", { stop: true });
    await waitFor(() => r.text().includes("run stopped: Claude's peak hours are blacked out"), 15000, "the stop");
    await gone(session.pid, session.grandchild);
    await waitFor(async () => (await r.health()).running === false);
    assert.ok(alive(r.child.pid), "the lane itself goes on");
    assert.equal(r.sessions().length, 1, "no chain after a stopped session");
  });

  it("ends the whole tree when the run limit comes", async () => {
    const { r, session } = await hanging("timeout", { env: { SCOUT_RUN_TIMEOUT_MIN: "0.03" } });
    await waitFor(() => r.text().includes("run timed out after"), 15000, "the timeout");
    await gone(session.pid, session.grandchild);
    await waitFor(async () => (await r.health()).running === false);
    assert.match(r.text(), /TIMED OUT/);
    assert.ok(alive(r.child.pid));
  });

  it("ends a session with its lane when the lane itself is ended any way at all: Node starts the session in a job object that closes with the lane", async () => {
    const { r, session } = await hanging("lane-ended");
    try {
      // Windows has no SIGTERM to send, and ending a process there runs nothing of its own: this is how Task Manager ends one.
      r.child.kill();
      await gone(session.pid, r.child.pid);
    } finally {
      // What the session started is not in that job (a process started by a member of a Node job is not a member), and nothing
      // here ends it: the session's own `taskkill /T` is what does, when it is ended by the lane (the tests above).
      try { process.kill(session.grandchild); } catch { /* already gone */ }
    }
  });
});

describe("the five lanes", () => {
  // The data folder a launcher run uses, made ready: the secrets the app is given, and the settings that make it a test.
  async function ready(tag, { token = "file-token", env = {} } = {}) {
    const local = folder(tag);
    const platformEnv = { LOCALAPPDATA: local, XDG_DATA_HOME: local, HOME: local, USERPROFILE: local };
    const data = dataFolder(platformEnv, process.platform, local);
    mkdirSync(join(data, "secrets"), { recursive: true });
    writeFileSync(join(data, "secrets", "agent-token"), "agent-token-value");
    writeFileSync(join(data, "secrets", "wake-secret"), SECRET);
    if (token) writeFileSync(join(data, "secrets", "claude-oauth-token"), token);
    const base = await portsInARow(LANES.length);
    const calls = join(local, "calls.jsonl");
    const spy = join(local, "spy.jsonl");
    return {
      local, data, base, calls, spy,
      env: withoutUndefined({
        ...cleanEnv(), ...platformEnv, SCOUT_PORT: String(base), SCOUT_CLAUDE_BIN: join(root, "fake-claude.cjs"),
        SCOUT_CHECK_IN_MIN: "0.02", SCOUT_PRESENCE_SEC: "3600", SCOUT_MODEL: "model-test", SCOUT_EFFORT: "medium",
        FAKE_CALLS: calls, SPAWN_SPY: spy, ...env,
      }),
    };
  }
  // The launcher itself, with what it prints.
  function launch(setup, env = {}) {
    const child = spawnTracked(process.execPath, [LAUNCHER], { env: { ...setup.env, ...env }, stdio: ["ignore", "pipe", "pipe"] });
    let output = "";
    child.stdout.on("data", (d) => { output += d; });
    child.stderr.on("data", (d) => { output += d; });
    return { child, output: () => output, pids: () => [...output.matchAll(/started, pid (\d+)/g)].map((m) => Number(m[1])) };
  }
  const refused = async (setup, env = {}) => {
    const run = launch(setup, env);
    assert.equal(await exited(run.child), 1);
    return run.output();
  };

  it("starts the five lanes on this machine; each takes its kind of work, runs a session in its own folders at the app's address, and submits", async () => {
    // The app names a kind of job no session has started yet, whichever session asks: a pasted posting, an employer to research,
    // a role to research and, once the search is woken, the search.
    const wanted = [["parser", "jobs_parse_next"], ["researcher", "jobs_employer"], ["analysis", "jobs_analysis"], ["search", "jobs_profile"]];
    let woken = false;
    const started = () => lines(setup.calls).map((c) => c.argv[c.argv.indexOf("--allowedTools") + 1].replace("mcp__jobsearch__", ""));
    const app = await fakeApp((p, q) => {
      if (p.endsWith("/instructions")) return { files: {} };
      if (!p.endsWith("/api/v1/agent/work")) return { ok: true };
      if (q.only !== "any") return { next: null };
      const open = wanted.filter(([kind, tool]) => !started().includes(tool) && (kind !== "search" || woken));
      return { next: open.length ? open[0][0] : null };
    });
    const setup = await ready("five", { env: { RESUMEBOT_APP_URL: app.url, NODE_OPTIONS: `--import=${pathToFileURL(join(root, "spy.mjs")).href}` } });
    const run = launch(setup);
    await waitFor(() => run.pids().length === 5, 20000, "five lanes to start");
    const ports = LANES.map((_, i) => setup.base + i);
    for (const port of ports) await waitFor(async () => (await fetch(`http://127.0.0.1:${port}/health`, { headers: { Connection: "close" } }).then((r) => r.json(), () => null))?.ok === true, 20000, `a lane on ${port}`);
    const lanePids = run.pids();
    // The search lane is woken, as the app wakes it: a signed wake on the port the launcher names, with the secrets file's secret.
    woken = true;
    const body = JSON.stringify({ mode: "scout", reason: "search_started", project_id: "search-1" });
    const timestamp = String(Math.floor(Date.now() / 1000));
    const signature = createHmac("sha256", SECRET).update(`${timestamp}.`).update(body).digest("hex");
    assert.equal((await fetch(`http://127.0.0.1:${ports[0]}/wake`, {
      method: "POST", body, headers: { Connection: "close", "Content-Type": "application/json", "X-Webhook-Timestamp": timestamp, "X-Webhook-Signature-V2": signature },
    })).status, 202);
    await waitFor(() => wanted.every(([, tool]) => started().includes(tool)), 30000, "a session of each kind");
    const sessions = lines(setup.calls).map((call) => [call.runner.split("/")[0], call]);
    assert.ok(sessions.every(([runner]) => /^session-[1-5]$/.test(runner)), "each ran in one of the five sessions");
    const folderOf = Object.fromEntries(LANES.map((l) => [l.runner, l.folder]));
    assert.ok(new Set(started()).size >= 4, "any session ran any kind, with that kind's tools");
    for (const [runner, call] of sessions) {
      assert.ok(sameFolder(call.cwd, join(setup.data, "work", folderOf[runner])), `${runner} works in its own folder`);
      assert.equal(call.configDir, join(setup.data, "claude", folderOf[runner]), `${runner} has its own Claude folder`);
      assert.equal(call.token, "file-token");
      assert.equal(call.apiKey, null);
      assert.ok(call.argv.includes("--strict-mcp-config"));
    }
    // They called the app at the address the settings gave, with the token the secrets folder holds, each as itself.
    await waitFor(() => app.seen.filter((s) => s.path === "/mcp").length >= 4, 15000, "calls at the door");
    for (const call of app.seen.filter((s) => s.path === "/mcp")) {
      assert.equal(call.headers.authorization, "Bearer agent-token-value");
      assert.match(call.headers["x-runner"], /^session-[1-5]\/[0-9a-f]{8}$/);
      assert.match(call.headers["x-session-id"], /^[a-z]+-[0-9a-f]{12}$/);
    }
    const asked = app.seen.filter((s) => s.path.endsWith("/work")).map((s) => s.query);
    assert.ok(asked.every((q) => /^\?only=any&runner=session-[1-5]$/.test(q)), "every session asks for the next job of any kind, naming itself");
    // Their folders and logs are where the launcher said.
    for (const lane of LANES) {
      assert.ok(existsSync(join(setup.data, "logs", lane.log)), `${lane.runner}'s log`);
      assert.ok(existsSync(join(setup.data, "work", lane.folder)));
    }
    assert.match(readFileSync(join(setup.data, "logs", "lanes.log"), "utf8"), /lane session-5 started/);

    // Every process was started with no console window.
    const spawns = spied(setup.spy);
    assert.ok(spawns.length >= LANES.length + wanted.length, `the launcher's five lanes and a session of each kind were seen: ${spawns.length}`);
    for (const s of spawns) assert.equal(s.windowsHide, true, `${s.name} ${s.first}`);

    // Stopping. On Windows the launcher cannot be asked: it is ended, which ends everything Node started under it. Elsewhere it
    // is told to stop and ends the lanes itself.
    // A session that has submitted ends by itself a moment later, and its number may by then be another process's (Windows hands
    // a number out again within seconds, more so with many processes starting), so only the sessions still at work are asked
    // about: those are the ones the launcher's end has to take with it.
    const submitted = new Set(app.seen.filter((s) => s.path === "/mcp").map((s) => s.body?.submit));
    const sessionPids = sessions.map(([, c]) => c.pid).filter((pid) => !submitted.has(pid));
    if (WINDOWS) run.child.kill();
    else run.child.kill("SIGTERM");
    await waitFor(() => lanePids.every((pid) => !alive(pid)), 20000, "the lanes to end");
    await waitFor(() => sessionPids.every((pid) => !alive(pid)), 10000, "the sessions to end");
    for (const port of ports) await waitFor(() => nothingListens(port), 10000, `the lane's port ${port} to be released`);
  });

  it("ends the lanes and the sessions they are running when the launcher is told to stop", { skip: WINDOWS }, async () => {
    const app = await fakeApp((p, q) => (p.endsWith("/work") ? { next: q.only === "any" ? "analysis" : null } : { ok: true }));
    const setup = await ready("told", { env: { RESUMEBOT_APP_URL: app.url, FAKE_MODE: "hang" } });
    const run = launch(setup);
    // Every lane asks for work at once and the app names the same kind to each, so up to five sessions hang here.
    await waitFor(() => lines(setup.calls).length >= 1, 30000, "a session");
    const lanePids = run.pids();
    run.child.kill("SIGTERM");
    assert.equal(await exited(run.child), 0);
    const sessionPids = lines(setup.calls).map((call) => call.pid);
    await waitFor(() => lanePids.every((pid) => !alive(pid)) && sessionPids.every((pid) => !alive(pid)), 15000, "everything to end");
  });

  it("starts a lane again when it ends by itself, and none again once it is stopped", async () => {
    const setup = await ready("restart", { env: { RESUMEBOT_APP_URL: "http://127.0.0.1:9" } });
    const settings = readSettings({ env: setup.env, home: setup.local });
    assert.deepEqual(await checkStart(settings), []);
    const said = [];
    const running = startLanes(settings, { env: setup.env, say: (line) => said.push(line), restartMs: 200 });
    const upOn = async (port) => (await fetch(`http://127.0.0.1:${port}/health`, { headers: { Connection: "close" } }).then((r) => r.json(), () => null))?.ok === true;
    try {
      for (const lane of running.lanes) await waitFor(() => upOn(lane.port), 20000, `${lane.runner} to listen`);
      const [, victim] = running.lanes;
      const first = victim.child.pid;
      victim.child.kill();
      await waitFor(() => victim.child.pid !== first, 10000, "the lane to be started again");
      await waitFor(() => upOn(victim.port), 20000, "the new lane to listen");
      assert.match(said.join("\n"), /lane session-2 ended .*starting it again in 0s/);
    } finally {
      await running.stop();
    }
    await waitFor(() => running.lanes.every((l) => !alive(l.child.pid)), 10000, "every lane to end");
    const after = running.lanes.map((l) => l.child.pid);
    await sleep(600);
    assert.deepEqual(running.lanes.map((l) => l.child.pid), after, "none is started again after stop()");
  });

  it("will not start when a port is taken (it is probably running already), naming it", async () => {
    const setup = await ready("port-taken");
    const holder = createNetServer();
    await new Promise((resolve) => holder.listen(setup.base + 2, "127.0.0.1", resolve));
    try {
      const output = await refused(setup);
      assert.match(output, new RegExp(`not starting: port ${setup.base + 2} \\(the session-3 lane's\\) is in use on 127\\.0\\.0\\.1: are the lanes running already\\?`));
      assert.doesNotMatch(output, /started, pid/);
    } finally {
      holder.close();
    }
  });

  it("will not start without the app's token and secret, naming the folder that lacks them", async () => {
    const setup = await ready("no-secrets");
    rmSync(join(setup.data, "secrets", "agent-token"));
    rmSync(join(setup.data, "secrets", "wake-secret"));
    const output = await refused(setup);
    assert.match(output, /not starting: agent-token is not in .*secrets: it is the same file the app is given/);
    assert.match(output, /not starting: wake-secret is not in /);
  });

  it("moves what a lane's working folder holds to work-leftovers and starts anyway", async () => {
    const setup = await ready("stray-work");
    const work = join(setup.data, "work", "writer");
    mkdirSync(work, { recursive: true });
    writeFileSync(join(work, "CLAUDE.md"), "not for a lane");
    const said = [];
    const settings = readSettings({ env: setup.env, home: setup.local });
    assert.deepEqual(await checkStart(settings, (l) => said.push(l)), []);
    assert.deepEqual(readdirSync(work), []);
    assert.equal(said.length, 1);
    assert.match(said[0], /moved 1 stray file\(s\) out of .*writer \(CLAUDE\.md\) to .*work-leftovers/);
    const left = join(setup.data, "work-leftovers", "writer");
    assert.equal(readFileSync(join(left, readdirSync(left)[0], "CLAUDE.md"), "utf8"), "not for a lane");
    assert.equal(moveStrays(work), "");
  });

  it("prunes the lanes' session files older than 7 days and nothing else, and says when the nightly prune is", () => {
    const data = mkdtempSync(join(tmpdir(), "resumebot-prune-"));
    try {
      const day = 86_400_000;
      const now = Date.now();
      const made = (rel, ageDays) => {
        const file = join(data, "claude", ...rel);
        mkdirSync(dirname(file), { recursive: true });
        writeFileSync(file, "x");
        utimesSync(file, new Date(now - ageDays * day), new Date(now - ageDays * day));
        return file;
      };
      const old = made(["scout", "projects", "p1", "old.jsonl"], 8);
      const oldDeep = made(["analysis-1", "projects", "p2", "s", "tool-results", "big.txt"], 9);
      const fresh = made(["scout", "projects", "p1", "new.jsonl"], 6);
      const config = made(["scout", "settings.json"], 30); // not a session file: outside projects/
      const said = [];
      assert.equal(pruneTranscripts({ data, platform: process.platform }, { now, say: (l) => said.push(l) }), 2);
      assert.ok(!existsSync(old) && !existsSync(oldDeep));
      assert.ok(existsSync(fresh) && existsSync(config));
      assert.ok(!existsSync(join(data, "claude", "analysis-1", "projects", "p2")), "the folders it emptied go too");
      assert.match(said[0], /removed 2 lane transcript file\(s\) older than 7 days/);
      assert.equal(pruneTranscripts({ data, platform: process.platform }, { now }), 0, "a second pass finds nothing");
      const wait = untilNightly(new Date(2026, 9, 16, 12, 0));
      assert.equal(wait, 15.5 * 3_600_000);
      assert.equal(untilNightly(new Date(2026, 9, 16, 2, 0)), 1.5 * 3_600_000);
    } finally {
      rmSync(data, { recursive: true, force: true });
    }
  });

  it("will not start pointed at an app on another machine: the lanes send it their token", async () => {
    const setup = await ready("far-app");
    const output = await refused(setup, { RESUMEBOT_APP_URL: "http://192.168.1.20:8430" });
    assert.match(output, /RESUMEBOT_APP_URL must be an address on this machine/);
    assert.doesNotMatch(output, /started, pid/);
  });

  it("keeps the lanes' settings in the data folder's .env: the app's address, the port, the lanes' own settings, under the environment", async () => {
    let served = 0;
    const app = await fakeApp((p, q) => (p.endsWith("/work") ? { next: q.only === "any" && q.runner === "session-1" && served++ === 0 ? "analysis" : null } : { ok: true }));
    const setup = await ready("dotenv", { env: { SCOUT_PORT: undefined, RESUMEBOT_APP_URL: undefined, SCOUT_MODEL: undefined, SCOUT_EFFORT: undefined } });
    writeFileSync(join(setup.data, ".env"), [
      `RESUMEBOT_APP_URL=${app.url}`, `SCOUT_PORT=${setup.base}`, "SCOUT_MODEL=model-from-file", "SCOUT_EFFORT=low", "SCOUT_MAX_TURNS=11",
      "JOBSEARCH_OWNER=the app's own setting, not a lane's",
    ].join("\n"));
    const run = launch(setup, { SCOUT_MAX_TURNS: "12" });
    await waitFor(() => lines(setup.calls).length === 1, 30000, "a session");
    const [call] = lines(setup.calls);
    assert.equal(call.runner.split("/")[0], "session-1", "the lanes found the app at the file's address");
    assert.equal(arg(call, "--model"), "model-from-file");
    assert.equal(arg(call, "--effort"), "low");
    assert.equal(arg(call, "--max-turns"), "12", "the environment's setting over the file's");
    assert.match(run.output(), new RegExp(`the first session takes the app's wake at http://127\\.0\\.0\\.1:${setup.base}/wake`));
    if (WINDOWS) run.child.kill();
    else run.child.kill("SIGTERM");
    await exited(run.child);
  });
});

// A made-up answer: distinctive, so that finding it in a log is certain.
const ANSWER_WORDS = "zq-answer-text-5521-never-in-a-log";

// A preload that throws from a timer in the process named `FAKE_THROW_IN` (a file name), with a message that holds a session's words.
const THROWER = `
import path from "node:path";
if (path.basename(process.argv[1] || "") === process.env.FAKE_THROW_IN) setTimeout(() => { throw new Error(process.env.FAKE_WORDS); }, 400);
`;
const preload = () => {
  const file = join(root, "thrower.mjs");
  writeFileSync(file, THROWER);
  return `--import ${pathToFileURL(file).href}`;
};

describe("what a lane logs when a session fails or the runner crashes (the log lines work, 2026-11-16)", () => {
  for (const mode of ["answer-error", "garble"]) {
    it(`logs a reason and lengths, never the words, of a session that ends ${mode === "garble" ? "with an unreadable answer" : "with an error answer"}`, async () => {
      const r = await startRunner({ tag: `words-${mode}`, env: { FAKE_MODE: mode, FAKE_WORDS: ANSWER_WORDS } });
      await r.wake("analysis_asked", "analysis-1");
      await waitFor(() => /run failed; it starts no chain/.test(r.text()), 15000, "the failure to be logged");
      const end = r.text().split("\n").find((l) => /run end /.test(l));
      assert.match(end, mode === "garble" ? /reason=unreadable-answer out_len=\d+ err_len=\d+/ : /reason=exit-1 result_len=\d+/);
      assert.ok(!r.text().includes(ANSWER_WORDS), "Claude's words are not in the log");
    });
  }

  it("leaves one line for an uncaught exception, with the error's name and not its message, and exits non-zero", async () => {
    const r = await startRunner({ tag: "uncaught", env: { NODE_OPTIONS: preload(), FAKE_THROW_IN: "scout.mjs", FAKE_WORDS: ANSWER_WORDS } });
    const code = await exited(r.child);
    assert.notEqual(code, 0);
    const found = r.text().split("\n").filter((l) => /uncaught exception/.test(l));
    assert.equal(found.length, 1);
    assert.match(found[0], /uncaught exception in lane \S+: Error$/);
    assert.ok(!r.text().includes(ANSWER_WORDS));
  });

  it("does the same in the launcher: one line in lanes.log, no message, a non-zero exit", async () => {
    const local = folder("launcher-crash");
    const platformEnv = { LOCALAPPDATA: local, XDG_DATA_HOME: local, HOME: local, USERPROFILE: local };
    const data = dataFolder(platformEnv, process.platform, local);
    mkdirSync(join(data, "secrets"), { recursive: true });
    writeFileSync(join(data, "secrets", "agent-token"), "agent-token-value");
    writeFileSync(join(data, "secrets", "wake-secret"), SECRET);
    const base = await portsInARow(LANES.length);
    const env = withoutUndefined({
      ...cleanEnv(), ...platformEnv, SCOUT_PORT: String(base), SCOUT_CLAUDE_BIN: join(root, "fake-claude.cjs"),
      NODE_OPTIONS: preload(), FAKE_THROW_IN: "lanes-local.mjs", FAKE_WORDS: ANSWER_WORDS,
    });
    const child = spawnTracked(process.execPath, [LAUNCHER], { env, stdio: ["ignore", "pipe", "pipe"] });
    let output = "";
    child.stdout.on("data", (d) => { output += d; });
    child.stderr.on("data", (d) => { output += d; });
    assert.notEqual(await exited(child), 0);
    const logged = readFileSync(join(data, "logs", "lanes.log"), "utf8");
    assert.equal(logged.split("\n").filter((l) => /uncaught exception/.test(l)).length, 1);
    assert.match(logged, /uncaught exception in the lanes launcher: Error\n/);
    assert.ok(!logged.includes(ANSWER_WORDS) && !output.includes(ANSWER_WORDS));
  });
});

describe("the interim runner", () => {
  const source = (file) => readFileSync(join(HERE, file), "utf8");

  it("leave scout.mjs a file of its own: it imports nothing but Node's own modules, so the image that carries it alone still starts", () => {
    const imports = [...source("scout.mjs").matchAll(/^import .* from "([^"]+)";$/gm)].map((m) => m[1]);
    assert.ok(imports.length > 0);
    for (const specifier of imports) assert.match(specifier, /^node:/, `scout.mjs imports ${specifier}`);
  });
});

describe("the bridge's register is the app's (docs/PRD.md path 52)", () => {
  const recorder = (answer = { ok: true }) => {
    const calls = [];
    const fetchImpl = async (url, init) => {
      calls.push({ url, method: init.method, auth: init.headers.Authorization, body: init.body ? JSON.parse(init.body) : null });
      return { ok: true, json: async () => answer };
    };
    return { calls, fetchImpl };
  };

  it("tells the app through the agent's own door, with the token read each time", async () => {
    const { calls, fetchImpl } = recorder();
    let token = "first";
    const register = appRegister({ appUrl: "http://127.0.0.1:8430", token: () => token, fetchImpl });
    await register.opened("T1", "reader-1", "session");
    token = "second";
    await register.removed("T 1");
    await register.released("reader-1");
    assert.deepEqual(calls.map((c) => [c.method, c.url.replace("http://127.0.0.1:8430/api/v1/agent/browser/tabs", ""), c.auth]), [
      ["POST", "", "Bearer first"], ["DELETE", "/T%201", "Bearer second"], ["POST", "/release", "Bearer second"],
    ]);
    assert.deepEqual(calls[0].body, { target_id: "T1", owner_id: "reader-1", kind: "session" });
  });

  it("answers the tabs to keep, and null when the app says nothing usable, is not there or has no token", async () => {
    const kept = recorder({ keep: ["A", "B"] });
    assert.deepEqual(await appRegister({ appUrl: "http://x", token: "t", fetchImpl: kept.fetchImpl }).reconcile(), ["A", "B"]);
    const odd = recorder({ nope: 1 });
    assert.equal(await appRegister({ appUrl: "http://x", token: "t", fetchImpl: odd.fetchImpl }).reconcile(), null);
    const down = async () => { throw new Error("ECONNREFUSED"); };
    assert.equal(await appRegister({ appUrl: "http://x", token: "t", fetchImpl: down }).reconcile(), null);
    const refused = async () => ({ ok: false, json: async () => ({}) });
    assert.equal(await appRegister({ appUrl: "http://x", token: "t", fetchImpl: refused }).reconcile(), null);
    const none = recorder();
    assert.equal(await appRegister({ appUrl: "http://x", token: () => "", fetchImpl: none.fetchImpl }).reconcile(), null);
    assert.equal(none.calls.length, 0, "no token, no call");
  });

  // What the bridge asks before it closes the app's Chrome for want of work (PRD 52.9, the owner 2026-11-04).
  it("asks the app what the browser queue holds with the agent's token, and answers null unless it says both counts", async () => {
    const held = recorder({ ok: true, waiting: 0, leased: 2 });
    assert.deepEqual(await appRegister({ appUrl: "http://x", token: "t", fetchImpl: held.fetchImpl }).busy(), { waiting: 0, leased: 2 });
    assert.deepEqual(held.calls.map((c) => [c.method, c.url, c.auth]), [["GET", "http://x/api/v1/agent/browser/busy", "Bearer t"]]);
    for (const odd of [{ ok: true }, { waiting: "0", leased: 0 }, { waiting: 0 }, { waiting: 1.5, leased: 0 }]) {
      assert.equal(await appRegister({ appUrl: "http://x", token: "t", fetchImpl: recorder(odd).fetchImpl }).busy(), null, JSON.stringify(odd));
    }
    const down = async () => { throw new Error("ECONNREFUSED"); };
    assert.equal(await appRegister({ appUrl: "http://x", token: "t", fetchImpl: down }).busy(), null, "the app not there");
    const refused = async () => ({ ok: false, json: async () => ({ waiting: 0, leased: 0 }) });
    assert.equal(await appRegister({ appUrl: "http://x", token: "t", fetchImpl: refused }).busy(), null, "the app refuses");
    const none = recorder({ waiting: 0, leased: 0 });
    assert.equal(await appRegister({ appUrl: "http://x", token: () => "", fetchImpl: none.fetchImpl }).busy(), null);
    assert.equal(none.calls.length, 0, "no token, no call");
  });
});

// The owner, 2026-11-05: the browser jobs' window's first tab is a page the app serves, at the address the launcher already holds
// for the app (docs/PRD.md 52.8). The bridge is told that address and no other.
describe("the browser bridge is told the app's address, for the page its first tab shows (2026-11-05)", () => {
  const ENOENT = Object.assign(new Error("no such file"), { code: "ENOENT" });
  const settingsFor = (env) => readSettings({ env: { XDG_DATA_HOME: "/x", ...env }, platform: "linux", home: "/h", read: () => { throw ENOENT; }, here: "/c/s" });

  it("is given the launcher's own setting for the app's address, whatever it is, and names the app's page there", () => {
    for (const [env, origin] of [[{}, "http://127.0.0.1:8430"], [{ RESUMEBOT_APP_URL: "http://localhost:9911/" }, "http://localhost:9911"]]) {
      const settings = settingsFor(env);
      const options = bridgeOptions(settings, 9333);
      assert.equal(options.appUrl, settings.pageUrl, "with no page address given, the setting the lanes are pointed at");
      assert.equal(options.appUrl, origin);
      assert.equal(anchorUrl(options.appUrl), `${origin}${WINDOW_PAGE_PATH}`);
    }
  });

  it("shows the window's first tab the page, not the engine the lanes are pointed at (RESUMEBOT_PAGE_URL)", () => {
    const settings = settingsFor({ RESUMEBOT_APP_URL: "http://127.0.0.1:8530", RESUMEBOT_PAGE_URL: "http://127.0.0.1:8430" });
    assert.equal(settings.appUrl, "http://127.0.0.1:8530");
    assert.equal(settings.pageUrl, "http://127.0.0.1:8430");
    const options = bridgeOptions(settings, 9333);
    assert.equal(options.appUrl, "http://127.0.0.1:8430", "the bridge is told the page");
    assert.equal(anchorUrl(options.appUrl), `http://127.0.0.1:8430${WINDOW_PAGE_PATH}`);
    const env = laneEnv(settings, LANES[0], 0, { env: {} });
    assert.equal(env.RESUMEBOT_API_URL, "http://127.0.0.1:8530", "the lanes' API is the engine");
    assert.equal(env.RESUMEBOT_MCP_URL, "http://127.0.0.1:8530/mcp");
  });

  it("refuses a page address that is not this machine's, as it does the app's", () => {
    assert.throws(() => settingsFor({ RESUMEBOT_PAGE_URL: "http://192.168.1.20:8430" }), /RESUMEBOT_PAGE_URL must be an address on this machine/);
  });

  it("keeps what the bridge was started with before: the port, which Chrome and whose profile, a log line and the app's register", async () => {
    const settings = settingsFor({ SCOUT_BROWSER: "own", SCOUT_CHROME_BIN: "/opt/chrome" });
    const lines = [];
    const options = bridgeOptions(settings, 9400, (line) => lines.push(line));
    assert.equal(options.port, 9400);
    assert.equal(options.mode, "own");
    assert.equal(options.profile, settings.browserProfile);
    assert.equal(options.chromeBin, "/opt/chrome");
    options.log("a line");
    assert.deepEqual(lines, ["browser: a line"]);
    assert.deepEqual(Object.keys(options.register).sort(), ["busy", "opened", "reconcile", "released", "removed"]);
  });
});

// The owner, 2026-11-14 (docs/PRD.md path 52): the bridge serves only clients that carry the app's key, which the launcher makes.
describe("the browser bridge's key (2026-11-14)", () => {
  const settingsIn = (secrets) => ({ secrets });

  it("is made once, 32 bytes of hex, in the secrets folder, and kept across restarts", () => {
    const secrets = mkdtempSync(join(tmpdir(), "bridge-key-"));
    try {
      const lines = [];
      const first = ensureBridgeKey(settingsIn(secrets), (line) => lines.push(line));
      assert.match(first, /^[0-9a-f]{64}$/);
      assert.equal(readFileSync(join(secrets, "bridge-key"), "utf8"), first);
      assert.equal(ensureBridgeKey(settingsIn(secrets), (line) => lines.push(line)), first, "a restart keeps it");
      assert.equal(readBridgeKey(settingsIn(secrets)), first);
      assert.equal(lines.length, 1, "said once, when made");
      assert.ok(lines.every((line) => !line.includes(first)), "the key itself is never logged");
    } finally { rmSync(secrets, { recursive: true, force: true }); }
  });

  it("is what the bridge is made with, and an install with none yet gives the bridge none (it then refuses everything)", () => {
    const secrets = mkdtempSync(join(tmpdir(), "bridge-key-"));
    try {
      const settings = { ...readSettings({ env: { XDG_DATA_HOME: "/x" }, platform: "linux", home: "/h", read: () => { throw Object.assign(new Error("no"), { code: "ENOENT" }); }, here: "/c/s" }), secrets };
      assert.equal(bridgeOptions(settings, 9333).key, "");
      const made = ensureBridgeKey(settings);
      assert.equal(bridgeOptions(settings, 9333).key, made);
    } finally { rmSync(secrets, { recursive: true, force: true }); }
  });
});
