// resumebot's five lanes on a local computer: five session-pool runners (see LANES below), each one `scout.mjs` under this
// Node, listening on 127.0.0.1 and pointed at the app on this machine.
//
// This launcher and the edits to scout.mjs that Windows needs (the loopback address, ending a session's whole process
// tree, no console window, the lane's own Claude folder and working folder, finding the user's claude.exe each time) are
// an interim runner for the lanes.
//
//   node app/scout/lanes-local.mjs
//
// It runs until it is ended. Ctrl+C (or SIGTERM) stops every lane and the sessions they started, with whatever those started.
// Ended any other way (Task Manager, `taskkill`, a crash) the lanes and their sessions end with it on Windows: Node puts what
// it starts in a job object that closes with it (tested: lanes-local.test.mjs); what a session itself started is not in that
// job and may stay. A lane that ends by itself is started again after 5 seconds (twice as long each time it fails again
// within a minute, up to 5 minutes). It refuses to start when a port is taken (it is probably running already), when a
// lane's working folder has anything in it, or when the files it needs are not there.
//
// Settings, from the `.env` in the data folder (the file the app's settings come from too) and then the environment, which wins:
//   RESUMEBOT_APP_URL       where the lanes' app is, on this machine: http://127.0.0.1:8430 (stack.py sets the engine's address,
//                           the page's port plus 100). Another machine is refused.
//   RESUMEBOT_PAGE_URL      the owner's page, for the browser window's first tab (default: RESUMEBOT_APP_URL).
//   SCOUT_PORT              the search lane's port, where the app sends its wake (JOBSEARCH_WAKE_URL: http://127.0.0.1:8651/wake);
//                           the analysis lanes, the owner's and the writer's take the next four. The app is told it has two
//                           researchers (JOBSEARCH_ANALYSTS=2, which stack.py sets when it starts the app).
//   SCOUT_SECRETS_DIR       the folder of agent-token and wake-secret, the same files the app is given (its
//                           JOBSEARCH_AGENT_TOKEN_FILE and JOBSEARCH_WAKE_SECRET_FILE), and claude-oauth-token, the token
//                           from `claude setup-token`, stored by `resumebot signin`: how every install signs its lanes in (without
//                           the file a lane uses the login in its own Claude folder, which a new install does not have; a stored
//                           token overrides that login, so an expired or invalid one fails the lane). Default:
//                           secrets/ in the data folder, readable by this user only.
//   SCOUT_INSTRUCTIONS_DIR  the instruction files, if the app cannot give a lane the owner's version. Default: ../instructions.
//   SCOUT_BROWSER           which Chrome the browser workers use. `profile` (the default, the owner 2026-10-16): one visible Chrome on a profile of the install's own
//                           (SCOUT_BROWSER_PROFILE), which the bridge of own-chrome.mjs starts when a session needs it. `own`:
//                           the user's everyday Chrome, signed in to their accounts; this launcher starts no Chrome for
//                           it (remote debugging switched on once at chrome://inspect/#remote-debugging, and Chrome's Allow
//                           each time it starts). Either way the bridge holds one DevTools connection and shows each session
//                           only the tabs it opened, all in one "resumebot" window.
//   SCOUT_BROWSER_PROFILE   with `profile`, that profile's folder, which the owner signs in to themselves (`node
//                           app/scout/lanes-local.mjs open-browser-profile` opens it). Default: browser-profile in the data
//                           folder, made at the start.
//   SCOUT_BROWSER_PORT      the bridge's port, which every session attaches to, on 127.0.0.1 only. Unset: 9333, or any free
//                           port when 9333 is taken (a Chrome an older runner started on it, say); the lanes are told the one
//                           in use. Set: that port, or the launcher says it is taken and does not start.
//   SCOUT_CHROME_USER_DATA  with `own`, the owner's Chrome profile folder when it is not in its usual place.
//   SCOUT_CHROME_BIN        Chrome, when it is not in its usual place.
//   SCOUT_BROWSER_MCP       the DevTools MCP server those sessions start, as its npm package and version
//                           (chrome-devtools-mcp@<version>): a line of the .env, passed on as it is; without it scout.mjs
//                           starts the version it pins.
//   any other SCOUT_* or RESUMEBOT_* line of the file goes to the lanes as it is (SCOUT_MODEL, SCOUT_EFFORT, SCOUT_MAX_TURNS ...,
//   scout.mjs has the list).
//
// What the lanes are given, each its own (these folders and the session flags are the boundary): under the data folder, a working folder that holds nothing (work/<runner>), a Claude folder
// (claude/<runner>: the user's CLAUDE.md, hooks and memory are not in it), its MCP config (run/<runner>.mcp.json) and its log
// (logs/runs*.log); this launcher's own log is logs/lanes.log.
//
// The data folder is the one RESUMEBOT_DATA_DIR names (the app and stack.py read the same variable); with none named, Windows
// %LOCALAPPDATA%\resumebot, macOS ~/Library/Application Support/resumebot, Linux ~/.local/share/resumebot (XDG_DATA_HOME).
//
// Learned with Claude Code on Windows, for any other runner to carry over: a session with an open stdin pipe
// waits three seconds before it starts, so give it none; one started from a Claude Code session inherits that session's
// ANTHROPIC_* and CLAUDE_* variables (its route, its account, "a person is here", the bare mode), so remove them;
// CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 is what keeps a CLAUDE.md in a parent of the working folder out (without it one is read, with
// the folder's own CLAUDE_CONFIG_DIR set); claude.exe starts git.exe, which only `taskkill /T` ends with it; and the job object
// Node makes for what it starts lets what a process starts break away from it, so a runner's own Job Object must not.

import { spawn, spawnSync } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, readdirSync, readFileSync, renameSync, rmdirSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { randomBytes } from "node:crypto";
import { createServer } from "node:net";
import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { ownChromeBridge, profileChromeArgs, resetWindowPlacement } from "./own-chrome.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));

// The five pool sessions, each taking the most rightward job of any kind. `folder` names the session's own
// folders (as before the pool); `runner` is the name the Agents page shows ("Session 1"). The app's dispatcher commands each one that is idle (a signed wake on its own port, carrying the item it claimed); each also has a slow check-in of its own, the fallback for a command that was lost.
export const LANES = [
  { runner: "session-1", folder: "scout", log: "runs.log" },
  { runner: "session-2", folder: "analysis-1", log: "runs-analysis-1.log" },
  { runner: "session-3", folder: "analysis-2", log: "runs-analysis-2.log" },
  { runner: "session-4", folder: "owner", log: "runs-owner.log" },
  { runner: "session-5", folder: "writer", log: "runs-writer.log" },
];

const DEFAULT_APP_URL = "http://127.0.0.1:8430";
const DEFAULT_PORT = 8651;
// The shared browser's DevTools port, on 127.0.0.1 (scout.mjs has the same default).
const DEFAULT_BROWSER_PORT = 9333;

const pathFor = (platform) => (platform === "win32" ? path.win32 : path.posix);

// Where this user's resumebot keeps its data: the folder RESUMEBOT_DATA_DIR names (the app reads the same variable,
// `app/settings.py`), else the user's own by system. The one place that names it.
export function dataFolder(env = process.env, platform = process.platform, home = homedir()) {
  const p = pathFor(platform);
  if (env.RESUMEBOT_DATA_DIR?.trim()) return env.RESUMEBOT_DATA_DIR.trim();
  if (platform === "win32") return p.join(env.LOCALAPPDATA || p.join(home, "AppData", "Local"), "resumebot");
  if (platform === "darwin") return p.join(home, "Library", "Application Support", "resumebot");
  return p.join(env.XDG_DATA_HOME || p.join(home, ".local", "share"), "resumebot");
}

// The KEY=value lines of a .env file, as an object: blank lines and # comments skipped, `export ` and one pair of quotes allowed.
export function parseEnvFile(text) {
  const out = {};
  for (const raw of String(text).split(/\r?\n/)) {
    const m = /^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/.exec(raw);
    if (!m) continue;
    let value = m[2];
    const quoted = /^(["'])(.*)\1$/.exec(value);
    value = quoted ? quoted[2] : value.replace(/\s+#.*$/, "");
    out[m[1]] = value;
  }
  return out;
}

// The app's address, which must be this machine's: the lanes send it their token. Returns the origin without a trailing slash;
// throws a plain sentence for anything else. `name` is the setting it checks, for that sentence.
export function checkAppUrl(value, name = "RESUMEBOT_APP_URL") {
  let url;
  try { url = new URL(value); } catch { throw new Error(`${name} is not an address: ${value}`); }
  if (url.protocol !== "http:" && url.protocol !== "https:") throw new Error(`${name} must be an http address: ${value}`);
  const host = url.hostname;
  if (!(host === "localhost" || host === "[::1]" || /^127(?:\.\d{1,3}){3}$/.test(host))) {
    throw new Error(`${name} must be an address on this machine (127.0.0.1 or localhost); ${host} is not: the lanes send it their token`);
  }
  return url.origin;
}

// Everything the launcher is set by: the data folder's .env under the environment, and the paths that follow from them.
// `read` and `here` are for the tests.
export function readSettings({ env = process.env, platform = process.platform, home = homedir(), read = readFileSync, here = HERE } = {}) {
  const p = pathFor(platform);
  const data = dataFolder(env, platform, home);
  let file = {};
  try {
    file = parseEnvFile(read(p.join(data, ".env"), "utf8"));
  } catch (e) {
    if (e?.code !== "ENOENT") throw e;
  }
  const get = (name) => (env[name] ? env[name] : file[name] || "");
  const port = Number(get("SCOUT_PORT"));
  const browser = (get("SCOUT_BROWSER") || "profile").trim().toLowerCase();
  if (browser !== "own" && browser !== "profile") throw new Error(`SCOUT_BROWSER must be own or profile, not ${browser}`);
  // The SCOUT_* and RESUMEBOT_* lines of the file that the environment does not set: the lanes get them as they are.
  const passed = {};
  for (const [name, value] of Object.entries(file)) {
    if (/^(?:SCOUT|RESUMEBOT)_/.test(name) && !env[name]) passed[name] = value;
  }
  return {
    platform,
    data,
    appUrl: checkAppUrl(get("RESUMEBOT_APP_URL") || DEFAULT_APP_URL),
    // The owner's page, which the browser window's first tab shows. The lanes are pointed at the engine (appUrl) when the
    // launcher starts two programs, and the engine serves no page; with no page address given it is the app's.
    pageUrl: get("RESUMEBOT_PAGE_URL")
      ? checkAppUrl(get("RESUMEBOT_PAGE_URL"), "RESUMEBOT_PAGE_URL")
      : checkAppUrl(get("RESUMEBOT_APP_URL") || DEFAULT_APP_URL),
    basePort: Number.isInteger(port) && port > 0 && port + LANES.length - 1 < 65536 ? port : DEFAULT_PORT,
    secrets: get("SCOUT_SECRETS_DIR") || p.join(data, "secrets"),
    instructions: get("SCOUT_INSTRUCTIONS_DIR") || p.resolve(here, "..", "instructions"),
    scout: p.join(here, "scout.mjs"),
    browser,
    browserProfile: get("SCOUT_BROWSER_PROFILE") || p.join(data, "browser-profile"),
    browserPort: Number(get("SCOUT_BROWSER_PORT")) > 0 ? Number(get("SCOUT_BROWSER_PORT")) : DEFAULT_BROWSER_PORT,
    // Whether the install named the port: then only that one will do; else a free one stands in when it is taken.
    browserPortSet: Number(get("SCOUT_BROWSER_PORT")) > 0,
    chromeBin: get("SCOUT_CHROME_BIN"),
    // The owner's time zone (the app's JOBSEARCH_TIMEZONE), for the nightly prune's hour; empty when unset or not a zone name.
    timezone: zoneKnown(get("JOBSEARCH_TIMEZONE").trim()) ? get("JOBSEARCH_TIMEZONE").trim() : "",
    passed,
  };
}

// The environment of one lane's `scout.mjs`: this one's environment (the sessions' own filtering is scout.mjs's), the file's
// settings, and what makes it this lane: its name, port, folders and where the app is.
export function laneEnv(settings, lane, index, { env = process.env } = {}) {
  const p = pathFor(settings.platform);
  const own = (kind, name = lane.folder) => p.join(settings.data, kind, name);
  return {
    ...env,
    ...settings.passed,
    RESUMEBOT_MCP_URL: `${settings.appUrl}/mcp`,
    RESUMEBOT_API_URL: settings.appUrl,
    SCOUT_RUNNER: lane.runner,
    SCOUT_PORT: String(settings.basePort + index),
    SCOUT_BIND: "127.0.0.1",
    SCOUT_LOG: own("logs", lane.log),
    SCOUT_SECRETS_DIR: settings.secrets,
    SCOUT_INSTRUCTIONS_DIR: settings.instructions,
    SCOUT_MCP_CONFIG: own("run", `${lane.runner}.mcp.json`),
    SCOUT_WORKDIR: own("work"),
    SCOUT_CLAUDE_CONFIG_DIR: own("claude"),
    SCOUT_BROWSER: settings.browser,
    SCOUT_BROWSER_PROFILE: settings.browserProfile,
    SCOUT_BROWSER_PORT: String(settings.browserPort), // the bridge's, as it is listening (`startBridge`)
    SCOUT_BROWSER_BRIDGE: "1", // the sessions attach to this launcher's bridge, which starts the profile's Chrome itself
    ...(settings.platform === "win32" ? { SCOUT_JOB_WRAP: "1" } : {}), // each session in a Job Object (jobwrap.py, PRD 52.6)
  };
}

// Ends a lane and what it started. Windows has no signals: `taskkill /T /F` ends the process and every process under it (the
// lane's sessions and what they started). Elsewhere the lane is asked to stop, and stops its session itself.
// scout.mjs has the same function and stands alone, so they are not shared.
export function killTree(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform !== "win32") {
    child.kill("SIGTERM");
    return;
  }
  if (!child.pid) return;
  const taskkill = path.join(process.env.SystemRoot || "C:\\Windows", "System32", "taskkill.exe");
  const ended = spawnSync(taskkill, ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore", timeout: 10_000 });
  if (ended.status !== 0 && ended.status !== 128) child.kill();
}

function portIsFree(port) {
  return new Promise((resolve) => {
    const probe = createServer().once("error", () => resolve(false));
    probe.listen(port, "127.0.0.1", () => probe.close(() => resolve(true)));
  });
}

// A lane's working folder must hold nothing (Claude Code would read it as the project's own), but a browser session can leave
// files in it (a saved page snapshot, a scratch file). They are moved, never deleted, to <data>/work-leftovers/<lane>/<UTC time>/
// and named in one log line. Returns the folder they went to, or "" when there was nothing.
export function moveStrays(work, say = () => {}) {
  const names = readdirSync(work);
  if (!names.length) return "";
  const to = path.join(path.dirname(path.dirname(work)), "work-leftovers", path.basename(work), new Date().toISOString().replace(/[:.]/g, "-"));
  mkdirSync(to, { recursive: true });
  for (const name of names) renameSync(path.join(work, name), path.join(to, name));
  say(`moved ${names.length} stray file(s) out of ${work} (${names.slice(0, 5).join(", ")}${names.length > 5 ? ", ..." : ""}) to ${to}`);
  return to;
}

// The lanes' Claude Code session files (<data>/claude/<lane>/projects/...): about 450 MB a day, never read again. Every
// file there older than `days` is deleted, and the folders it leaves empty. Returns how many files went.
export function pruneTranscripts(settings, { days = 7, now = Date.now(), say = () => {} } = {}) {
  const p = pathFor(settings.platform);
  const cutoff = now - days * 86_400_000;
  let removed = 0;
  const walk = (dir) => {
    let names;
    try { names = readdirSync(dir); } catch { return; }
    for (const name of names) {
      const file = p.join(dir, name);
      try {
        const info = statSync(file);
        if (info.isDirectory()) {
          walk(file);
          if (!readdirSync(file).length) rmdirSync(file);
        } else if (info.mtimeMs < cutoff) {
          unlinkSync(file);
          removed++;
        }
      } catch { /* in use or gone: the next pass */ }
    }
  };
  for (const lane of LANES) walk(p.join(settings.data, "claude", lane.folder, "projects"));
  if (removed) say(`removed ${removed} lane transcript file(s) older than ${days} days`);
  return removed;
}

// Whether this runtime knows `zone` as a time zone name.
export function zoneKnown(zone) {
  try { new Intl.DateTimeFormat("en-US", { timeZone: zone }); return true; } catch { return false; }
}

// The offset (ms) of `zone` from UTC at the instant `at`.
function zoneOffsetMs(zone, at) {
  const f = new Intl.DateTimeFormat("en-US", { timeZone: zone, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", second: "numeric" });
  const o = {};
  for (const part of f.formatToParts(at)) if (part.type !== "literal") o[part.type] = Number(part.value);
  return Date.UTC(o.year, o.month - 1, o.day, o.hour, o.minute, o.second) - Math.floor(at / 1000) * 1000;
}

// The next 3:30 AM after `now`, in milliseconds from `now`: when the nightly prune runs. It is 3:30 on the owner's clock
// (`zone`, the install's JOBSEARCH_TIMEZONE) when one is given and known, else on this machine's.
export function untilNightly(now = new Date(), zone = "") {
  if (!zone || !zoneKnown(zone)) {
    const at = new Date(now);
    at.setHours(3, 30, 0, 0);
    if (at <= now) at.setDate(at.getDate() + 1);
    return at - now;
  }
  const at = now.getTime();
  const f = new Intl.DateTimeFormat("en-US", { timeZone: zone, year: "numeric", month: "numeric", day: "numeric" });
  const day = {};
  for (const part of f.formatToParts(at)) if (part.type !== "literal") day[part.type] = Number(part.value);
  const at330 = (offsetDays) => {
    const naive = Date.UTC(day.year, day.month - 1, day.day + offsetDays, 3, 30);
    // 3:30 is never in the hour the clocks skip or repeat in a zone that changes at 2:00 or 3:00; where one does, the first instant is used.
    for (const near of [naive - 86_400_000, naive + 86_400_000]) {
      const t = naive - zoneOffsetMs(zone, near);
      if (zoneOffsetMs(zone, t) === naive - t) return t;
    }
    return naive - zoneOffsetMs(zone, naive - 86_400_000);
  };
  const today = at330(0);
  return (today > at ? today : at330(1)) - at;
}

// What stops a start, as plain sentences (none: it can start). Makes the folders the lanes need.
export async function checkStart(settings, say = () => {}) {
  const p = pathFor(settings.platform);
  const problems = [];
  for (const name of ["agent-token", "wake-secret"]) {
    if (!existsSync(p.join(settings.secrets, name))) problems.push(`${name} is not in ${settings.secrets}: it is the same file the app is given`);
  }
  if (!existsSync(settings.instructions)) problems.push(`the instruction files are not in ${settings.instructions} (SCOUT_INSTRUCTIONS_DIR)`);
  if (!existsSync(settings.scout)) problems.push(`${settings.scout} is not there`);
  for (const kind of ["logs", "run"]) mkdirSync(p.join(settings.data, kind), { recursive: true });
  if (settings.browser === "profile" && settings.browserProfile) mkdirSync(settings.browserProfile, { recursive: true });
  for (const [index, lane] of LANES.entries()) {
    const port = settings.basePort + index;
    if (!(await portIsFree(port))) problems.push(`port ${port} (the ${lane.runner} lane's) is in use on 127.0.0.1: are the lanes running already? (SCOUT_PORT is the first of ${LANES.length})`);
    const work = p.join(settings.data, "work", lane.folder);
    mkdirSync(work, { recursive: true });
    try { moveStrays(work, say); } catch (e) { say(`could not move what is in ${work} (${e.message}); starting anyway`); }
  }
  return problems;
}

// Starts the five lanes and keeps them up. Returns { lanes, stop }: `lanes` is [{ runner, lane, port, child }] (the child
// changes when a lane is started again), and `stop()` ends every lane and what it started, and does not start any again.
export function startLanes(settings, { env = process.env, say = console.log, restartMs = 5000, restartMaxMs = 300_000, stableMs = 60_000 } = {}) {
  let stopping = false;
  const lanes = LANES.map((lane, index) => ({ ...lane, port: settings.basePort + index, child: null, delay: restartMs, timer: null }));
  const launch = (state, index) => {
    const started = Date.now();
    const child = spawn(process.execPath, [settings.scout], {
      env: laneEnv(settings, state, index, { env }),
      cwd: settings.data,
      windowsHide: true,
      stdio: "ignore",
    });
    state.child = child;
    say(`lane ${state.runner} started, pid ${child.pid}, on 127.0.0.1:${state.port}`);
    // A lane that ends by itself, or that never starts, is started again (once: `exit` follows `error` sometimes).
    let over = false;
    const ended = (what) => {
      if (over || stopping) return;
      over = true;
      state.delay = Date.now() - started >= stableMs ? restartMs : Math.min(state.delay * 2, restartMaxMs);
      say(`lane ${state.runner} ${what}; starting it again in ${Math.round(state.delay / 1000)}s`);
      state.timer = setTimeout(() => { if (!stopping) launch(state, index); }, state.delay);
    };
    child.on("error", (e) => ended(`could not start (${e.message})`));
    child.on("exit", (code, signal) => ended(`ended (${signal || `exit ${code}`})`));
  };
  lanes.forEach((state, index) => launch(state, index));
  // However this process exits, the lanes go with it, here, at once. An end that runs nothing at all (Task Manager) is the job
  // object's, on Windows (see the header).
  process.on("exit", () => { for (const state of lanes) killTree(state.child); });
  return {
    lanes,
    async stop() {
      stopping = true;
      for (const state of lanes) clearTimeout(state.timer);
      const gone = lanes
        .filter((s) => s.child && s.child.exitCode === null && s.child.signalCode === null)
        .map((s) => new Promise((resolve) => s.child.once("exit", resolve)));
      for (const state of lanes) killTree(state.child);
      await Promise.race([Promise.all(gone), new Promise((resolve) => setTimeout(resolve, 5000))]);
    },
  };
}

// The bridge's register of open tabs, which is the app's (docs/PRD.md path 52): the bridge tells the app whose each tab is
// and asks it, when it connects to the app's own Chrome, which tabs to keep. Every call is the agent's, with the token in
// the secrets folder read each time; an app that does not answer is no one's error (the bridge closes nothing then).
export function appRegister({ appUrl, token, fetchImpl = globalThis.fetch }) {
  const call = async (method, suffix, body, at = "/tabs") => {
    const bearer = typeof token === "function" ? token() : token;
    if (!bearer) return null;
    try {
      const res = await fetchImpl(`${appUrl}/api/v1/agent/browser${at}${suffix}`, {
        method,
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${bearer}` },
        ...(body ? { body: JSON.stringify(body) } : {}),
        signal: AbortSignal.timeout(5000),
      });
      return res.ok ? await res.json() : null;
    } catch {
      return null;
    }
  };
  return {
    opened: (targetId, ownerId, kind) => call("POST", "", { target_id: targetId, owner_id: ownerId, kind }),
    removed: (targetId) => call("DELETE", `/${encodeURIComponent(targetId)}`),
    released: (ownerId) => call("POST", "/release", { owner_id: ownerId }),
    reconcile: async () => {
      const said = await call("POST", "/reconcile", {});
      return said && Array.isArray(said.keep) ? said.keep : null;
    },
    // What the browser queue holds ({ waiting, leased }: the items a worker could be handed now, and those it holds), which the
    // bridge asks before it closes the app's Chrome for want of work (PRD 52.9). null when the app does not say.
    busy: async () => {
      const said = await call("GET", "", undefined, "/busy");
      return said && Number.isInteger(said.waiting) && Number.isInteger(said.leased) ? { waiting: said.waiting, leased: said.leased } : null;
    },
  };
}

// The key the browser bridge asks of every request (docs/PRD.md path 52; the owner, 2026-11-14): a random secret (32 bytes,
// hex) in the data folder's secrets, made the first time the lanes start and kept across restarts, like the app's other
// secrets. Never printed or logged. The app and every session read the same file.
export const BRIDGE_KEY_FILE = "bridge-key";
export function readBridgeKey(settings) {
  try { return readFileSync(path.join(settings.secrets, BRIDGE_KEY_FILE), "utf8").trim(); } catch { return ""; }
}
export function ensureBridgeKey(settings, say = () => {}) {
  const found = readBridgeKey(settings);
  if (found) return found;
  const key = randomBytes(32).toString("hex");
  mkdirSync(settings.secrets, { recursive: true });
  writeFileSync(path.join(settings.secrets, BRIDGE_KEY_FILE), key, { mode: 0o600 });
  say(`made ${path.join(settings.secrets, BRIDGE_KEY_FILE)}`);
  return key;
}

// What the browser bridge (own-chrome.mjs `ownChromeBridge`) is made with: the port, which Chrome and whose profile, the app's
// register of tabs (at the address the lanes are pointed at, `settings.appUrl`), and the owner's page (`settings.pageUrl`: the
// bridge's first tab shows the page the app serves there, the owner 2026-11-05). `say` is where its log goes.
export function bridgeOptions(settings, port, say = () => {}, { env = process.env, exists = existsSync } = {}) {
  return {
    port, mode: settings.browser, profile: settings.browserProfile, chromeBin: browserCommand(settings, { env, exists }).file,
    appUrl: settings.pageUrl,
    key: readBridgeKey(settings),
    log: (line) => say(`browser: ${line}`),
    register: appRegister({
      appUrl: settings.appUrl,
      token: () => { try { return readFileSync(path.join(settings.secrets, "agent-token"), "utf8").trim(); } catch { return ""; } },
    }),
  };
}

// How long a stop waits for the browser workers to finish the item each holds (about three minutes).
export const DRAIN_WAIT_MS = 180_000;

// Before the lanes stop (Ctrl+C, a stop, a release): the app is asked to drain the browser workers (it hands out no item
// and starts no session, and each worker finishes the item it holds and is told to stop), and the stop waits until no
// worker holds one, or `waitMs`. Resolves with whether every worker finished. The app not answering stops nothing: the
// lanes stop at once, as before, and a lost item comes back when its lease runs out.
export async function drainBrowser({ appUrl, token, waitMs = DRAIN_WAIT_MS, everyMs = 2000, fetchImpl = globalThis.fetch, say = () => {} }) {
  const call = async (method) => {
    const res = await fetchImpl(`${appUrl}/api/v1/agent/browser/drain`, {
      method,
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      ...(method === "POST" ? { body: JSON.stringify({ on: true }) } : {}),
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) throw new Error(`the app answered ${res.status}`);
    return res.json();
  };
  let said;
  try {
    said = await call("POST");
  } catch (e) {
    say(`couldn't ask the app to let the browser workers finish (${e.message}); stopping now`);
    return false;
  }
  const until = Date.now() + waitMs;
  let told = false;
  while (Number(said?.holding) > 0 && Date.now() < until) {
    if (!told) say(`waiting for ${said.holding} browser worker(s) to finish the item they hold (up to ${Math.round(waitMs / 60000)} minutes)`);
    told = true;
    await new Promise((resolve) => setTimeout(resolve, everyMs));
    try { said = await call("GET"); } catch { break; }
  }
  const done = !(Number(said?.holding) > 0);
  if (told) say(done ? "the browser workers finished their items" : "the browser workers did not finish in time; their items go back to the queue");
  return done;
}

// Starts the browser bridge on the install's port. A port the install did not name (SCOUT_BROWSER_PORT unset) that is taken,
// by a Chrome an older runner started on 9333 or by anything else, is no reason to stop: the bridge listens on a free port
// instead, and `settings.browserPort` becomes the one in use, which every lane is told (`laneEnv`). A port the install named
// is the only one used. `make(port)` makes the bridge (own-chrome.mjs `ownChromeBridge`); resolves with it, started.
// The port in use is written to the data folder's BROWSER_PORT_FILE, which the app reads (browser/code.py: its own code
// reaches the browser through the bridge too), so a free port picked here reaches the app as well as the lanes.
export const BROWSER_PORT_FILE = "browser-port";
export async function startBridge(settings, make, say = () => {}) {
  const on = async (port) => {
    const bridge = make(port);
    await bridge.start();
    settings.browserPort = bridge.port ?? port;
    if (settings.data) {
      try {
        writeFileSync(path.join(settings.data, BROWSER_PORT_FILE), `${settings.browserPort}\n`);
      } catch (e) {
        say(`could not tell the app the browser bridge's port (${e.message}); it uses SCOUT_BROWSER_PORT or 9333`);
      }
    }
    return bridge;
  };
  try {
    return await on(settings.browserPort);
  } catch (e) {
    if (e?.code !== "EADDRINUSE" || settings.browserPortSet) throw e;
    say(`127.0.0.1:${settings.browserPort} is in use (perhaps a Chrome an older runner started on it); the browser bridge takes a free port instead`);
    return await on(0);
  }
}

// How the Chrome of the install's own profile is started (SCOUT_BROWSER=profile): visible, its DevTools port picked by Chrome
// on 127.0.0.1 only and found by the bridge in the profile's DevToolsActivePort (own-chrome.mjs starts it the same way when a
// browser session needs it). `file` is "" when Chrome is not found (SCOUT_CHROME_BIN names it).
export function browserCommand(settings, { env = process.env, exists = existsSync } = {}) {
  const p = pathFor(settings.platform);
  const places = settings.platform === "win32"
    ? [env.ProgramFiles, env["ProgramFiles(x86)"], env.LOCALAPPDATA].filter(Boolean).map((d) => p.join(d, "Google", "Chrome", "Application", "chrome.exe"))
    : settings.platform === "darwin"
      ? ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
      : ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser"];
  return {
    file: settings.chromeBin || places.find((place) => exists(place)) || "",
    args: profileChromeArgs(settings.browserProfile),
  };
}

// `node app/scout/lanes-local.mjs open-browser-profile`: opens the shared browser, visible, on its profile, so the owner signs
// in to the sites they want themselves. Nothing is typed or stored for them; Chrome keeps its own sign-ins in the profile.
// The browser sessions attach to this same Chrome while it is open.
function openBrowserProfile() {
  const settings = readSettings();
  mkdirSync(settings.browserProfile, { recursive: true });
  const { file, args } = browserCommand(settings);
  if (!file) {
    console.error("Chrome was not found: set SCOUT_CHROME_BIN in the data folder's .env to it");
    process.exit(1);
  }
  resetWindowPlacement(settings.browserProfile);
  spawn(file, args, { detached: true, stdio: "ignore" }).on("error", (e) => {
    console.error(`Chrome could not be started: ${e.message}`);
    process.exit(1);
  }).unref();
  console.log(`Opened Chrome on the browser profile (${settings.browserProfile}). Sign in to the sites you want, then leave it open or close it.`);
}

async function main() {
  if (process.argv[2] === "open-browser-profile") {
    openBrowserProfile();
    return;
  }
  let settings;
  const logFile = () => path.join(settings.data, "logs", "lanes.log");
  // Started with no console (a scheduled task) or with its pipe closed, the launcher goes on: the log file is the record.
  for (const stream of [process.stdout, process.stderr]) stream.on("error", () => {});
  const say = (line) => {
    const entry = `${new Date().toISOString()} ${line}`;
    console.log(entry);
    try { appendFileSync(logFile(), `${entry}\n`); } catch { /* the folder is made by checkStart; before that, the console is all */ }
  };
  try {
    settings = readSettings();
  } catch (e) {
    console.error(e.message);
    process.exit(1);
  }
  // A crash is never lost: one line (the error's name and a system code, never its message, which may hold a session's
  // words), then a non-zero exit. A stray rejection is logged the same way and the launcher goes on.
  const words = (e) => `${e && typeof e.name === "string" ? e.name : typeof e}${e && typeof e.code === "string" && /^[A-Z0-9_]{2,40}$/.test(e.code) ? ` code=${e.code}` : ""}`;
  process.on("uncaughtException", (e) => {
    try { say(`uncaught exception in the lanes launcher: ${words(e)}`); } catch { /* the exit is what matters */ }
    process.exit(70);
  });
  process.on("unhandledRejection", (e) => say(`unhandled rejection in the lanes launcher: ${words(e)}`));
  const problems = await checkStart(settings, say);
  if (problems.length) {
    for (const problem of problems) say(`not starting: ${problem}`);
    process.exit(1);
  }
  say(`resumebot lanes: the app at ${settings.appUrl}, data in ${settings.data}; the first session takes the app's wake at http://127.0.0.1:${settings.basePort}/wake`);
  // The bridge the browser sessions attach to (own-chrome.mjs): one window, each session in tabs of its own. It connects
  // when a browser session first needs it: to the Chrome of the app's own profile, which it starts, or to the user's everyday Chrome.
  let bridge;
  try {
    ensureBridgeKey(settings, say);
    bridge = await startBridge(settings, (port) => ownChromeBridge(bridgeOptions(settings, port, say)), say);
    say(`browser jobs use ${settings.browser === "own" ? "your own Chrome" : "the Chrome of the browser profile"}, through 127.0.0.1:${settings.browserPort}`);
  } catch (e) {
    say(`not starting: the browser bridge could not listen on 127.0.0.1:${settings.browserPort} (${e.message})${e.code === "EADDRINUSE"
      ? ": SCOUT_BROWSER_PORT names a port that is in use; name another, or leave it out and a free one is used" : ""}`);
    process.exit(1);
  }
  const prune = () => { try { pruneTranscripts(settings, { say }); } catch (e) { say(`could not prune the lane transcripts (${e.message})`); } };
  prune(); // at start, and every night
  const nightly = () => setTimeout(() => { prune(); nightly(); }, untilNightly(new Date(), settings.timezone)).unref();
  nightly();
  const running = startLanes(settings, { say });
  let ending = false;
  const end = async (why) => {
    if (ending) return;
    ending = true;
    say(`${why}: stopping the lanes`);
    // The browser workers finish the item each holds first.
    let token = "";
    try { token = readFileSync(path.join(settings.secrets, "agent-token"), "utf8").trim(); } catch { /* none: no drain */ }
    if (token) await drainBrowser({ appUrl: settings.appUrl, token, say });
    await running.stop();
    await bridge.stop();
    process.exit(0);
  };
  for (const signal of ["SIGINT", "SIGTERM", ...(process.platform === "win32" ? ["SIGBREAK", "SIGHUP"] : [])]) {
    process.on(signal, () => void end(signal));
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  await main();
}
