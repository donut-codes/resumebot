// resumebot's scout runner: one headless Claude Code (Sonnet) session per job search, on the owner's Claude
// subscription -- never an API key. Trimmed from the source project's planner runner (app/PROVENANCE.md) to the one kind of
// session this app has.
//
// Starts a run when the app sends a signed wake (`mode: "scout"`). A timer (SCOUT_CHECK_IN_MIN) is the check-in for
// a search the wake missed. Runs never overlap: a wake that arrives mid-run is folded into one follow-up run.
//
// Ten kinds of session take turns on this runner, each of its own (docs/PRD.md 23.3, 29, 30, 31, 36, 38): Find Gaps (the
// analyst's role, a Sonnet session the owner watches on My Resume: `jobs.gaps`, `jobs.gaps_progress`, `jobs.gaps_report`;
// the public web, read-only, for a title that is not a target role; it goes after the role check), the parser (a
// small Haiku session that splits a posting the owner pasted into a target role's fields, or divides the resume they
// imported into the master resume's, while the owner waits: `jobs.parse_next`, `jobs.parse_report`; no web tools at
// all; it goes first), the role check (a small Haiku session
// that judges the owner's pursued roles, a sample of stopped postings or one pasted posting against their role tabs
// as a dry run of the title check, while the owner waits: `jobs.match_next`, `jobs.match_report`; no web tools at
// all; it goes next), the scout's search, which
// finds employers' job boards (`jobs.profile`, `jobs.boards`, `jobs.board_found`, `jobs.report`), the titles pass (a
// small Haiku session that judges, against the owner's target roles, the title of every posting that passed the gates (the classifier only orders them and drops none), a batch at a time, yes or no for each, with
// the target role a yes matches; it also gives the roles found before target roles the one each matches, once, when
// the owner starts that: `jobs.titles_next`, `jobs.titles_report`; no web tools at all), the
// analyst (researches a found role: `jobs.analysis`,
// `jobs.analysis_submit`), the employer researcher (a small Haiku session that researches one employer, the analyst's role
// with an instruction file of its own: `jobs.employer`, `jobs.employer_report`; the public web, read-only), the writer (a seat: one writing turn with no tools and no door) and the scout's site work
// (`site`: learns to read one site's listings in plan, do and act tasks: `work.next`, `task.next`, `task.complete`,
// `plan.propose`, `skill.try`, `lesson.record`, `act.complete` and the rest of the site tools). Each session is given that
// kind's instructions alone, added to Claude Code's own system prompt (`--append-system-prompt-file`: the text is written
// to a file for the session and deleted when it ends, so an instruction file of any size gets there; none of another
// kind's), and it may use only its own tools plus the public web (no shell, no files, no browser). Before a session the runner asks the app which work is waiting (GET /api/v1/agent/work); when the app
// can't be reached it goes by the wake's reason (`site_project` is site work, `tag_roles` a title session). After a session, while more work waits,
// it starts the next at once, so a night with no limits goes on until the app has nothing left for it. One search, one
// role researched, one packet, or one site project's tasks, a session. A session that ends because Claude's usage is used up is reported as that, and
// the app ends the night's run cleanly (docs/PRD.md 25.12).
//
// Each run reports itself to the app (the agent session heartbeat): "started" before Claude takes its task, "alive"
// every minute while it works, "ended" however it ends. A failed report never fails a run. Between sessions it says
// "idle" every SCOUT_PRESENCE_SEC (120), so the app knows an agent is up to take work now (a pasted posting waits
// for no one); that is the one thing that tells it the runner is there between check-ins.
//
// The pool. A runner is one session of the app's session pool (the lane `any`, the only one): it takes every kind of
// work, the app naming the most rightward one (the owner's asks, site learning, packets, research, title checks, the
// search), and it looks for work on its check-in, every two minutes.
// The app's dispatcher commands a waiting runner (docs/PRD.md 47.1 to 47.3): it claims the item for the runner and sends the signed wake
// with `claim`, `kind` and `item`, and the runner starts that session without asking; one that cannot (busy, held at the usage
// limit) answers 409 and the app gives the claim back. The check-in is the slow fallback for a command that was lost: the app
// answers it with the claim already made for this runner, if there is one.
// The runner asks the app for the pool's work (GET /api/v1/agent/work?only=any, with `&runner=<name>` for a runner named
// `session-<n>`); a reply naming a kind it has no session for is read as nothing waiting, and said once. With the app out
// of reach it goes by the wake's reason. It goes on to the next session at once while the app keeps naming work, with
// no pause. A session that ends at Claude's usage limit holds the runner: it asks for nothing, on its check-in or any
// other way, until the limit lifts (30 minutes when no time is given). The runner says it is up between sessions
// ("idle", every SCOUT_PRESENCE_SEC) and names itself and its lane (`lane`, `runner`) in every heartbeat, so the app
// knows which runners are there and what each is doing (the Agents page shows every runner's session) and does not
// take them for the scout's own session (whether an agent can take a job now), though a usage limit one meets is heard
// as ever. The name is SCOUT_RUNNER, else "any"; two runners need a SCOUT_RUNNER each, or the app takes them for one.
// The app, not the runner, keeps two runners off the same role.
//
// Settings (env): SCOUT_MODEL (claude-sonnet-5-5; every kind
// of session but the title pass's and the parser's), SCOUT_TITLES_MODEL (claude-haiku-5-5) and
// SCOUT_TITLES_EFFORT (low),
// SCOUT_PARSER_MODEL (claude-haiku-5-5) and SCOUT_PARSER_EFFORT (low), SCOUT_CHECKER_MODEL (claude-haiku-5-5)
// and SCOUT_CHECKER_EFFORT (low), SCOUT_RESEARCHER_MODEL (claude-haiku-5-5) and SCOUT_RESEARCHER_EFFORT (low),
// SCOUT_EFFORT (medium; low|medium|high|xhigh|max, or "none" to leave it to the CLI's own default),
// SCOUT_MAX_TURNS (80), SCOUT_RUN_TIMEOUT_MIN (45), SCOUT_HEARTBEAT_SEC (60), SCOUT_CHECK_IN_MIN (2),
// SCOUT_RUNNER (none: "any"; else a name for this runner,
// `session-1`, which its heartbeats call it and every session's calls say whose they are, and the runner reports its
// start, so a role a killed session was researching is freed at once, see `RUNNER` below),
// SCOUT_PORT (8651), RESUMEBOT_MCP_URL (the app's MCP door), RESUMEBOT_API_URL (its API; the MCP address without /mcp).
//
// Secrets are files under /secrets, read at run time, never logged:
//   claude-oauth-token  from `claude setup-token`, stored by `resumebot signin` (see below)
//   agent-token         the scout's token for the app (the app's agent-token)
//   wake-secret         the shared secret the app signs its wake with
//
// In local-lanes mode the lanes run this same file under Node, five processes
// started by lanes-local.mjs, outside any image. What that adds, none of which changes a runner in the default mode (each of
// these settings is unset there):
//   SCOUT_BIND               the address the runner listens on. Unset: every address; on Windows: 127.0.0.1 alone, nothing
//                            else on the network can wake a lane.
//   SCOUT_CLAUDE_CONFIG_DIR  a Claude folder of this runner's own, the `CLAUDE_CONFIG_DIR` of every session it starts. The
//                            user's own CLAUDE.md, hooks, skills, plugins and memory live under the default folder, so a
//                            lane never sees them; and with it set a session reads no CLAUDE.md from anywhere (not the working
//                            folder's parents either), writes no memory, and takes no ANTHROPIC_* or CLAUDE_* variable from
//                            the environment the runner was started in (Git Bash's place apart): started from a Claude Code
//                            session, that environment is the host session's account, route and "a person is here".
//   SCOUT_WORKDIR            on a local computer, an empty folder of this runner's own: the session's working folder. Claude Code in
//                            `-p` mode shows no trust dialog, so what is in it (a CLAUDE.md, hooks, an .mcp.json) would be
//                            used; a session does not start while anything is in it.
//   SCOUT_CLAUDE_BIN         the Claude Code to run. Unset on Windows: the user's own `claude.exe`, found each time a
//                            session starts (`findClaude`), because the Claude desktop app's copy lives in a folder that is
//                            new with every update. A .js, .mjs or .cjs file runs under this Node (a stand-in for Claude:
//                            Windows cannot run a script by its name).
// In every runner, where it changes nothing in the default mode: the Claude token file is how every install signs its agents in
// (`resumebot signin` stores the token `claude setup-token` made; without the file a session runs on the login in its
// folder), and a session's environment has no
// ANTHROPIC_AUTH_TOKEN, as it has no ANTHROPIC_API_KEY: the sign-in is the file's or the folder's, never another route's.
// On Windows a session ends with its whole process tree (`taskkill /T`), runs with no console window, and has no input
// (Claude Code otherwise waits three seconds for piped input before it starts). A runner and its sessions also end when
// what started them ends, however it ends (Task Manager too): Node puts every process it starts, unless it is detached, in a
// job object that closes with it, and nothing here is detached. What a session itself starts is not in that job (a process
// started by a member of a Node job is not a member): it ends with the session, by `taskkill /T`, and not when the runner
// is ended outright.

import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { homedir } from "node:os";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// A number setting: the environment's value when it is a positive number, else the default. A value that isn't a number
// (`Number("abc")` is NaN) would otherwise become a timer of one millisecond.
function num(name, fallback) {
  const n = Number(process.env[name]);
  return process.env[name] && Number.isFinite(n) && n > 0 ? n : fallback;
}

const PORT = num("SCOUT_PORT", 8651);
const APP_MCP = process.env.RESUMEBOT_MCP_URL || "http://localhost:8430/mcp";
// The runner's one lane (see The pool above), as the app is told it (`only=`, `lane`).
const POOL = "any";
// How often, in minutes, the runner looks for work when no wake announces it: every 2 minutes, the length of a claim lease
// (path 47.3): the app's dispatcher commands an idle session with the item it claimed, so this is only the safety net for
// a command that was lost, and its answer is the claim already made for the session (the owner, 2026-10-16: 20 polls a
// minute kept the computer busy all day for nothing).
const CHECK_IN_MIN = 2;
const RUN_TIMEOUT_MS = num("SCOUT_RUN_TIMEOUT_MIN", 45) * 60_000;
// How long a session that was told to stop (the run timeout, the peak-hours blackout) has to end before it is killed.
const KILL_GRACE_MS = 10_000;
// The most a wake's body may hold (it carries an id and a reason), and the most the folded follow-up's reason may grow to.
const WAKE_BODY_MAX = 64 * 1024;
const PENDING_MAX = 1000;
// The main model, named by its id like the small ones below. The alias "sonnet" is whatever the Claude Code in use maps
// it to: 2.1.197 maps it to Sonnet 5, a step behind the latest. Move to the next Sonnet here, on purpose, once it is out
// and the Claude Code in use accepts its id.
const DEFAULT_MODEL = "claude-sonnet-5-5";
const MODEL = process.env.SCOUT_MODEL || DEFAULT_MODEL;
// `claude --effort <level>` (low, medium, high, xhigh, max). Set explicitly so a change of the CLI's own default never
// changes how hard the scout thinks.
const EFFORT = process.env.SCOUT_EFFORT || "medium";
// The title sessions (the one-off tagging of roles, the header-line reads of gate facts) are small and many: Haiku, at low
// effort (Haiku 5.5 takes an effort; "none" leaves it to the CLI's own default). Everything else runs on MODEL.
const TITLES_MODEL = process.env.SCOUT_TITLES_MODEL || "claude-haiku-5-5";
const TITLES_EFFORT = process.env.SCOUT_TITLES_EFFORT || "low";
// The parser (Fill from a posting on What to look for; Import your resume on Me): one small Haiku session per pasted
// posting or imported resume.
const PARSER_MODEL = process.env.SCOUT_PARSER_MODEL || "claude-haiku-5-5";
const PARSER_EFFORT = process.env.SCOUT_PARSER_EFFORT || "low";
// The role check (Check my roles on What to look for, docs/PRD.md 31.2): a dry run of the title check, a small Haiku
// session per check.
const CHECKER_MODEL = process.env.SCOUT_CHECKER_MODEL || "claude-haiku-5-5";
const CHECKER_EFFORT = process.env.SCOUT_CHECKER_EFFORT || "low";
// The employer researcher (docs/PRD.md path 36): one small Haiku session reads public pages for an employer at a time.
const RESEARCHER_MODEL = process.env.SCOUT_RESEARCHER_MODEL || "claude-haiku-5-5";
const RESEARCHER_EFFORT = process.env.SCOUT_RESEARCHER_EFFORT || "low";
// The mail reader (docs/PRD.md path 14): a small Haiku session that classes the job mail the rules could not settle. It sees
// only the start of each message's text, never the mailbox login, and has no web tools.
const MAIL_MODEL = process.env.SCOUT_MAIL_MODEL || "claude-haiku-5-5";
const MAIL_EFFORT = process.env.SCOUT_MAIL_EFFORT || "low";
// The browser's two kinds (the owner, 2026-10-15: nothing is scheduled in the Claude desktop app; the browser runs in
// the pool). Both work the app's queue of small items one at a time: the browser worker takes
// `source`, `read` and `scan` items, the Pathfinder `map` items. Both run on MODEL (the owner, 2026-10-16: the browser
// workers make judgement calls on unfamiliar pages, so Sonnet, not Haiku; every item is small, so the context never
// fills). A session works items until the app has none it can take now, then ends: no run timeout unless
// SCOUT_BROWSER_TIMEOUT_MIN sets one, no turn cap (the owner, 2026-10-16: a time box per item, kept by the app, never a
// cap per session), and the app's lease (10 minutes, renewed by every call) in place of a wall clock. Which browser is SCOUT_BROWSER's. `profile` (the default, the owner 2026-10-16): one visible
// Chrome on the app's own profile (`node lanes-local.mjs open-browser-profile` opens it), started by the launcher's
// bridge (own-chrome.mjs) when a session needs it. `own` (a setting): the owner's own everyday Chrome, reached through
// the same bridge. The runner starts no Chrome either way, and a session does not start when the bridge is not running or
// cannot attach its Chrome (`ensureBrowser` says why, and the app is told: Wake up and Run say it). Each session's DevTools
// MCP server attaches to the bridge on SCOUT_BROWSER_PORT and works in tabs of its own, all in one window, so several
// sessions read at once. Where Chrome is, when it is not in its usual place, is the launcher's setting (SCOUT_CHROME_BIN,
// lanes-local.mjs).
// SCOUT_BROWSER_MCP is the server's npm package with its version (`chrome-devtools-mcp@<version>`; the pinned one below
// when unset): the install's setting (lanes-local.mjs passes it from the data folder's .env), and the .env's value wins.
const BROWSER_READER_MODEL = process.env.SCOUT_BROWSER_READER_MODEL || MODEL;
const BROWSER_READER_EFFORT = process.env.SCOUT_BROWSER_READER_EFFORT || EFFORT;
const PATHFINDER_MODEL = process.env.SCOUT_PATHFINDER_MODEL || MODEL;
const PATHFINDER_EFFORT = process.env.SCOUT_PATHFINDER_EFFORT || EFFORT;
const BROWSER_TIMEOUT_MS = num("SCOUT_BROWSER_TIMEOUT_MIN", 0) * 60_000; // 0: none
const DEFAULT_BROWSER_MCP = "chrome-devtools-mcp@1.10.1";
const BROWSER_MCP = (process.env.SCOUT_BROWSER_MCP || "").trim() || DEFAULT_BROWSER_MCP;
const BROWSER_MODE = (process.env.SCOUT_BROWSER || "").trim().toLowerCase() === "own" ? "own" : "profile";
// Set by the local launcher: the sessions attach to its bridge (own-chrome.mjs: one window, each session in tabs of its own,
// closed when it ends), which starts the profile's Chrome itself; this runner starts none.
const BROWSER_BRIDGE = (process.env.SCOUT_BROWSER_BRIDGE || "").trim() === "1";
const BROWSER_PORT = num("SCOUT_BROWSER_PORT", 9333);
const BROWSER_URL = `http://127.0.0.1:${BROWSER_PORT}`;
// What a browser session is told about the browser it is in, in its prompt (the instructions say the same).
const BROWSER_WORDS = BROWSER_MODE === "own"
  ? "You are in the owner's own Chrome: open your own tab (new_page) and touch nothing else. Never apply, fill in a form other than a site's own job search box (a cookie notice is declined with its Reject or Decline option, never a block), solve a CAPTCHA, message, connect or post. Other sessions work in the same Chrome: work only in the pages you opened and close only them."
  : "Never apply, fill in a form other than a site's own job search box (a cookie notice is declined with its Reject or Decline option, never a block), solve a CAPTCHA, message, connect or post. Other sessions share the browser: open your own pages, work only in them and close only them.";
// One window for all the browser jobs, each session in tabs (the owner, 2026-10-16); the bridge opens every page as a tab of that window and closes a session's tabs when
// it ends.
const BROWSER_TABS = "Work in your one tab: navigate your one page; never open a new page per posting; close any extra tab as soon as you're done with it. Never use isolatedContext.";
// How long a browser session waits for the owner's Chrome to be attached (`own`): the bridge waits up to a minute for
// the owner's Allow in Chrome's prompt.
const OWN_CHROME_WAIT_MS = num("SCOUT_OWN_CHROME_WAIT_SEC", 90) * 1000;
// How often the runner says it is up while it is between sessions.
const PRESENCE_MS = num("SCOUT_PRESENCE_SEC", 120) * 1000;
const MAX_TURNS = process.env.SCOUT_MAX_TURNS || "80";
// The app's REST door for the session heartbeat; by default the MCP address's own host.
const APP_API = (process.env.RESUMEBOT_API_URL || APP_MCP.replace(/\/mcp\/?$/, "")).replace(/\/$/, "");
const HEARTBEAT_MS = num("SCOUT_HEARTBEAT_SEC", 60) * 1000;
// Where things live. The defaults are the default mode's; the overrides exist so the tests can run the runner anywhere.
const SECRETS_DIR = process.env.SCOUT_SECRETS_DIR || "/secrets";
const INSTRUCTIONS_DIR = process.env.SCOUT_INSTRUCTIONS_DIR || "/app/instructions";
const MCP_CONFIG = process.env.SCOUT_MCP_CONFIG || "/tmp/scout.mcp.json";
// A session's instructions reach Claude as a file beside its MCP config (`--append-system-prompt-file`), never as an
// argument on the command line: the kernel refuses one argument over 131,072 bytes (E2BIG), and an instruction file the
// owner edits has no limit on its size. Written for one session, readable by this runner alone, deleted when it ends.
const SYSTEM_FILE = MCP_CONFIG.replace(/(\.json)?$/, ".system.md");
// The hook that ends a session once its work is accepted (`claudeSettings`, end-session.mjs), and the note it leaves in
// END_MARK, which tells `run` that the stop was a finish.
const END_HOOK = join(dirname(fileURLToPath(import.meta.url)), "end-session.mjs");
const END_MARK = MCP_CONFIG.replace(/(\.json)?$/, ".ended");
const CLAUDE_BIN = process.env.SCOUT_CLAUDE_BIN || "claude";
const WORKDIR = process.env.SCOUT_WORKDIR || "/home/node";
const LOG = process.env.SCOUT_LOG || "/data/runs.log";
// What a local computer's lanes need (see the header).
const WINDOWS = process.platform === "win32";
const BIND = process.env.SCOUT_BIND || (WINDOWS ? "127.0.0.1" : "");
const CLAUDE_CONFIG_DIR = process.env.SCOUT_CLAUDE_CONFIG_DIR || "";

// The app's tools by the name a session sees them (its MCP server is named `jobsearch` in the session's config, so
// Claude Code names them `mcp__jobsearch__jobs_profile` and so on: every character outside letters, digits, `_` and
// `-` becomes `_`).
const tools = (...names) => names.map((t) => `mcp__jobsearch__${t.replace(/[^A-Za-z0-9_-]/g, "_")}`);
// The browser door's tools (`/mcp-browser/`, the server `resumebot-browser` in a browser session's config) and every tool
// of the DevTools server (`chrome-devtools`).
const browserTools = (...names) => [
  ...names.map((t) => `mcp__resumebot-browser__${t.replace(/[^A-Za-z0-9_-]/g, "_")}`), "mcp__chrome-devtools__*",
];

function secret(name) {
  return readFileSync(`${SECRETS_DIR}/${name}`, "utf8").trim();
}

// The key the browser bridge asks of every request (docs/PRD.md path 52; made by lanes-local.mjs): "" when there is none, and
// the bridge then refuses, which `ensureBrowser` says.
function bridgeKey() {
  try { return secret("bridge-key"); } catch { return ""; }
}
const bridgeHeaders = () => ({ "X-Bridge-Key": bridgeKey() });

// The Claude token a session signs in with, from `claude setup-token`, or null when there is no such file or it is empty.
// The token from `claude setup-token`, stored by `resumebot signin`, is how every install signs its lanes in; without the file
// a lane uses the login in its own Claude folder, which a new install does not have. A stored token overrides that login, so
// an expired or invalid token fails the lane.
function claudeToken() {
  try {
    return secret("claude-oauth-token") || null;
  } catch (e) {
    if (e?.code !== "ENOENT") throw e;
    return null;
  }
}

// The scout's token for the app.
function agentToken() {
  return secret("agent-token");
}

// An error as one safe line: its name, and a system code such as ENOENT when it has one. Never its message: that may hold a session's words.
function crashWords(e) {
  const name = e && typeof e === "object" && typeof e.name === "string" ? e.name : typeof e;
  const code = e && typeof e === "object" && typeof e.code === "string" && /^[A-Z0-9_]{2,40}$/.test(e.code) ? ` code=${e.code}` : "";
  return `${name}${code}`;
}

function log(line) {
  const entry = `${new Date().toISOString()} ${line}`;
  console.log(entry);
  try { appendFileSync(LOG, entry + "\n"); } catch {}
}

// On Windows the default Claude folder is the user's own, with their CLAUDE.md, hooks and memory in it, and the default working
// folder (/home/node) is not there: a runner without folders of its own would run a lane in theirs.
if (WINDOWS && (!CLAUDE_CONFIG_DIR || !process.env.SCOUT_WORKDIR)) {
  log("on Windows SCOUT_CLAUDE_CONFIG_DIR and SCOUT_WORKDIR must name folders of this runner's own, never the user's Claude folder (lanes-local.mjs sets them); not starting");
  process.exit(1);
}

// How often it looks for work when no wake announces it: SCOUT_CHECK_IN_MIN, else every 2 minutes.
const INTERVAL_MIN = num("SCOUT_CHECK_IN_MIN", CHECK_IN_MIN);
// The name its heartbeats give this runner (`runner`), which the app tells runners apart by: SCOUT_RUNNER (a name for the
// runner, `session-1`), else "any". The name SCOUT_RUNNER gives (`NAMED`) is also what the claims go by, so it must be
// one runner's alone: every MCP call a session makes names its session and, for a runner named so, the runner
// (`X-Session-Id`, `X-Runner: <name>/<start>`, `START` being new every time the process starts), so the app knows whose a
// role it handed out is: it takes the role back when the session ends or goes quiet, and every role of an earlier start
// of this runner, killed with it, when this start reports itself (`announceStart`). "any" is shared by any two runners
// with no SCOUT_RUNNER, so it names the runner to the page and nothing else.
const NAMED = (process.env.SCOUT_RUNNER || "").trim().slice(0, 40);
const RUNNER = (NAMED || POOL).slice(0, 40);
const RUNNER_START = randomBytes(4).toString("hex");
// What every heartbeat of this runner says about who is sending it.
const WHO = { lane: POOL, runner: RUNNER };

// Tell the app how this run is going, and hear back whether to stop (Claude's peak hours, when the owner blacks them
// out). `extra` is what a state adds: a session that ended at Claude's usage limit says so (`limitReport`, see
// `usageLimit`). The reports say the runner's lane (`lane`): its sessions are not the scout's own, so they leave what the
// app shows of the scout's session alone (the limit they meet is heard all the same). They name the runner too
// (`runner`, see The pool above). Never throws: the app being down must not stop the scout. Returns the app's reply, or {}
// when it can't say.
async function report(sessionId, state, extra = {}) {
  try {
    const res = await fetch(`${APP_API}/api/v1/agent/heartbeat`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${agentToken()}` },
      body: JSON.stringify({ session_id: sessionId, state, ...WHO, ...extra }),
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) log(`heartbeat ${state} refused: ${res.status}`);
    return res.ok ? await res.json().catch(() => ({})) : {};
  } catch (e) {
    log(`heartbeat ${state} failed: ${e.message}`);
    return {};
  }
}

// Between sessions the runner says it is up ("idle"), so the app can tell the owner an agent is there to take a posting
// to split right now, and which runners are up (the Agents page). A session says it itself (its heartbeat), so no ping
// goes out while one runs. A check-in that is only asking the app for work is not a session: the pool's check-in and its
// beat are both two minutes, the beat lands a millisecond after the check-in has started asking, and the beat alone was
// skipped every time. So a check-in that finds
// nothing says it itself (`run`, `fromCheckIn`). Never throws, and says so in the log only when the app stops answering
// and when it answers again, not every time.
let presenceDown = false;
async function presence(fromCheckIn = false) {
  if (running && !fromCheckIn) return;
  const browser = await ownBrowserStatus();
  try {
    const res = await fetch(`${APP_API}/api/v1/agent/heartbeat`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${agentToken()}` },
      body: JSON.stringify({ session_id: "runner-idle", state: "idle", ...WHO, ...(browser === null ? {} : { browser }) }),
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) throw new Error(`refused: ${res.status}`);
    if (presenceDown) log("the app answers again");
    presenceDown = false;
  } catch (e) {
    if (!presenceDown) log(`couldn't tell the app the runner is up: ${e.message}`);
    presenceDown = true;
  }
}

// A runner that has a name says on start that it has started, so the app can free at once the roles that sessions of its
// earlier start (killed with it: a restart, a deploy) were researching, not when their silence would be noticed. The app
// may still be coming up beside it, so it asks again a few times. Never throws; a report that never lands costs only the
// few minutes the app takes to notice the silence by itself.
const START_REPORT_TRIES = num("SCOUT_START_REPORT_TRIES", 8);
const START_REPORT_WAIT_MS = num("SCOUT_START_REPORT_WAIT_SEC", 15) * 1000;
async function announceStart() {
  if (!NAMED) return;
  for (let attempt = 1; attempt <= START_REPORT_TRIES; attempt++) {
    try {
      const res = await fetch(`${APP_API}/api/v1/agent/heartbeat`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${agentToken()}` },
        body: JSON.stringify({ session_id: "runner-start", state: "boot", runner: NAMED, boot: RUNNER_START, browser_mode: BROWSER_MODE }),
        signal: AbortSignal.timeout(10_000),
      });
      if (res.ok) return;
      log(`start report refused: ${res.status}`);
    } catch (e) {
      log(`couldn't report the start to the app: ${e.message}`);
    }
    await new Promise((resolve) => setTimeout(resolve, START_REPORT_WAIT_MS));
  }
}

// What is behind this connection, stated once on start. All plain facts, none a secret. Sessions never overlap, so it
// takes one task at a time; it runs Claude Code headless on a Claude subscription, hosted.
function declaration(env = process.env) {
  const facts = {
    model_name: env.SCOUT_MODEL || DEFAULT_MODEL,
    hosting: "hosted",
    runtime: "Claude Code",
    engine: env.SCOUT_ENGINE || "Claude subscription",
    concurrency: 1,
    usage_limit: env.SCOUT_USAGE_LIMIT || "A Claude subscription's usage limit: work waits until it resets",
  };
  if (env.SCOUT_HOST) facts.host = env.SCOUT_HOST;
  return facts;
}

// Never throws and never blocks a run: the app being down only delays it knowing this.
async function declare(delaysSec = [0, 5, 15, 30, 60, 120, 300, 600]) {
  for (const delay of delaysSec) {
    if (delay) await new Promise((r) => setTimeout(r, delay * 1000));
    try {
      const res = await fetch(`${APP_API}/api/v1/agent/capabilities`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${agentToken()}` },
        body: JSON.stringify(declaration()),
        signal: AbortSignal.timeout(10_000),
      });
      const reply = await res.json().catch(() => ({}));
      if (res.ok && reply.ok) {
        log("declared what is behind this connection");
        return;
      }
      log(`declare refused: ${res.status} ${reply.reason || ""}`);
    } catch (e) {
      log(`declare failed: ${e.message}`);
    }
  }
}

// The instruction file in this image, read each run, with its header comment dropped.
const strip = (f) => readFileSync(`${INSTRUCTIONS_DIR}/${f}`, "utf8").replace(/^\s*<!--[\s\S]*?-->\s*/, "").trim();

// The instruction files a session starts with: the version the owner last saved in the app (How they work), else the
// file in this image. One ask for all of a session's files; when the app can't be asked, or has no text for a file,
// that file is read from the image, so a session never starts without its instructions.
async function instructionTexts(names) {
  let got = {};
  try {
    const res = await fetch(`${APP_API}/api/v1/agent/instructions?names=${encodeURIComponent(names.join(","))}`, {
      headers: { Authorization: `Bearer ${agentToken()}` },
      signal: AbortSignal.timeout(10_000),
    });
    if (res.ok) got = (await res.json().catch(() => ({}))).files || {};
    else log(`asking for the instruction files refused: ${res.status}; reading the image's own`);
  } catch (e) {
    log(`asking for the instruction files failed: ${e.message}; reading the image's own`);
  }
  return names.map((f) => (typeof got[f]?.text === "string" && got[f].text.trim() ? got[f].text.trim() : strip(f)));
}

// The owner's stable material for a kind of session (`session_context.py` in the app): the analyst's master resume,
// scoring and response shape. It goes at the end of the session's system prompt,
// the cached prefix, instead of coming back in a tool reply every session; the digest the app gave is named in the MCP
// configuration (`X-Context`), and a tool leaves the material out of its reply only for a session that names the app's
// current digest. When the app can't be asked, the session has none and the tools send the material as they always did.
async function ownerContext(name) {
  try {
    const res = await fetch(`${APP_API}/api/v1/agent/context?kind=${encodeURIComponent(name)}`, {
      headers: { Authorization: `Bearer ${agentToken()}` },
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) {
      log(`asking for the ${name} context refused: ${res.status}; the tools send it in their replies`);
      return null;
    }
    const got = await res.json().catch(() => ({}));
    return typeof got.text === "string" && got.text.trim() && typeof got.digest === "string" && got.digest ? got : null;
  } catch (e) {
    log(`asking for the ${name} context failed: ${e.message}; the tools send it in their replies`);
    return null;
  }
}

// Each agent's session: its id prefix, instructions (its own alone), first prompt and tools. The task itself
// comes from the app's tool, so the user prompt is only why this run started.
// `model`, `effort` and `webTools` (the web tools) are per kind; a kind that names none runs on MODEL and EFFORT with the
// whole public web.
// `also` are files shared by several kinds, appended to the system prompt after the kind's own: the one place the
// scoring is explained is read by every agent that scores a posting.
const SCORING = "jobsearch.scoring.instructions.md";
// The parser's second mode (Import your resume on Me): the same session, handed a resume instead of a posting. Its
// instructions come after the posting parser's, and the job's `kind` in what jobs.parse_next gives says which applies.
const PARSER_RESUME = "jobsearch.parser.resume.instructions.md";
// Its third mode: the master resume read once for its structured record when the owner saves a new version.
const PARSER_PROFILE = "jobsearch.parser.profile.instructions.md";
// Its fourth mode: a posting's text read once for its structured record (shadow).
const PARSER_STRUCTURE = "jobsearch.parser.structure.instructions.md";
// Its fifth mode: about 25 cleansed postings ranked against the owner's short profile (shadow).
const PARSER_RANK = "jobsearch.parser.rank.instructions.md";
// Its sixth mode: a site's recorded traffic read for a recipe, on Sonnet.
const PARSER_RECIPE = "jobsearch.parser.recipe.instructions.md";
// Its seventh mode: the master resume condensed into atomic claims, on Sonnet, when the owner saves a new version.
const PARSER_CLAIMS = "jobsearch.parser.claims.instructions.md";
// The kinds of session, with their tools and models. The browser's two kinds were added at the owner's ask on 2026-10-15.
const AGENTS = {
  // A pasted posting (Fill from a posting) or an imported resume (Import your resume): a small model splits it into a
  // target role's fields, or into the master resume's, and reports them, while the owner waits. It has no web tools: it
  // never searches and never opens a link, and the text is all it knows.
  parser: {
    mode: "parser", file: "jobsearch.parser.instructions.md", also: [PARSER_RESUME, PARSER_PROFILE, PARSER_STRUCTURE, PARSER_RANK, PARSER_RECIPE, PARSER_CLAIMS],
    tools: tools("jobs.parse_next", "jobs.parse_report"),
    model: PARSER_MODEL, effort: PARSER_EFFORT, webTools: "",
    prompt: (why) => `This run was started by: ${why}. Call jobs.parse_next for the posting or resume the owner gave, split it into its fields and send them with jobs.parse_report, and go on until none is waiting. Then stop.`,
  },
  // A check of the owner's role tabs (Check my roles): a small model judges the owner's pursued roles, a sample of
  // stopped postings or one pasted posting the way the title pass does, as a dry run, while the owner waits. It has no
  // web tools, like the parser and the title pass.
  checker: {
    mode: "checker", file: "jobsearch.checker.instructions.md", tools: tools("jobs.match_next", "jobs.match_report"),
    model: CHECKER_MODEL, effort: CHECKER_EFFORT, webTools: "",
    prompt: (why) => `This run was started by: ${why}. Call jobs.match_next for the check the owner asked for, judge each item and send your answers with jobs.match_report, and go on until none are left. Then stop.`,
  },
  // Find Gaps (path 38, on My Resume): the analyst's role in a session of its own, which reads the postings for one role (a
  // run the owner asked for and watches), groups the asks into themes and grades each against the whole master resume, or
  // drafts one line from the owner's own words. The public web, read-only, for a title that is not a target role; it
  // changes nothing in the owner's documents.
  gaps: {
    mode: "gaps", file: "jobsearch.gaps.instructions.md",
    tools: tools("jobs.gaps", "jobs.gaps_progress", "jobs.gaps_report"),
    prompt: (why) => `This run was started by: ${why}. Call jobs.gaps for what is waiting: a line to draft from the owner's words, or a run to find the gaps for. Do it, send it with jobs.gaps_report, and go on until nothing is waiting. Then stop.`,
  },
  search: {
    mode: "scout", file: "jobsearch.scout.instructions.md",
    tools: tools("jobs.profile", "jobs.boards", "jobs.board_found", "jobs.report"),
    prompt: (why) => `This run was started by: ${why}. Call jobs.profile for your task: find job boards, then end the search with jobs.report (done). Then stop.`,
  },
  // The title pass (paths 30.1 and 31.1): a small model judges the titles of the postings that passed the gates against
  // the owner's target roles, one batch a session, and answers yes or no for each. It has no web tools: it never searches and never opens a link.
  titles: {
    mode: "titles", file: "jobsearch.titles.instructions.md", tools: tools("jobs.titles_next", "jobs.titles_report"),
    model: TITLES_MODEL, effort: TITLES_EFFORT, webTools: "",
    prompt: (why) => `This run was started by: ${why}. Call jobs.titles_next once for your batch of titles and answer each with jobs.titles_report. Then stop: the next batch is for the next session.`,
  },
  analysis: {
    mode: "analyst", file: "jobsearch.analyst.instructions.md", also: [SCORING], context: "analysis",
    tools: tools("jobs.analysis", "jobs.analysis_submit"),
    prompt: (why) => `This run was started by: ${why}. Call jobs.analysis for the role waiting for research, research it, and send what you found with jobs.analysis_submit. Then stop.`,
  },
  // The employer researcher (path 36): the analyst's role in a small session of its own, which reads an employer's public
  // pages (WebSearch and WebFetch, signed out) and reports its facts with their sources, its job board, and its word on
  // the pairs of records and the splits the app asks it about. Code checks and decides; the owner is asked of the rest.
  researcher: {
    mode: "researcher", file: "jobsearch.researcher.instructions.md", tools: tools("jobs.employer", "jobs.employer_report"),
    model: RESEARCHER_MODEL, effort: RESEARCHER_EFFORT,
    prompt: (why) => `This run was started by: ${why}. Call jobs.employer for the employer waiting for research, research it, and send what you found with jobs.employer_report. Then stop.`,
  },
  // The mail reader (path 14): the analyst's role in a small session that classes a batch of the owner's job mail, from the
  // start of each message's text alone (no web tools: nothing in a message is opened). Code checks every answer and matches
  // the roles; nothing moves on its word alone.
  mail: {
    mode: "mail", file: "jobsearch.mail.instructions.md", tools: tools("jobs.mail", "jobs.mail_report"), webTools: "",
    model: MAIL_MODEL, effort: MAIL_EFFORT,
    prompt: (why) => `This run was started by: ${why}. Call jobs.mail for the messages waiting to be classed, answer each, and send the answers with jobs.mail_report. Call jobs.mail again until it says none are left. Then stop.`,
  },
  // The Coach (interview prep, docs/PRD.md 15.2): a session that takes the prep of an interview, researches the company and
  // the interviewers on the public web (WebSearch and WebFetch, signed out), and sends a report that code checks; or, in a
  // second session, reads that report as the interviewer would and sends findings. Never applies, fills a form or sends.
  coach: {
    mode: "coach", file: "jobsearch.coach.instructions.md",
    also: ["jobsearch.coach.review.instructions.md", "jobsearch.coach.clarify.instructions.md"],
    tools: tools("jobs.coach", "jobs.coach_submit"),
    prompt: (why) => `This run was started by: ${why}. Call jobs.coach for the job waiting for you (an interview prep, or a Find Gaps job to clarify), do what its mode says (write the report, read it as the interviewer, write questions about a run's gaps, or propose master lines from the owner's answers), send it with jobs.coach_submit, and fix whatever it sends back. Then stop.`,
  },
  // A seat: one writing turn the app hands out (a packet's resume or letter, a review of one). Claude gets the app's system
  // text and prompt and answers once, with no tools and no MCP door: everything it needs is in the prompt, and the app, not
  // the session, checks and keeps the answer (`takeSeat`, `answerSeat`). It is still the writer's work, so it reports
  // as the writer.
  seat: {
    mode: "writer", webTools: "", tools: [], seat: true,
    prompt: () => "",
  },
  // The browser worker and the Pathfinder: the door's tools and the DevTools server's, and nothing else (no web tools, no
  // shell, no files). They work the app's queue one item at a time: ask for the next item,
  // do it, hand it back, ask again, until the app says to stop. Their instructions come with the first item, by kind, so
  // the system prompt is the prompt's rules alone.
  browser_reader: {
    mode: "browser_reader", browser: true, webTools: "",
    tools: browserTools("browser.next_item", "browser.hand_back", "browser.heartbeat", "browser.page_digest", "browser.postings"),
    model: BROWSER_READER_MODEL, effort: BROWSER_READER_EFFORT, maxTurns: null, timeoutMs: BROWSER_TIMEOUT_MS,
    untimed: true,
    prompt: (why) => `This run was started by: ${why}. Work the owner's browser items for resumebot, one at a time. Call browser.next_item on the resumebot-browser server and follow the instructions it returns exactly: do the one item, hand it back with browser.hand_back, and call browser.next_item again, until it says stop. Call browser.heartbeat before each page load and wait what it says. Read a page with browser.page_digest first, and take a full snapshot only when it lacks what you need. Use only the resumebot-browser tools and the chrome-devtools tools. ${BROWSER_WORDS} ${BROWSER_TABS} Hand in every posting you see; never judge whether a posting fits the search, the app does that. When it says stop, reply with one line: what you handed back.`,
  },
  pathfinder: {
    mode: "pathfinder", browser: true, webTools: "",
    tools: browserTools("browser.next_item", "browser.hand_back", "browser.heartbeat", "browser.page_digest", "browser.postings", "browser.skill"),
    model: PATHFINDER_MODEL, effort: PATHFINDER_EFFORT, maxTurns: null, timeoutMs: BROWSER_TIMEOUT_MS,
    untimed: true,
    prompt: (why) => `This run was started by: ${why}. Map the owner's job sites in the browser for resumebot, one site at a time. Call browser.next_item on the resumebot-browser server and follow the instructions it returns exactly: map the item's site, hand in its runbook with browser.skill, hand the item back with browser.hand_back, and call browser.next_item again, until it says stop. Call browser.heartbeat before each page load and wait what it says. Read a page with browser.page_digest first, and take a full snapshot only when it lacks what you need. Use only the resumebot-browser tools and the chrome-devtools tools. ${BROWSER_WORDS} ${BROWSER_TABS} Hand in every posting you read; never judge whether a posting fits the search, the app does that. When it says stop, reply with one line: which sites you mapped and which you could not.`,
  },
  // A site project (path 29): the scout's role text is the system prompt, and each task comes with its own
  // instruction. The tools are the site tools but `project.create` (a session starts no project of its own); none
  // writes a check result, because Check is code.
  site: {
    mode: "site", file: "agents.scout.role.instructions.md",
    tools: tools(
      "work.next", "task.next", "task.complete", "task.escalate", "plan.propose", "plan.revise", "item.lookup",
      "review.read", "lesson.list", "lesson.record", "lesson.retire", "act.complete", "skill.try", "doc.read",
    ),
    prompt: (why) => `This run was started by: ${why}. Call work.next for your projects, then task.next with a project_id for a task. Follow its instruction and finish it with the call its report_with names (an item ends with task.complete, which hands you the next task). When nothing is waiting, stop.`,
  },
};

// The model the app named with the last answer about work ("haiku" for the analyst while the stage-one test on Haiku is
// under way, otherwise nothing): that session runs on the small model, as a title session does.
let workModel = null;

async function sessionKind(work, reason) {
  const agent = AGENTS[work];
  // The public web (the title pass: none at all), no shell, no files.
  const webTools = agent.webTools ?? "WebSearch,WebFetch";
  const held = agent.context ? await ownerContext(agent.context) : null;
  return {
    mode: agent.mode,
    prefix: agent.mode,
    isSeat: !!agent.seat,
    seat: null, // the seat the app hands out, put here by `run` (`takeSeat`) once the session has said it started
    // A parser session the app says is for a recipe runs on the default model (Sonnet) at its default effort.
    model: (work === "parser" && workModel === "sonnet" ? MODEL : agent.model) || MODEL,
    effort: (work === "parser" && workModel === "sonnet" ? EFFORT : agent.effort) || EFFORT,
    server: "jobsearch",
    browser: !!agent.browser,
    // The browser's kinds have no turn cap (null): the app boxes each item's time instead.
    maxTurns: agent.maxTurns === null ? null : agent.maxTurns || MAX_TURNS,
    timeoutMs: agent.untimed ? agent.timeoutMs : agent.timeoutMs || RUN_TIMEOUT_MS,
    system: [agent.file ? (await instructionTexts([agent.file, ...(agent.also || [])])).join("\n\n") : "", held?.text]
      .filter(Boolean).join("\n\n"),
    contextDigest: held ? held.digest : null,
    prompt: agent.prompt(reason),
    webTools,
    allowed: [...agent.tools, ...webTools.split(",").filter(Boolean)],
    doorTools: agent.tools.map((t) => t.replace(/^mcp__jobsearch__/, "")).join(","),
  };
}

// What the runner makes of the app's answer: a kind of work it has a session for, or nothing. A reply naming another kind
// is the app's slip: nothing is waiting for this runner, and the log says so the first time, not once a minute.
let otherKindSaid = false;
function poolWork(next) {
  if (next && Object.hasOwn(AGENTS, next)) return next;
  if (next && !otherKindSaid) {
    otherKindSaid = true;
    log(`the app named ${next} work for the ${POOL} lane; it takes ${Object.keys(AGENTS).join(", ")} alone, so nothing is waiting for it`);
  }
  return null;
}

// Which work waits, as the app says: "parser", "checker", "gaps", "search", "titles", "analysis", "researcher", "coach", "site", "seat",
// "browser_reader", "pathfinder" or null.
// Undefined when the app can't be asked. The runner asks for the pool's work (`?only=any`).
// The item the app claimed for this runner with its answer (`claim`, and the ids of `item`), or null: the session started
// for it names the claim (`X-Claim`) and its first prompt names the item, so its own take-next call returns exactly that.
let workClaim = null;
// A command the app's dispatcher sent (docs/PRD.md 47.1, a signed wake with `claim`): the item is already claimed for this
// runner, so the run it starts does not ask. Taken by the run that starts for it (`chooseWork`).
let commanded = null;

async function askWork() {
  workClaim = null;
  try {
    const res = await fetch(`${APP_API}/api/v1/agent/work?only=${POOL}${/^session-\d+$/.test(RUNNER) ? `&runner=${RUNNER}` : ""}`, {
      headers: { Authorization: `Bearer ${agentToken()}` },
      signal: AbortSignal.timeout(10_000),
    });
    const reply = await res.json().catch(() => ({}));
    workModel = res.ok && reply.model === "sonnet" ? reply.model : null;
    workClaim = res.ok && reply.claim ? { id: String(reply.claim), ids: Array.isArray(reply.item?.ids) ? reply.item.ids.map(String) : [] } : null;
    if (res.ok && "next" in reply) return poolWork(reply.next);
    log(`asking for work refused: ${res.status}`);
  } catch (e) {
    log(`asking for work failed: ${e.message}`);
  }
  return undefined;
}

// The kind of work a wake's reason names, for when the app can't be asked. A wake that says a search started is a
// search; any other reason (the scheduled check-in too) names no search, so a check-in never spends a session on a
// search that may not exist.
function wakeWork(reason) {
  if (/parse_asked/.test(reason) && !/search_started/.test(reason)) return "parser";
  if (/check_asked/.test(reason) && !/search_started/.test(reason)) return "checker";
  if (/gaps_asked/.test(reason) && !/search_started/.test(reason)) return "gaps";
  if (/packet_asked/.test(reason) && !/search_started/.test(reason)) return "seat";
  if (/analysis_asked/.test(reason) && !/search_started/.test(reason)) return "analysis";
  if (/employers_asked/.test(reason) && !/search_started/.test(reason)) return "researcher";
  if (/mail_asked/.test(reason) && !/search_started/.test(reason)) return "mail";
  if (/coach_asked/.test(reason) && !/search_started/.test(reason)) return "coach";
  if (/site_project/.test(reason) && !/search_started/.test(reason)) return "site";
  if (/tag_roles/.test(reason) && !/search_started/.test(reason)) return "titles";
  if (/search_started/.test(reason)) return "search";
  return null;
}

// The work for this run: the app's word, or, when it can't be asked, the wake's reason (`wakeWork`). With the app out of
// reach, a reason that names no kind of work starts nothing.
async function chooseWork(reason) {
  if (commanded) {
    const told = commanded;
    commanded = null;
    workModel = told.model === "sonnet" ? "sonnet" : null;
    workClaim = { id: told.claim, ids: told.ids };
    const named = poolWork(told.kind);
    if (named) return named;
    workClaim = null; // not a kind this runner has a session for: the claim lapses with its lease
    return null;
  }
  const asked = await askWork();
  if (asked !== undefined) return asked;
  const named = wakeWork(reason);
  if (named) return named;
  log(`the app can't be reached; no session for: ${reason}`);
  return null;
}

// The models a seat can name: the app says "opus" or "sonnet", the install says which Claude model each is.
const SEAT_MODELS = { opus: process.env.SCOUT_OPUS_MODEL || "claude-opus-5-5", sonnet: MODEL };

// The claude command line for a seat: one turn, the app's own system text, no tools and no MCP door. Caching is off in the
// environment (`run`): the turn is one call, so a cache write would only cost more.
function seatArgs(seat) {
  return [
    "-p", seat.prompt, "--model", SEAT_MODELS[seat.model] || MODEL, "--system-prompt-file", SYSTEM_FILE,
    "--tools", "", "--strict-mcp-config", "--output-format", "json",
    ...(seat.effort ? ["--effort", seat.effort] : []),
  ];
}

// The claude command line for one session. The instructions are not on it: `run` writes them to SYSTEM_FILE first.
// The strict MCP config and each kind's tool lists are the roles' boundary, with the Claude folder and the empty working
// folder of a local lane.
function claudeArgs(kind) {
  if (kind.seat) return seatArgs(kind.seat);
  return [
    "-p", kind.prompt, "--model", kind.model, "--append-system-prompt-file", SYSTEM_FILE,
    ...(kind.maxTurns ? ["--max-turns", kind.maxTurns] : []),
    ...(kind.effort && kind.effort !== "none" ? ["--effort", kind.effort] : []),
    "--mcp-config", MCP_CONFIG, "--strict-mcp-config",
    "--tools", kind.webTools, "--allowedTools", ...kind.allowed,
    // Sessions are kept (under /home/node/.claude) so a run's reasoning can be traced.
    "--output-format", "json",
  ];
}

// The session's instructions file is removed as soon as the session is over, however it ended (a file a killed runner
// left behind is removed before the next session writes its own).
function dropSystemFile() {
  try {
    rmSync(SYSTEM_FILE, { force: true });
  } catch (e) {
    log(`couldn't remove the session's instructions file: ${e.message}`);
  }
}

// Windows has no file modes: `mode: 0o600` does nothing there, and a file made in a folder outside the user's profile inherits
// whoever that folder lets in (other accounts, the Users group, SYSTEM, administrators). So on Windows the access is cut down
// to this account with icacls (as stack.py's only_me does for a folder), and a failure stops the session: the file holds the
// app's token or the owner's own rules, and a session that cannot keep it private does not start.
let account = "";
function onlyMe(file) {
  const system32 = join(process.env.SystemRoot || "C:\\Windows", "System32");
  if (!account) {
    const who = spawnSync(join(system32, "whoami.exe"), ["/user", "/fo", "csv", "/nh"], { encoding: "utf8", windowsHide: true, timeout: 10_000 });
    account = String(who.stdout || "").split(",")[1]?.trim().replaceAll('"', "") || "";
    if (!/^S-1-/.test(account)) {
      account = "";
      throw new Error("could not tell which account this runner is (whoami), so a file cannot be limited to it");
    }
  }
  // `/inheritance:r` drops what the folder passes on. A file made by a process whose token carries its own default access (an
  // elevated one: SYSTEM and Administrators as well as the account) holds those as its own entries, which only /remove takes off.
  const others = ["S-1-5-18", "S-1-5-32-544", "S-1-5-32-545", "S-1-1-0", "S-1-5-11"].filter((sid) => sid !== account); // SYSTEM, administrators, users, everyone, signed-in users
  const done = spawnSync(join(system32, "icacls.exe"), [file, "/inheritance:r", "/grant:r", `*${account}:F`, ...others.flatMap((sid) => ["/remove:g", `*${sid}`])], { windowsHide: true, stdio: "ignore", timeout: 10_000 });
  if (done.status !== 0) throw new Error(`could not limit ${file} to this account (icacls exited ${done.status})`);
}

// A file only this runner can read, with `text` in it. It is made empty first, so that nothing is in it before it is private.
// A file that already exists keeps the mode it was made with (the callers remove an old one, or know it is theirs).
function writePrivate(file, text) {
  writeFileSync(file, "", { mode: 0o600 });
  if (WINDOWS) onlyMe(file);
  writeFileSync(file, text);
}

// Claude Code's cache keeps what a session wrote for 1 hour on a subscription, and a write for an hour costs 2x the input price
// where one for 5 minutes costs 1.25x. A session's turns are seconds apart (the longest gap is the writer composing a document,
// a minute or two) and the lanes re-read the shared prefix every few seconds, so 5 minutes is enough: it cuts the cost of
// cache writes by 37.5%.
const PROMPT_CACHE_TTL = "5m";
// The tools that finish a session (end-session.mjs), as the hook's matcher names them.
const END_HOOK_TOOLS = [
  "mcp__jobsearch__jobs_analysis_submit",
  "mcp__jobsearch__jobs_employer_report", "mcp__resumebot-browser__browser_next_item",
];

// The settings a lane's Claude folder holds: the cache lifetime, and the hook that ends a session when its work is accepted
// (a PostToolUse hook that prints `{"continue": false}`: no closing summary turn, which re-reads the whole context to write a
// paragraph nobody reads). Merged into what the folder already has (other keys stay, an older copy of
// this hook is replaced) and written only when it changes. Paths use forward slashes: Claude Code runs a hook command in a Unix
// shell, also on Windows, where a backslash would be read as an escape.
function claudeSettings(dir, { node = process.execPath, hook = END_HOOK } = {}) {
  const file = join(dir, "settings.json");
  let settings = {};
  try {
    const read = JSON.parse(readFileSync(file, "utf8"));
    if (read && typeof read === "object" && !Array.isArray(read)) settings = read;
  } catch { /* none yet, or not JSON: start from nothing */ }
  const command = `"${node.replace(/\\/g, "/")}" "${hook.replace(/\\/g, "/")}"`;
  const others = (Array.isArray(settings.hooks?.PostToolUse) ? settings.hooks.PostToolUse : [])
    .filter((entry) => !JSON.stringify(entry).includes("end-session.mjs"));
  const next = {
    ...settings, promptCacheTtl: PROMPT_CACHE_TTL,
    hooks: { ...settings.hooks, PostToolUse: [...others, { matcher: END_HOOK_TOOLS.join("|"), hooks: [{ type: "command", command }] }] },
  };
  if (JSON.stringify(next) !== JSON.stringify(settings)) writeFileSync(file, `${JSON.stringify(next, null, 2)}\n`);
  return next;
}

// A browser session's two servers: the app's browser door, behind its own token, told which session calls (the app keeps
// each session's run apart by it), and the DevTools MCP server, which attaches to the one shared Chrome (`ensureBrowser`)
// and leaves it open when the session ends. On Windows npx is a .cmd file, which only cmd.exe runs.
function browserServers({ apiUrl = APP_API, token = secret("browser-token"), pkg = BROWSER_MCP, browserUrl = BROWSER_URL, sessionId = "", claim = "", windows = WINDOWS } = {}) {
  // Only the tools a reader uses: navigation, input and debugging (snapshot, evaluate_script). The performance, emulation,
  // network and memory categories are about 15 tool definitions that no instruction names, sent on every turn.
  // Through the launcher's bridge a session connects to an address that names it (docs/PRD.md 52.2): the bridge knows whose
  // each tab is, and registers it with the app. Otherwise the DevTools port is named as it always was.
  const target = BROWSER_BRIDGE && sessionId
    ? ["--wsEndpoint", `ws://127.0.0.1:${BROWSER_PORT}/devtools/browser/${encodeURIComponent(sessionId)}?key=${encodeURIComponent(bridgeKey())}`]
    : ["--browserUrl", browserUrl];
  const args = ["-y", pkg, ...target, "--usageStatistics=false", "--performanceCrux=false",
    "--no-category-performance", "--no-category-emulation", "--no-category-network", "--no-category-memory"];
  return {
    "resumebot-browser": {
      type: "http", url: `${apiUrl}/mcp-browser/`,
      headers: { Authorization: `Bearer ${token}`, ...(sessionId ? { "X-Session-Id": sessionId } : {}), ...(claim ? { "X-Claim": claim } : {}), ...(NAMED ? { "X-Runner": `${NAMED}/${RUNNER_START}` } : {}) },
    },
    "chrome-devtools": {
      type: "stdio", ...(windows ? { command: "cmd", args: ["/c", "npx", ...args] } : { command: "npx", args }),
      env: { CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS: "1" },
    },
  };
}

// The shared browser, through the launcher's bridge (lanes-local.mjs runs it; it starts the profile's Chrome itself, or
// attaches the owner's): "" when a browser session can attach, else the one line Wake up and Run say (the bridge is not
// running, or open Chrome, switch remote debugging on, or Allow it).
async function ensureBrowser() {
  try {
    const res = await fetch(`${BROWSER_URL}/json/version`, { headers: bridgeHeaders(), signal: AbortSignal.timeout(OWN_CHROME_WAIT_MS) });
    if (res.ok) return "";
    const said = await res.json().catch(() => ({}));
    return String(said.why || `the browser bridge answered ${res.status}`);
  } catch (e) {
    return `the browser bridge is not running on ${BROWSER_URL} (lanes-local.mjs starts it): ${e.message}`;
  }
}

// Why the owner's Chrome cannot take browser jobs now, from the bridge without connecting ("" when it can, or may), or
// null when there is no answer (no bridge, or SCOUT_BROWSER=profile). Said to the app between sessions.
async function ownBrowserStatus() {
  if (BROWSER_MODE !== "own") return null;
  try {
    const res = await fetch(`${BROWSER_URL}/status`, { headers: bridgeHeaders(), signal: AbortSignal.timeout(2000) });
    return res.ok ? String((await res.json()).why || "") : null;
  } catch {
    return null;
  }
}

let running = false;
// The Claude process of the session running now, so a blackout can stop it.
let current = null;
// One folded follow-up: a wake that arrives mid-run isn't lost, and two are one. Its reason stays short.
let pending = null;
// A runner that hit Claude's usage limit asks the app for nothing before this time (ms since the epoch; 0: not held).
let holdUntil = 0;
// Whether the runner has said there is nothing waiting since it last had work (see `run`).
let laneIdleSaid = false;

function requestRun(reason) {
  if (running) {
    if (!pending) pending = reason;
    else if (pending.length + reason.length < PENDING_MAX) pending = `${pending}+${reason}`;
    log(`run requested (${reason}) while one is going; queued one follow-up`);
    return;
  }
  // Held at the usage limit (see `holdLane`): nothing asks the app for work, on a check-in or otherwise.
  if (Date.now() < holdUntil) return;
  void run(reason);
}

// Ends a session. Elsewhere that is the signal; on Windows there are no signals, and ending the Claude process leaves what it
// started (the processes of its own sessions and tools) running, so the whole tree goes, at once, whatever the signal says.
function killChild(child, signal) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  if (!WINDOWS) {
    child.kill(signal);
    return;
  }
  if (!child.pid) return;
  const taskkill = join(process.env.SystemRoot || "C:\\Windows", "System32", "taskkill.exe");
  const ended = spawnSync(taskkill, ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore", timeout: 10_000 });
  // 128 is "no such process": it ended on its own just now. Any other failure: the one process, at least.
  if (ended.status !== 0 && ended.status !== 128) child.kill();
}

// Ask a session to stop, and kill it if it hasn't ended KILL_GRACE_MS later: a session that ignores SIGTERM would
// otherwise hold the runner for good (`running` stuck true while /health says all is well).
function stopSession(child, mode) {
  if (child.exitCode !== null || child.signalCode !== null || child.stopping) return;
  child.stopping = true;
  killChild(child, "SIGTERM");
  const hard = setTimeout(() => {
    if (child.exitCode === null && child.signalCode === null) {
      log(`${mode} run did not stop on SIGTERM; killing it`);
      killChild(child, "SIGKILL");
      // A process the session started could hold its pipes open: the run ends with the session, not with them.
      child.stdout?.destroy();
      child.stderr?.destroy();
    }
  }, KILL_GRACE_MS);
  child.once("exit", () => clearTimeout(hard));
}

// A session that ended at the Claude subscription's usage limit says so in Claude Code's own words, as its result:
// "You've hit your session limit · resets 3pm (UTC)", "You've hit your weekly limit · resets Oct 20, 3pm (UTC)", "You're
// out of usage credits · resets 3pm (UTC)", "You're out of extra usage", "Your org is out of usage" (as Claude Code
// 2.1.197 words them), or, from an older one, "Claude AI usage limit reached|1760000000". Only
// waiting cures it, so the app is told once, on the session's "ended" heartbeat (`limitReport`), and starts no Claude
// work before it lifts. Only a session that failed counts: one that merely talks about a limit in its answer did not
// stop on one. `usageLimit` reads the words once and gives the app both of what it keeps: `words`, Claude's own words
// from where the limit is named to the end of that line (the heartbeat's `usage_limit`: the app ends the night's runs
// cleanly, keeping what they found, docs/PRD.md 25.12), and `resets_at`, the instant it lifts (ISO, UTC), or null when
// the words give no time (the heartbeat's `limit`: a catch-up after a tracker import waits for it, `catch_up.py`). The
// CLI prints the time in a zone it names after it ("resets 3pm (America/Los_Angeles)"): the time is read in that zone, and in
// this process's own only when the words name none (or one this runtime does not know). Not a
// usage limit: the service limiting requests for a moment ("Server is temporarily limiting requests (not your usage
// limit)"), which the next session gets past.
const LIMIT_WORDS = /you['’]ve (?:hit|reached) your [^\n]{0,40}\blimit\b|you['’]ve (?:hit|reached) your [^\n·]{0,60}?limit|you['’]re out of (?:extra )?usage|your org is out of usage|usage limit reached/i;
const NOT_A_USAGE_LIMIT = /temporarily limiting requests|not your usage limit/i;
const RESETS = /\bresets?\s+(?:(?:on\s+)?([a-z]{3,9})\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?,?\s+(?:at\s+)?)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b(?:\s*\(([A-Za-z][A-Za-z0-9_+\-/]*)\))?/i;
const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
// A limit lasts a week at most: a time further off than that was misread, and the app is told no time rather than one.
const LIMIT_LONGEST_MS = 8 * 24 * 3600 * 1000;

// --- zone arithmetic (begin; the tests read this block) ---
function zoneKnown(zone) {
  try { new Intl.DateTimeFormat("en-US", { timeZone: zone }); return true; } catch { return false; }
}
// The wall clock of `zone` at the instant `at` (ms): { y, mo (0-11), d, h, mi, s }.
function wallClock(zone, at) {
  const f = new Intl.DateTimeFormat("en-US", { timeZone: zone, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", second: "numeric" });
  const o = {};
  for (const part of f.formatToParts(at)) if (part.type !== "literal") o[part.type] = Number(part.value);
  return { y: o.year, mo: o.month - 1, d: o.day, h: o.hour, mi: o.minute, s: o.second };
}
function zoneOffsetMs(zone, at) {
  const w = wallClock(zone, at);
  return Date.UTC(w.y, w.mo, w.d, w.h, w.mi, w.s) - Math.floor(at / 1000) * 1000;
}
// The instants (ms, ascending) at which the wall clock of `zone` reads that day and time: two in the hour that repeats when
// the clocks go back, and, in the hour they skip, the instant that time would have been on the clock before the change.
function wallInstants(zone, y, mo, d, h, mi) {
  const naive = Date.UTC(y, mo, d, h, mi);
  const found = new Set();
  for (const near of [naive - 86_400_000, naive + 86_400_000]) {
    const at = naive - zoneOffsetMs(zone, near);
    if (zoneOffsetMs(zone, at) === naive - at) found.add(at);
  }
  if (!found.size) found.add(naive - zoneOffsetMs(zone, naive - 86_400_000));
  return [...found].sort((a, b) => a - b);
}
// --- zone arithmetic (end) ---

function limitResets(text, now) {
  const epoch = /\|\s*(\d{9,13})\b/.exec(text);
  let when = null;
  if (epoch) {
    const n = Number(epoch[1]);
    when = new Date(n < 1e11 ? n * 1000 : n);
  } else {
    const m = RESETS.exec(text);
    if (!m) return null;
    const hour = (Number(m[4]) % 12) + (m[6].toLowerCase() === "p" ? 12 : 0);
    const minute = Number(m[5] || 0);
    const zone = m[7] && zoneKnown(m[7]) ? m[7] : Intl.DateTimeFormat().resolvedOptions().timeZone;
    const today = wallClock(zone, now);
    let at;
    if (m[1]) {
      const month = MONTHS.indexOf(m[1].slice(0, 3).toLowerCase());
      if (month < 0) return null;
      const on = (year) => wallInstants(zone, year, month, Number(m[2]), hour, minute);
      let instants = on(m[3] ? Number(m[3]) : today.y);
      if (!m[3] && instants[instants.length - 1] < now - 36 * 3600 * 1000) instants = on(today.y + 1);
      at = instants.find((t) => t > now) ?? instants[instants.length - 1];
    } else {
      at = wallInstants(zone, today.y, today.mo, today.d, hour, minute).find((t) => t > now)
        ?? wallInstants(zone, today.y, today.mo, today.d + 1, hour, minute)[0];
    }
    when = new Date(at);
  }
  const at = when.getTime();
  return Number.isFinite(at) && at > now && at - now <= LIMIT_LONGEST_MS ? when : null;
}

function usageLimit(text, now = Date.now()) {
  const said = String(text || "");
  const found = LIMIT_WORDS.exec(said);
  if (!found || NOT_A_USAGE_LIMIT.test(said)) return null;
  const when = limitResets(said, now);
  return {
    resets_at: when ? when.toISOString() : null,
    text: said.replace(/\s+/g, " ").trim().slice(0, 200),
    words: said.slice(found.index, found.index + 240).split("\n")[0].trim(),
  };
}

// What a session's "ended" heartbeat adds when it ended at the usage limit (`usageLimit`): nothing otherwise.
function limitReport(limit) {
  return limit ? { usage_limit: limit.words, limit: { resets_at: limit.resets_at, text: limit.text } } : {};
}

// What a finished session cost, as Claude Code's result prints it (`total_cost_usd`, at API prices) with its token counts;
// null when the result says neither. The app counts the night's dollar budget from it (`budget.py`).
function spendOf(parsed) {
  const u = parsed && typeof parsed.usage === "object" && parsed.usage ? parsed.usage : {};
  const cost = Number(parsed && parsed.total_cost_usd);
  const input = Number(u.input_tokens || 0) + Number(u.cache_creation_input_tokens || 0) + Number(u.cache_read_input_tokens || 0);
  const output = Number(u.output_tokens || 0);
  if (!(cost > 0) && !input && !output) return null;
  return { cost_usd: cost > 0 ? cost : null, input_tokens: input, output_tokens: output };
}

// What a session's "ended" heartbeat adds about its cost (`spendOf`): nothing when the result gave none.
function spendReport(spend) {
  return spend ? { spend } : {};
}

// How long the runner is held when the limit's words gave no time for it to lift.
const LIMIT_HOLD_MS = 30 * 60_000;

// A runner whose session ended at the usage limit asks the app for no work until the limit lifts, or for half an hour
// when the words gave no time. A hold is said once, and only ever gets longer.
function holdLane(limit) {
  const until = limit.resets_at ? Date.parse(limit.resets_at) : Date.now() + LIMIT_HOLD_MS;
  if (!(until > holdUntil)) return;
  holdUntil = until;
  log(`usage limit: holding until ${new Date(until).toISOString()}`);
}

// The user's own Claude Code on Windows, found each time a session starts and never kept: the Claude desktop app's copy lives
// in a folder that is new with every update, and the old one is removed. In this order: `claude.exe` on the PATH (never a .cmd
// shim), the native installer's place, WinGet's, then the copy inside the Claude desktop app (`findDesktopClaude`, in each folder
// of `desktopClaudeRoots`: a whole copy beats one that cannot be told whole, then the newest version wins).
// Returns { path, via } or null.
function findClaude(env = process.env) {
  const isFile = (p) => { try { return statSync(p).isFile(); } catch { return false; } };
  const home = env.USERPROFILE || homedir();
  for (const dir of String(env.PATH || "").split(";").map((d) => d.trim().replace(/^"(.*)"$/, "$1")).filter(Boolean)) {
    if (isFile(join(dir, "claude.exe"))) return { path: join(dir, "claude.exe"), via: "the PATH" };
  }
  const places = [
    [join(home, ".local", "bin", "claude.exe"), "the native install"],
    [join(env.LOCALAPPDATA || join(home, "AppData", "Local"), "Microsoft", "WinGet", "Links", "claude.exe"), "WinGet"],
  ];
  for (const [path, via] of places) if (isFile(path)) return { path, via };
  const copies = desktopClaudeRoots(env, home).map(findDesktopClaude).filter(Boolean);
  copies.sort((a, b) => (Boolean(b.proven) - Boolean(a.proven)) || compareVersions(b.version, a.version));
  return copies[0] ?? null;
}

// The Claude desktop app is a packaged app: what it keeps under %APPDATA%\Claude is really in its package's own folder,
// %LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude. A program started inside the app sees it at %APPDATA%; one started
// from a terminal, by hand or by the launcher does not, and sees only the package folder. Newer versions keep their copies
// in %LOCALAPPDATA%\Claude-Data\claude_<package id>\claude-code instead. All three are looked in.
function desktopClaudeRoots(env, home) {
  const roots = [join(env.APPDATA || join(home, "AppData", "Roaming"), "Claude", "claude-code")];
  const packages = join(env.LOCALAPPDATA || join(home, "AppData", "Local"), "Packages");
  let names = [];
  try { names = readdirSync(packages).filter((n) => /^Claude_[a-z0-9]+$/i.test(n)); } catch { /* no packages folder */ }
  for (const name of names) roots.push(join(packages, name, "LocalCache", "Roaming", "Claude", "claude-code"));
  const data = join(env.LOCALAPPDATA || join(home, "AppData", "Local"), "Claude-Data");
  try {
    for (const name of readdirSync(data).filter((n) => /^claude_[a-z0-9]+$/i.test(n))) roots.push(join(data, name, "claude-code"));
  } catch { /* no Claude-Data folder */ }
  return roots;
}

// More than 0 when version a is newer than version b, by number and not by name ("2.1.1000" is newer than "2.1.295").
function compareVersions(a, b) {
  const [x, y] = [a, b].map((v) => v.split(".").map(Number));
  for (let i = 0; i < Math.max(x.length, y.length); i++) if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) - (y[i] || 0);
  return 0;
}

// The Claude desktop app keeps its copies at <root>\<version>\<build>\claude.exe, one folder for each update, and a `.payload`
// beside the program that says how big it should be. The newest version whose copy is whole wins: one that is still being
// written (or damaged) is passed over for the version before it, as is one with no `.payload` to prove it, unless nothing else
// is there. Returns { path, via, version, proven } (proven: the `.payload` vouches for it) or null.
function findDesktopClaude(root) {
  let versions;
  try { versions = readdirSync(root).filter((v) => /^\d+(?:\.\d+){1,3}$/.test(v)); } catch { return null; }
  versions.sort((a, b) => compareVersions(b, a));
  let unproven = null;
  for (const version of versions) {
    let builds;
    try { builds = readdirSync(join(root, version)); } catch { continue; }
    const modified = (b) => { try { return statSync(join(root, version, b)).mtimeMs; } catch { return 0; } };
    for (const build of builds.sort((a, b) => modified(b) - modified(a))) {
      const exe = join(root, version, build, "claude.exe");
      let size;
      try {
        const st = statSync(exe);
        if (!st.isFile()) continue;
        size = st.size;
      } catch { continue; }
      let promised = null;
      try { promised = JSON.parse(readFileSync(join(root, version, build, ".payload"), "utf8")).size; } catch { /* none to read */ }
      if (typeof promised === "number" && promised !== size) continue;
      const found = { path: exe, via: `the Claude desktop app, ${version}`, version };
      if (typeof promised === "number") return { ...found, proven: true };
      unproven ??= found;
    }
  }
  return unproven;
}

const CLAUDE_NOT_FOUND = "Claude Code was not found on this PC: install it, or open the Claude desktop app once (looked for claude.exe on the PATH, in the native install's and WinGet's folders and in the Claude desktop app's)";
// The Claude `findClaude` found last, so the log says when it changes (an update of the Claude desktop app) and not each time.
let claudeSaid = "";

// What a session starts: { file, before } (`before` goes ahead of the session's own arguments), or null when Claude Code is
// not on this PC. SCOUT_CLAUDE_BIN says which when it is set; else on Windows `findClaude` looks, now; else it is `claude` on the
// PATH, as in the image. A .js, .mjs or .cjs file runs under this Node.
function claudeCommand() {
  let file = CLAUDE_BIN;
  if (WINDOWS && !process.env.SCOUT_CLAUDE_BIN) {
    const found = findClaude();
    if (!found) return null;
    file = found.path;
    if (file !== claudeSaid) log(`claude is ${file} (from ${found.via})`);
    claudeSaid = file;
  }
  return /\.[cm]?js$/i.test(file) ? { file: process.execPath, before: [file] } : { file, before: [] };
}

// A session started under jobwrap.py is in a Windows Job Object that ends everything in it when the session does (docs/PRD.md
// 52.6: its MCP servers and git, which `taskkill /T` does not always reach); `taskkill /T` stays as the fallback. The local
// launcher sets SCOUT_JOB_WRAP=1; SCOUT_JOBWRAP_PYTHON names the Python when `py -3` is not there. Without a Python the
// session starts as it did, and the log says so once.
const JOB_WRAP = WINDOWS && (process.env.SCOUT_JOB_WRAP || "").trim() === "1";
let jobWrapper; // undefined: not looked for yet; null: none found
function findJobWrapper() {
  const script = join(dirname(fileURLToPath(import.meta.url)), "jobwrap.py");
  if (!existsSync(script)) return null;
  const named = (process.env.SCOUT_JOBWRAP_PYTHON || "").trim();
  for (const [file, before] of named ? [[named, []]] : [["py", ["-3"]], ["python", []]]) {
    const probe = spawnSync(file, [...before, "-c", "pass"], { windowsHide: true, stdio: "ignore", timeout: 10_000 });
    if (probe.status === 0) return { file, before: [...before, script] };
  }
  return null;
}
function jobWrapped(command) {
  if (!JOB_WRAP) return command;
  if (jobWrapper === undefined) {
    jobWrapper = findJobWrapper();
    if (!jobWrapper) log("no Python for the job object wrapper (jobwrap.py): sessions start without one; taskkill /T ends their trees");
  }
  return jobWrapper ? { file: jobWrapper.file, before: [...jobWrapper.before, "--", command.file, ...command.before] } : command;
}

// Why a session cannot start in its working folder, or "" when it can. On a local computer the folder holds nothing (see the header): what
// is in it is read by Claude Code as the project's own.
function workdirProblem() {
  if (!CLAUDE_CONFIG_DIR) return "";
  try {
    const names = readdirSync(WORKDIR);
    if (!names.length) return "";
    // Moved, never deleted, to work-leftovers/<lane>/<UTC time>/ beside the work folders; one log line; the session starts.
    const to = join(dirname(dirname(WORKDIR)), "work-leftovers", basename(WORKDIR), new Date().toISOString().replace(/[:.]/g, "-"));
    mkdirSync(to, { recursive: true });
    for (const name of names) renameSync(join(WORKDIR, name), join(to, name));
    log(`moved ${names.length} stray file(s) out of ${WORKDIR} (${names.slice(0, 5).join(", ")}${names.length > 5 ? ", ..." : ""}) to ${to}`);
    return "";
  } catch (e) {
    return `the working folder cannot be read or emptied: ${e.message}`;
  }
}

// One Claude session: resolves with how it ended. `timedOut` says the run limit stopped it.
// Why a session failed, as a code, never Claude's words: the log keeps the reason and lengths, not the answer.
const AUTH_WORDS = /\b401\b|authenticat|not logged in|please run \/login|invalid (api key|x-api-key|token)|oauth token|token has expired/i;
function endReason(result, parsed, limit, failed) {
  if (!failed) return "reason=ok";
  if (result.timedOut) return "reason=timeout";
  if (limit) return "reason=usage-limit";
  const words = [parsed && parsed.result, result.err, parsed ? "" : result.out].filter(Boolean).join("\n");
  if (AUTH_WORDS.test(words)) return "reason=auth-failure";
  if (result.own) return "reason=could-not-start";
  if (!parsed) return result.out.trim() ? "reason=unreadable-answer" : "reason=no-output";
  if (result.code) return `reason=exit-${result.code}`;
  return "reason=claude-reported-error";
}

function runClaude(kind, env) {
  return new Promise((resolve) => {
    const found = claudeCommand();
    if (!found) {
      resolve({ code: -1, signal: null, out: "", err: CLAUDE_NOT_FOUND, own: true, timedOut: false });
      return;
    }
    const command = jobWrapped(found);
    const problem = workdirProblem();
    if (problem) {
      resolve({ code: -1, signal: null, out: "", err: problem, own: true, timedOut: false });
      return;
    }
    const child = spawn(command.file, [...command.before, ...claudeArgs(kind)], {
      env, cwd: WORKDIR, windowsHide: true,
      // Windows: no input at all (see the header). Elsewhere the session has the pipe it always had.
      ...(WINDOWS ? { stdio: ["ignore", "pipe", "pipe"] } : {}),
    });
    current = child;
    let out = "";
    let err = "";
    let timedOut = false;
    child.stdout.on("data", (d) => { out += d; });
    child.stderr.on("data", (d) => { err += d; });
    // A browser session has no wall clock unless the install sets one (kind.timeoutMs 0).
    const timer = kind.timeoutMs > 0 ? setTimeout(() => {
      timedOut = true;
      log(`${kind.mode} run timed out after ${Math.round(kind.timeoutMs / 1000)}s; stopping it`);
      stopSession(child, kind.mode);
    }, kind.timeoutMs) : null;
    child.on("error", (e) => { clearTimeout(timer); resolve({ code: -1, signal: null, out, err: String(e.message), own: true, timedOut }); });
    child.on("close", (code, signal) => { clearTimeout(timer); resolve({ code, signal, out, err, timedOut }); });
  });
}

// However a run ends: the runner is free again, and what is waiting goes next. A wake that came in during the run goes
// first (the folded follow-up); otherwise, when the session went well and more work waits (a queue of roles to
// research, packets to write, a night's titles and postings), the next session starts at once, and the chain goes on for
// as long as the app names work. A failed session starts no chain: an expired token or Claude's usage limit would fail
// every session after it.
async function finish(chainOn) {
  running = false;
  try {
    if (pending) {
      const why = pending;
      pending = null;
      requestRun(`follow-up: ${why}`);
    } else if (chainOn) {
      const next = await askWork();
      if (next) requestRun(`more work: ${next}`);
    }
  } catch (e) {
    log(`after the run: ${e.message}`);
  }
}

// Asks the app for the seat waiting: the seat, or null when none is (another session took it first) or the app can't say.
async function takeSeat(sessionId, claim = null) {
  try {
    const res = await fetch(`${APP_API}/api/v1/agent/seat/take`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${agentToken()}` },
      body: JSON.stringify({ session: sessionId, ...(claim ? { claim: claim.id } : {}) }),
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) {
      log(`taking a seat refused: ${res.status}`);
      return null;
    }
    const reply = await res.json().catch(() => ({}));
    return reply && reply.seat ? reply.seat : null;
  } catch (e) {
    log(`taking a seat failed: ${e.message}`);
    return null;
  }
}

// Hands the app a seat's answer: the text Claude wrote, or the reason there is none. An answer that does not reach the
// app is logged and not fatal: the app puts the seat back by itself when its session goes quiet.
async function answerSeat(sessionId, seat, body) {
  try {
    const res = await fetch(`${APP_API}/api/v1/agent/seat/${encodeURIComponent(seat.id)}/answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${agentToken()}` },
      body: JSON.stringify({ session: sessionId, ...body }),
      signal: AbortSignal.timeout(30_000),
    });
    if (!res.ok) log(`the answer to seat ${seat.id} was refused: ${res.status}`);
  } catch (e) {
    log(`the answer to seat ${seat.id} could not be sent: ${e.message}`);
  }
}

async function run(reason) {
  running = true;
  let sessionId = null;
  let mode = "run";
  let beat = null;
  let chainOn = false;
  // Set when the session ended at the subscription's usage limit (`usageLimit`): the app is told with the end.
  let limit = null;
  let spend = null; // what the session cost at API prices, from Claude Code's own result (`spendReport`)
  // A browser session: why its browser could not be had ("" when it was), told to the app with the end.
  let browserSaid = null;
  let seat = null; // the seat this session took, when its work is a seat
  try {
    const work = await chooseWork(reason);
    if (!work) {
      // The runner looks often: it says there is nothing waiting the first time, not each time until there is work.
      if (!laneIdleSaid) log(`nothing waiting (${reason})`);
      laneIdleSaid = true;
      await presence(true); // it asked and nothing waits: it is up and between sessions, whenever its own beat lands
      return;
    }
    laneIdleSaid = false;
    const started = Date.now();
    const kind = await sessionKind(work, reason);
    const claim = workClaim; // what the app claimed for this session with its answer (`askWork`)
    workClaim = null;
    if (claim) kind.prompt += ` The app has claimed this for you: ${claim.ids.join(", ")}. Your first call for work returns it.`;
    mode = kind.mode;
    sessionId = `${kind.prefix}-${randomBytes(6).toString("hex")}`;
    log(`${kind.mode} run start (${reason}) session ${sessionId}`);
    // Before Claude calls jobs.profile, so the app knows this session is on the open search.
    // Pause everything is this `stop` in the app's reply to a heartbeat (it is the same word the peak-hours blackout
    // uses): no session starts, and the one running is ended (the beat below, which on Windows ends its whole process
    // tree).
    if ((await report(sessionId, "started", claim ? { claim: claim.id } : {})).stop) {
      log(`${kind.mode} run not started: Claude's peak hours are blacked out`);
      return;
    }
    // Each minute: still alive, and stop at once when the app says the peak-hours blackout has begun.
    beat = setInterval(() => void report(sessionId, "alive").then((reply) => {
      if (reply.stop && current && !current.stopping) {
        log(`${kind.mode} run stopped: ${reply.why || "Claude's peak hours are blacked out"}`);
        stopSession(current, kind.mode);
      }
    }), HEARTBEAT_MS);
    if (kind.isSeat) {
      seat = await takeSeat(sessionId, claim);
      if (!seat) {
        log(`${kind.mode} run: no seat is waiting; ending`);
        return;
      }
      kind.seat = seat;
    }
    if (kind.browser) {
      const why = await ensureBrowser();
      browserSaid = why;
      if (why) throw new Error(why);
    }
    // A seat has no MCP door, so no config for it.
    if (!seat) writePrivate(MCP_CONFIG, JSON.stringify({ mcpServers: kind.browser ? browserServers({ sessionId, claim: claim?.id }) : {
      [kind.server]: {
        type: "http", url: APP_MCP,
        headers: {
          Authorization: `Bearer ${agentToken()}`, "X-Session-Id": sessionId,
          ...(claim ? { "X-Claim": claim.id } : {}),
          ...(kind.contextDigest ? { "X-Context": kind.contextDigest } : {}),
          // The door lists this kind's own tools alone (`--allowedTools` limits what may be called, not what is sent: 38
          // definitions, about 9k tokens read on every turn, of which a session uses two to six).
          "X-Tools": kind.doorTools,
          ...(NAMED ? { "X-Runner": `${NAMED}/${RUNNER_START}` } : {}),
        },
      },
    } }));
    // The session's instructions, in a file only this runner can read (`claudeArgs` names it). Whatever an earlier
    // session left is removed first: a file that already exists keeps the mode it was made with.
    dropSystemFile();
    writePrivate(SYSTEM_FILE, seat ? seat.system : kind.system);
    const env = { ...process.env };
    if (CLAUDE_CONFIG_DIR) {
      // A local lane takes nothing of Claude Code's or the API's from the environment it was started in. When the launcher was
      // started from a Claude Code session or the desktop app's terminal, that environment is the host session's (its
      // account, its route, "a person is here", the bare mode ...), none of it this lane's. Git Bash's place stays: Claude
      // Code on Windows looks for it. A runner in the default mode has a clean environment and is left as it is.
      for (const name of Object.keys(env)) {
        if (/^(?:ANTHROPIC_|CLAUDE_|CLAUDECODE$)/i.test(name) && !/^CLAUDE_CODE_GIT_BASH_PATH$/i.test(name)) delete env[name];
      }
    }
    delete env.ANTHROPIC_API_KEY; // subscription only, never the API
    delete env.ANTHROPIC_AUTH_TOKEN; // nor a token of another route; the Claude token is the file's or none
    const token = claudeToken();
    if (token) env.CLAUDE_CODE_OAUTH_TOKEN = token;
    else delete env.CLAUDE_CODE_OAUTH_TOKEN;
    if (CLAUDE_CONFIG_DIR) {
      // A Claude folder of its own, and no CLAUDE.md or memory from anywhere (the default folder's, the working folder's
      // parents').
      mkdirSync(CLAUDE_CONFIG_DIR, { recursive: true });
      env.CLAUDE_CONFIG_DIR = CLAUDE_CONFIG_DIR;
      env.CLAUDE_CODE_DISABLE_CLAUDE_MDS = "1";
      env.CLAUDE_CODE_DISABLE_AUTO_MEMORY = "1";
      // The folder's settings (5-minute cache, the hook that ends a finished session). The hook leaves a note when it ends
      // a session.
      try {
        claudeSettings(CLAUDE_CONFIG_DIR);
      } catch (e) {
        log(`couldn't write the Claude folder's settings: ${e.message}`);
      }
      env.SCOUT_END_MARK = END_MARK;
      rmSync(END_MARK, { force: true });
    }
    // Load the app's tools up front. Claude Code otherwise defers MCP tools behind its ToolSearch tool, which
    // `--tools` leaves out.
    env.ENABLE_TOOL_SEARCH = "false";
    // The most tokens of one tool reply Claude Code lets a session see (its default is 25,000). The app refuses a reply of
    // over 200,000 characters (REPLY_CEILING_CHARS, planengine/budget.py): 50,000 tokens at four characters a token, up
    // to 100,000 at two (dense JSON). So a reply the app sends always arrives whole.
    env.MAX_MCP_OUTPUT_TOKENS = "100000";
    // A seat is one call: a cache would only be written, never read.
    if (seat) env.DISABLE_PROMPT_CACHING = "1";
    const result = await runClaude(kind, env);
    let summary = `exit=${result.code}${result.signal ? ` signal=${result.signal}` : ""}${result.timedOut ? " TIMED OUT" : ""}`;
    let failed = result.timedOut || result.code !== 0;
    let answer = null; // a seat's answer for the app, sent once the session's outcome is known
    try {
      const parsed = JSON.parse(result.out);
      if (parsed.is_error) failed = true;
      spend = spendOf(parsed);
      if (seat) {
        answer = { text: String(parsed.result ?? ""), spend };
        if (failed) answer.error = `Claude ${parsed.is_error ? "reported an error" : "did not finish"}: ${String(parsed.result || result.err || summary).slice(0, 300)}`;
      }
      if (failed) limit = usageLimit([parsed.result, ...(Array.isArray(parsed.errors) ? parsed.errors : []), result.err].filter(Boolean).join("\n"));
      summary += ` turns=${parsed.num_turns} ${parsed.is_error ? "ERROR " : ""}${endReason(result, parsed, limit, failed)} result_len=${String(parsed.result || "").length}`;
    } catch {
      failed = true; // not the JSON a finished session prints
      if (seat) answer = { text: "", spend: null, error: `Claude printed no result (${summary.trim()}): ${String(result.err || result.out || "").slice(0, 300)}` };
      limit = usageLimit([result.out, result.err].filter(Boolean).join("\n"));
      summary += ` ${endReason(result, null, limit, failed)} out_len=${result.out.length} err_len=${result.err.length}`;
      // The runner's own words (Claude not found, a folder it cannot use) are not a session's; they are safe to keep.
      if (result.own) summary += ` err=${JSON.stringify(result.err.slice(0, 300))}`;
    }
    // A session the hook ended once its work was accepted is a finished one, whatever exit it ended with.
    const endedBy = CLAUDE_CONFIG_DIR && existsSync(END_MARK) ? readFileSync(END_MARK, "utf8").trim() : "";
    if (endedBy && !result.timedOut && !limit) {
      failed = false;
      summary += ` ended by the hook after ${endedBy}`;
    }
    rmSync(END_MARK, { force: true });
    if (seat) await answerSeat(sessionId, seat, answer || { text: "", spend: null, error: "Claude gave no result" });
    log(`${kind.mode} run end (${Math.round((Date.now() - started) / 1000)}s) ${summary}`);
    if (limit) {
      log(`${kind.mode} run stopped: Claude's usage limit (${limit.words}); the app is told`);
      log(`${kind.mode} run ended at Claude's usage limit; ${limit.resets_at ? `it lifts at ${limit.resets_at}` : "no time was given for it to lift"}`);
      holdLane(limit);
    }
    if (failed) log(`${kind.mode} run failed; it starts no chain of more sessions`);
    chainOn = !failed;
  } catch (e) {
    log(`${mode} run failed: ${e.message}`);
  } finally {
    clearInterval(beat);
    current = null;
    // The instructions go before the app hears that the session ended.
    dropSystemFile();
    // Always say it ended, however it ended (and, when it did, that it was Claude's usage limit that ended it).
    if (sessionId) await report(sessionId, "ended", { ...limitReport(limit), ...spendReport(spend), ...(browserSaid === null ? {} : { browser: browserSaid }) });
    await finish(chainOn);
  }
}

function validSignature(timestamp, body, given) {
  if (!timestamp || !given) return false;
  const sent = Number(timestamp);
  if (!Number.isFinite(sent) || Math.abs(Date.now() / 1000 - sent) > 300) return false;
  try {
    const expected = createHmac("sha256", secret("wake-secret")).update(`${timestamp}.`).update(body).digest("hex");
    const a = Buffer.from(expected);
    const b = Buffer.from(String(given));
    return a.length === b.length && timingSafeEqual(a, b);
  } catch (e) {
    log(`wake could not be checked: ${e.message}`);
    return false;
  }
}

const server = createServer((req, res) => {
  if (req.method === "GET" && req.url === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: true, running, lane: POOL }));
    return;
  }
  if (req.method !== "POST" || req.url !== "/wake") {
    res.writeHead(404).end();
    return;
  }
  const chunks = [];
  let size = 0;
  let tooBig = false;
  req.on("data", (c) => {
    size += c.length;
    if (size > WAKE_BODY_MAX) tooBig = true;
    else chunks.push(c);
  });
  req.on("end", () => {
    try {
      if (tooBig) {
        log("wake refused: body too large");
        res.writeHead(413).end();
        return;
      }
      const body = Buffer.concat(chunks);
      if (!validSignature(req.headers["x-webhook-timestamp"], body, req.headers["x-webhook-signature-v2"])) {
        log("wake refused: bad or missing signature");
        res.writeHead(401).end();
        return;
      }
      let payload = {};
      try { payload = JSON.parse(body.toString("utf8")); } catch {}
      // The dispatcher's command carries the claim it made for this runner. A runner that cannot start it now says so (409),
      // and the app gives the claim back at once; the one it accepts is started without asking the app for work.
      const claim = payload.mode === "scout" && typeof payload.claim === "string" && typeof payload.kind === "string" ? payload : null;
      if (claim && (running || stopping || Date.now() < holdUntil || commanded)) {
        res.writeHead(409, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ accepted: false, busy: true }));
        return;
      }
      res.writeHead(202, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ accepted: true }));
      // Only an id and a reason are ever in a wake. The mode says whose wake it is: this runner takes "scout" (for all
      // the kinds of session; the reason and the app's word on waiting work pick the session).
      if (payload.mode !== "scout") {
        log(`wake ignored: mode ${payload.mode}`);
        return;
      }
      if (claim) {
        commanded = { claim: claim.claim, kind: claim.kind, model: claim.model, ids: Array.isArray(claim.item?.ids) ? claim.item.ids.map(String) : [] };
        requestRun(`dispatch ${claim.kind} claim ${claim.claim}`);
        return;
      }
      requestRun(`wake ${payload.reason} project ${payload.project_id}`);
    } catch (e) {
      log(`wake failed: ${e.message}`);
      if (!res.headersSent) res.writeHead(500).end();
    }
  });
}).listen({ port: PORT, ...(BIND ? { host: BIND } : {}) }, () => {
  log(`scout (lane ${POOL}) listening on :${PORT}, check-in every ${INTERVAL_MIN} min${BIND ? `, only on ${BIND}` : ""}`);
  // A local runner says once which Claude Code and which sign-in its sessions will use, so its log shows that one is missing.
  if (WINDOWS && !claudeCommand()) log(CLAUDE_NOT_FOUND);
  if (CLAUDE_CONFIG_DIR) {
    try {
      log(claudeToken() ? "sessions sign in with the token file" : "no token file: sessions run on the login in the Claude folder");
    } catch (e) {
      log(`the token file cannot be read: ${e.message}`);
    }
  }
  // The runner states what is behind the connection.
  declare();
  void announceStart();
});
// Nothing to listen on (the port is taken, the address is not this machine's): say so and stop, with no stack trace.
server.on("error", (e) => {
  log(`cannot listen on ${BIND || "any address"}:${PORT}: ${e.message}; not starting`);
  process.exit(1);
});

// The app's own schedule starts a search and wakes the scout; this check-in is the safety net for a wake that was
// missed (jobs.profile says to stop when no search is open). It is also how a runner looks for work when no wake
// announces it.
setInterval(() => requestRun("scheduled check-in"), INTERVAL_MIN * 60_000);
// The runner says it is up between sessions (as the lane's, not the scout's).
setInterval(() => void presence(), PRESENCE_MS);

// A stray rejection is logged, not fatal: the runner outlives one bad call.
process.on("unhandledRejection", (e) => log(`unhandled rejection in lane ${RUNNER}: ${crashWords(e)}`));
// A crash is never lost: one line (time, lane, the error's name and code, never its message),
// then a non-zero exit.
process.on("uncaughtException", (e) => {
  try { log(`uncaught exception in lane ${RUNNER}: ${crashWords(e)}`); } catch {}
  process.exit(70);
});
// Stop the session running and go.
let stopping = false;
function shutdown(why) {
  if (stopping) return;
  stopping = true;
  log(`${why}: stopping`);
  if (current) killChild(current, "SIGTERM");
  server.close();
  setTimeout(() => process.exit(0), 300);
}
// A container's stop sends SIGTERM, which a Node process running as PID 1 ignores unless it handles it. On Windows Ctrl+C is SIGINT
// and closing the console SIGBREAK or SIGHUP; ending the process any other way (Task Manager, `taskkill`) runs nothing, and
// the job object (see the header) ends the session.
for (const signal of ["SIGTERM", "SIGINT", ...(WINDOWS ? ["SIGBREAK", "SIGHUP"] : [])]) {
  process.on(signal, () => shutdown(signal));
}
