// The browser bridge: every browser session's DevTools MCP server attaches here (`--browserUrl`), never to Chrome itself.
// The launcher (lanes-local.mjs) runs it on 127.0.0.1:SCOUT_BROWSER_PORT. It holds one DevTools connection to Chrome and
// gives each session only what it opened:
//
// - One window for all the browser jobs (the owner, 2026-10-16). The bridge opens a first tab of its own, titled "resumebot" (a page the app serves at the address the
//   launcher passes as `appUrl`, `WINDOW_PAGE_PATH`; the data: page `ANCHOR_URL` when there is no address: the owner,
//   2026-11-05), and every page a session opens is a tab opened from it, so it lands in that window: DevTools cannot name
//   the window a new tab goes to (`Target.createTarget` takes no window, and without `newWindow` a tab goes to whichever
//   window was used last, perhaps the owner's), but a tab a page opens stays in that page's window. Nor can it make a tab
//   group (that needs an extension), so the "resumebot" tab is the window's label. A session cannot make a context of its
//   own (that is a window of its own).
// - A session sees only the tabs it opened and the pop-ups they open: no other tab is listed, shown, attached to (an
//   automatic attach is undone at once) or closed, and a command for any other is refused. When the session ends, however
//   it ends, every tab it opened is closed. In the user's everyday Chrome the "resumebot" tab goes with the last session; in
//   the profile's Chrome it stays as long as the bridge holds Chrome (opened as soon as it connects, before anything is
//   closed), because Chrome ends when its last tab closes, and a bridge that closed it by the way ended Chrome and started
//   it again in a loop. It goes, with Chrome, on purpose
//   only when the browser queue is empty (the owner, 2026-11-04; `IDLE_GRACE_MS`).
// - Go (the owner, 2026-10-16) asks for a normal, visible window of its own (`resumebotWindow`), opens the rest of its tabs
//   from its first (`resumebotFrom`), and hands them all to the owner when it is done (`Target.resumebotKeep`).
// - The browser jobs' window stays on screen (the owner, 2026-11-04, which
//   replaces the 2026-10-16 rule that it stays minimized). It is opened in the normal state at `ON_SCREEN` bounds, in the
//   background, and never minimized or moved off screen; the bridge never asks Chrome to raise or focus it. A window the
//   bridge made, or one found minimized or off screen, is put in the normal state at `ON_SCREEN` bounds once (`showOnScreen`);
//   a tab opened in it asks nothing of it. What Chrome and Windows do themselves is not checked live (docs/PRD.md 52.8).
//
// Which Chrome (SCOUT_BROWSER):
// - `profile` (the default, the owner 2026-10-16: the app's own profile, which the owner has signed in to, so no Allow is
//   clicked each time Chrome starts): a Chrome on the install's own profile (SCOUT_BROWSER_PROFILE), described below.
// - `own` (a setting, for an owner who wants their everyday sign-ins to carry over): the user's everyday
//   Chrome, signed in to their accounts. Chrome 136+ refuses `--remote-debugging-port` on the default profile; the
//   supported way in (Chrome 144+, what chrome-devtools-mcp's `--autoConnect` uses: spiked with 1.10.1 on Chrome 155) is
//   remote debugging switched on at chrome://inspect/#remote-debugging, which writes DevToolsActivePort in the profile's
//   folder, and Chrome then asks the owner to allow each new DevTools connection. One connection, held here, asks once
//   each time Chrome starts (a connection per session would ask at every session). The bridge never starts this Chrome.
// - `profile`: the bridge starts it, visible, when a browser job (a session or a code read) first needs it, its DevTools
//   port picked by Chrome on 127.0.0.1 and read from the same file, so no fixed port of Chrome's can clash with the bridge's
//   own; nothing starts it with the bridge. It closes it again when the browser queue is empty (`armIdle`).
//
// The tab register (docs/PRD.md path 52): which tab is whose is kept in the app's database, not here. A session's
// DevTools server connects to `ws://127.0.0.1:<port>/devtools/browser/<session id>`, so the bridge knows whose each tab
// is and tells the app (`register.opened`), and tells it when a tab goes (`register.removed`) or a session's connection
// does (`register.released`). The app's own code (`code.<job>`, `go`) registers its tabs itself. When the bridge
// connects to the app's own Chrome it asks the app what to keep (`register.reconcile`) and closes every other page
// tab (`reconcile`); the app closes a tab by naming it (`POST /tabs/close`) and asks for the same sweep at its own
// start (`POST /tabs/reconcile`). The user's everyday Chrome is never swept. Nothing is closed without the register's
// word, and nothing when the app cannot be reached.
//
// Nothing here signs in, types or stores anything.

import { spawn } from "node:child_process";
import { createHash, timingSafeEqual } from "node:crypto";
import { existsSync, lstatSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { homedir } from "node:os";
import path from "node:path";

// What Wake up and Run say when the browser jobs cannot start, one line each.
export const WHY_CLOSED = "Open Chrome to let the browser jobs run";
export const WHY_SWITCH = "Turn on remote debugging in Chrome at chrome://inspect/#remote-debugging to let the browser jobs run";
export const WHY_CONSENT = "Click Allow in Chrome's remote debugging prompt to let the browser jobs run";
export const WHY_NOT_STARTED = "The browser jobs' Chrome could not be started";

// The first tab of the browser jobs' window: its title names the window. This data: page is the fallback, used when the bridge is
// not told the app's address (`anchorUrl`); with it, the first tab is a page the app itself serves.
export const ANCHOR_URL = "data:text/html,<title>resumebot</title><p style='font:16px sans-serif'>resumebot's browser jobs work in the tabs of this window. Each closes its tabs when it is done.</p>";

// The app's own page for the window's first tab (the owner, 2026-11-05): app/frontend/browser-window.html, built into the page the app serves (app/backend/app/
// web.py) beside the shell, in the app's own look, with no script and no call to the API, so it loads at once, needs no sign-in and
// goes on showing if the app restarts while it is open.
export const WINDOW_PAGE_PATH = "/browser-window.html";

// The bridge serves only resumebot's own clients (docs/PRD.md path 52; the owner, 2026-11-14): a request carries the app's secret key, in this header (the HTTP
// routes) or in the address's `key` query (the WebSocket upgrade, whose clients cannot set a header). The launcher makes the
// key (`secrets/bridge-key`) and gives it to the bridge as `key`; a bridge with none refuses everything.
export const BRIDGE_KEY_HEADER = "x-bridge-key";
export const BRIDGE_KEY_QUERY = "key";

// Whether a request carries `key`, compared in constant time (both sides hashed first, so the lengths never show).
export function carriesKey(req, key) {
  if (!key) return false;
  let given = req.headers?.[BRIDGE_KEY_HEADER];
  if (Array.isArray(given)) given = given[0];
  if (!given) {
    try { given = new URL(req.url || "/", "http://127.0.0.1").searchParams.get(BRIDGE_KEY_QUERY); } catch { given = ""; }
  }
  const digest = (text) => createHash("sha256").update(String(text ?? "")).digest();
  return timingSafeEqual(digest(given || ""), digest(key));
}

// The first tab's address: the app's page at the address the launcher holds for the app (`appUrl`, lanes-local.mjs
// `settings.pageUrl`), its origin alone, or the data: page when there is no address to name (none given, not an address, not http).
export function anchorUrl(appUrl) {
  try {
    const url = new URL(appUrl);
    if (url.protocol === "http:" || url.protocol === "https:") return `${url.origin}${WINDOW_PAGE_PATH}`;
  } catch { /* not an address: the data: page */ }
  return ANCHOR_URL;
}

// The owner's default Chrome profile folder ("User Data"), or SCOUT_CHROME_USER_DATA.
export function chromeUserData(env = process.env, platform = process.platform, home = homedir()) {
  if (env.SCOUT_CHROME_USER_DATA?.trim()) return env.SCOUT_CHROME_USER_DATA.trim();
  if (platform === "win32") return path.win32.join(env.LOCALAPPDATA || path.win32.join(home, "AppData", "Local"), "Google", "Chrome", "User Data");
  if (platform === "darwin") return path.posix.join(home, "Library", "Application Support", "Google", "Chrome");
  return path.posix.join(env.XDG_CONFIG_HOME || path.posix.join(home, ".config"), "google-chrome");
}

function present(file) {
  try { lstatSync(file); return true; } catch { return false; }
}

// Whether Chrome is running on that profile (its lock: `lockfile` on Windows, the `SingletonLock` link elsewhere) and the
// DevTools address it wrote (DevToolsActivePort: the port, then the path), or "" when remote debugging is off.
export function chromeState(userData, { platform = process.platform, exists = present, read = (f) => readFileSync(f, "utf8") } = {}) {
  const join = platform === "win32" ? path.win32.join : path.posix.join;
  const running = exists(join(userData, platform === "win32" ? "lockfile" : "SingletonLock"));
  let endpoint = "";
  try {
    const [port, at] = read(join(userData, "DevToolsActivePort")).split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    if (/^\d+$/.test(port) && Number(port) > 0 && Number(port) < 65536 && at?.startsWith("/")) endpoint = `ws://127.0.0.1:${port}${at}`;
  } catch { /* off */ }
  return { running, endpoint };
}

// A window meant to be seen (the browser jobs' window, Go's) is put here, never left to a saved placement: Windows records a
// minimized window at -32000,-32000, Chrome keeps that in the profile, and a window opened from it can come back off-screen.
export const ON_SCREEN = { left: 80, top: 60, width: 1280, height: 860 };

// A window the owner cannot see: minimized, or parked where Windows puts a minimized one (-32000,-32000). Only that
// sentinel counts as off screen: DevTools says nothing of the screens, and a negative coordinate alone can be the owner's
// monitor to the left of the main one.
const PARKED = -30000;
const hiddenWindow = (b) => b?.windowState === "minimized" || [b?.left, b?.top].some((v) => typeof v === "number" && v <= PARKED);

// Before Chrome starts: a saved window placement with a negative coordinate is set to on-screen values.
export function resetWindowPlacement(profile) {
  const file = path.join(profile, "Default", "Preferences");
  try {
    const prefs = JSON.parse(readFileSync(file, "utf8"));
    const wp = prefs?.browser?.window_placement;
    if (!wp || ![wp.left, wp.top, wp.right, wp.bottom].some((v) => typeof v === "number" && v < 0)) return;
    prefs.browser.window_placement = { ...wp, left: ON_SCREEN.left, top: ON_SCREEN.top, right: ON_SCREEN.left + ON_SCREEN.width,
      bottom: ON_SCREEN.top + ON_SCREEN.height, maximized: false };
    writeFileSync(file, JSON.stringify(prefs));
  } catch { /* no profile yet, or not readable: Chrome makes its own */ }
}

// How the bridge starts the Chrome of the install's own profile (`profile`): visible, its DevTools port picked by Chrome,
// on 127.0.0.1 only. `bin` is Chrome; "" when it was not found.
export function profileChromeArgs(profile) {
  // Not started minimized: the window stays on screen (the owner, 2026-11-04, which replaces the 2026-10-16 rule that it is minimized at
  // launch). `resetWindowPlacement` has already put a saved off-screen placement back.
  return [`--user-data-dir=${profile}`, "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
    "--no-first-run", "--no-default-browser-check",
    // Chrome's standard performance flags, so the window's pages still load and run normally when other windows cover it.
    "--disable-background-timer-throttling", "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows"];
}

// How Chrome is started so that it is nobody's child: on Windows `taskkill /T` (how a stop, a restart or a lane's end ends
// what a process started, stack.py and lanes-local.mjs) ends every process under the one it names, and a Chrome started
// directly by the launcher is under it, with the owner's tabs (the Go window's). Started through `cmd /c start`, Chrome's
// parent is a cmd that is gone at once, so no tree walk reaches it. Returns [command, args, windowsVerbatimArguments].
export function detachedChromeCommand(bin, args, platform = process.platform) {
  if (platform !== "win32") return [bin, args, false];
  const quote = (a) => (/[\s"]/.test(a) ? `"${a.replace(/"/g, '\\"')}"` : a);
  return ["cmd.exe", ["/d", "/s", "/c", `"start "" /b ${[bin, ...args].map(quote).join(" ")}"`], true];
}

// --- WebSocket frames (RFC 6455), the server's side: text, ping, pong and close; a client's frames are masked. ---------

function frame(opcode, payload) {
  const body = Buffer.isBuffer(payload) ? payload : Buffer.from(payload);
  const n = body.length;
  const head = n < 126 ? Buffer.from([0x80 | opcode, n])
    : n < 65536 ? Buffer.from([0x80 | opcode, 126, n >> 8, n & 255])
      : Buffer.concat([Buffer.from([0x80 | opcode, 127]), (() => { const b = Buffer.alloc(8); b.writeBigUInt64BE(BigInt(n)); return b; })()]);
  return Buffer.concat([head, body]);
}

// Reads whole messages off a socket. `onMessage(text)`; `onClose()` once, however the socket ends.
function serveFrames(socket, onMessage, onClose) {
  let buf = Buffer.alloc(0);
  let parts = [];
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    onClose();
  };
  socket.on("data", (chunk) => {
    buf = Buffer.concat([buf, chunk]);
    for (;;) {
      if (buf.length < 2) return;
      const fin = buf[0] & 0x80;
      const opcode = buf[0] & 0x0f;
      const masked = buf[1] & 0x80;
      let n = buf[1] & 0x7f;
      let at = 2;
      if (n === 126) { if (buf.length < 4) return; n = buf.readUInt16BE(2); at = 4; }
      else if (n === 127) { if (buf.length < 10) return; n = Number(buf.readBigUInt64BE(2)); at = 10; }
      const mask = masked ? buf.subarray(at, at + 4) : null;
      if (masked) at += 4;
      if (buf.length < at + n) return;
      const data = Buffer.from(buf.subarray(at, at + n));
      buf = buf.subarray(at + n);
      if (mask) for (let i = 0; i < data.length; i++) data[i] ^= mask[i & 3];
      if (opcode === 0x8) { try { socket.end(frame(0x8, Buffer.alloc(0))); } catch {} close(); return; }
      if (opcode === 0x9) { socket.write(frame(0xa, data)); continue; }
      if (opcode === 0xa) continue;
      parts.push(data);
      if (fin) {
        const text = Buffer.concat(parts).toString("utf8");
        parts = [];
        onMessage(text);
      }
    }
  });
  // The owner, 2026-11-14: the app's own client hangs up with no close frame, and an upgraded socket allows half-open
  // ends, so it gets `end` and never `close`: the session stayed "connected" and Chrome stayed open all day. A peer that
  // ends its side is gone, however it said so.
  socket.on("end", () => { try { socket.end(); } catch {} close(); });
  socket.on("close", close);
  socket.on("error", close);
}

// The commands no session sends, whatever it opened: they reach past its own tabs (the whole browser, every cookie), or
// make a window of its own (a context).
const REFUSED = new Set([
  "Browser.close", "Browser.crash", "Browser.crashGpuProcess", "Browser.resetPermissions", "Browser.grantPermissions",
  "Browser.setPermission", "Storage.getCookies", "Storage.setCookies", "Storage.clearCookies", "Storage.clearDataForOrigin",
  "Target.exposeDevToolsProtocol", "Target.setRemoteLocations", "Target.autoAttachRelated", "Target.createBrowserContext",
  "Target.disposeBrowserContext",
  // Never take the owner's focus: no session raises a tab or the window.
  "Target.activateTarget",
]);

// The bridge's own tab stands for it in the maps below.
const BRIDGE = { bridge: true, closed: true };
// A tab a session handed to the owner (`Target.resumebotKeep`: Go's window): no session drives, closes or sweeps it again.
const KEPT = { kept: true, closed: true };
// What an empty tab shows: a tab opened and never navigated, a failed navigation's, or Chrome's own new tab page.
export const BLANK_URLS = new Set(["about:blank", "chrome://newtab/", "chrome://new-tab-page/"]);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The register when there is none (the tests that do not look at it). `busy` says what the app's browser queue holds
// ({ waiting, leased }); null, as here, is "the app does not say", and then the bridge closes no Chrome.
export const NO_REGISTER = { opened: async () => {}, removed: async () => {}, released: async () => {}, reconcile: async () => null,
  busy: async () => null };
// How long the app's Chrome (SCOUT_BROWSER=profile) must have no work before the bridge closes it (the owner, 2026-11-04): no session connected, no browser item waiting or leased, no Go window or kept tab open. About two
// minutes, so a queue that refills at once does not start Chrome again.
export const IDLE_GRACE_MS = 120_000;
// How long a session that asks for Chrome while it is ending waits for it to have gone, when it does not (a tab opened meanwhile).
export const END_WAIT_MS = 5_000;
// How often a Chrome that has closed its DevTools connection is looked at for its profile's lock to be gone.
const END_POLL_MS = 50;
// A DevTools address that names the app's own code, which registers its own tabs: `code.<job>` or `go`.
export const ownsItsTabs = (label) => label === "go" || label.startsWith("code.");
// Tabs opened one at a time from the first tab; one that gives none is tried again this many times on the same tab
// before the first tab is replaced (docs/PRD.md 52.5).
const OPEN_TRIES = 3;
// The most held tabs the "a tab did not open" line names (the oldest); the rest are counted.
const HELD_NAMED = 5;
const MAX_BODY = 1_000_000;

/**
 * The bridge. `start()` listens on 127.0.0.1:`port`; `stop()` closes it and every tab the sessions opened.
 * `mode` is `own` (the owner's Chrome, never started here) or `profile` (`profile` is its folder, `chromeBin` the Chrome
 * it is started with). `consentMs`: how long a connection waits for the owner's Allow. `appUrl`: where the app is, for the page its
 * first tab shows (`anchorUrl`); none, and that tab is the data: page. `key`: the secret every request must carry
 * (`carriesKey`); none, and the bridge refuses every request. `WebSocketImpl`, `state` and `launch` are for the tests.
 */
export function ownChromeBridge({ port, mode = "profile", userData = chromeUserData(), profile = "", chromeBin = "",
  consentMs = 60_000, openMs = 15_000, startMs = 30_000, idleCloseMs = IDLE_GRACE_MS, endWaitMs = END_WAIT_MS, openRetryMs = 300,
  internalMs = 10_000, log = () => {}, appUrl = "", key = "",
  register = NO_REGISTER, WebSocketImpl = globalThis.WebSocket,
  state = () => chromeState(mode === "profile" ? profile : userData), launch = null } = {}) {
  const firstTabUrl = anchorUrl(appUrl); // the window's first tab: the app's page, or the data: page
  let upstream = null; // the one connection to Chrome, open
  let connecting = null; // Promise<string> while it is being made
  let failed = null; // { why, endpoint }: the last try that did not connect, for the passive status
  let product = "Chrome";
  let weStarted = false; // the bridge started this Chrome (its window is the app's own), or it was already open
  let seq = 0;
  const pending = new Map(); // upstream id -> { client, id, then } | { internal, resolve }
  const clients = new Set();
  // Tabs Chrome made paused for the debugger (`waitingForDebugger` on their attach) and that nobody has resumed: sessionId ->
  // { targetId, who, at }. Only for the log (`pausedNow`), when a window.open gives no tab.
  const paused = new Map();
  const resumedLogged = new Set(); // targets whose resume by the bridge is already in the log: once each
  const targets = new Map(); // targetId -> client (or BRIDGE), or null for one that is not a session's
  const sessions = new Map(); // sessionId -> client (or BRIDGE)
  let anchor = null; // { targetId, sessionId } of the bridge's own tab, the window's first
  let anchoring = null; // Promise<anchor | null> while it is being opened
  const opens = []; // tabs asked for and not seen yet: { client, id, timer }
  let openChain = Promise.resolve(); // the opens go one at a time (docs/PRD.md 52.5)
  let conns = 0; // connections made, for a name for one that did not give its own
  let generation = 0; // counts the Chromes lost: a step begun on an earlier one ends when it sees the count has moved

  // The profile's Chrome is started when a browser job first asks for it, kept open through the whole queue (the owner,
  // 2026-10-16: Chrome opening and closing steals focus) and closed when the queue is empty (the owner, 2026-11-04, after a "resumebot" tab had stayed open for as long as the app ran). The queue is empty when
  // no session is connected, the app says no browser item is waiting or leased (`register.busy`), and no tab is open that
  // the owner should see (Go's window, kept tabs, any page that is not blank); that must hold for `idleCloseMs` (the grace:
  // a queue that refills at once does not start Chrome again), and is looked at again each time it is not. Chrome ends the
  // way it always has for the bridge, when its last tab closes (`endChrome`), or by `Browser.close` when the bridge's first tab is
  // gone and only blank pages are left (the owner, 2026-11-12,
  // after a Chrome stayed open all day: the app's own Chrome closes when its work is done and nothing else is open in it). The
  // user's everyday Chrome is never closed (the owner, 2026-10-22; unchanged).
  // Armed when the last session goes, when the bridge connects with none, and when the bridge starts and finds the app's Chrome
  // already running (`adoptRunning`).
  let idleTimer = null;
  const disarmIdle = () => { clearTimeout(idleTimer); idleTimer = null; };
  // Why the last idle check kept Chrome open, logged once and again only when the reason changes (the owner, 2026-11-12: every
  // keep-open path was silent, so a Chrome that stayed all day could not be explained). Cleared when nothing is kept, when
  // Chrome ends and when it is lost. Never a tab's address or title; a session's label is the app's own name for it.
  let lastKeep = null;
  const keepOpen = (why) => {
    if (why !== lastKeep) log(`the browser jobs are done but Chrome stays open: ${why}`);
    lastKeep = why;
    armIdle();
  };
  const armIdle = () => {
    disarmIdle();
    if (mode !== "profile" || !idleCloseMs || !upstream) return;
    idleTimer = setTimeout(async () => {
      idleTimer = null;
      if (!upstream) return;
      if (clients.size) { keepOpen(`a session is connected (${[...clients].map((c) => c.label).join(", ")})`); return; }
      const born = generation;
      // Something moved while this waited on Chrome or the app: a session came, Chrome went, or the grace began again.
      const moved = () => clients.size > 0 || !upstream || idleTimer !== null || born !== generation;
      const found = await internal("Target.getTargets", {});
      if (moved()) return;
      if (!found.result) { keepOpen("Chrome did not say what is open"); return; } // nothing is closed, and it is looked at again
      const pages = (found.result.targetInfos || []).filter((t) => t.type === "page");
      const others = pages.filter((t) => t.targetId !== anchor?.targetId && !BLANK_URLS.has(t.url));
      if (others.length || hasKept()) {
        // Tabs the owner should see (Go's window among them; a bridge restarted since has forgotten which are whose) keep
        // Chrome open: only the app's own window goes, as before. Looked at again after the grace (docs/PRD.md 52.9), so
        // a tab that closes later, or a kept tab that is gone, still lets Chrome close.
        if (others.length && anchor) {
          log("the browser queue is done: closing the browser jobs' window (the other tabs stay)");
          const gone = anchor.targetId;
          anchor = null;
          targets.delete(gone);
          await internal("Target.closeTarget", { targetId: gone });
        }
        if (!moved()) keepOpen(others.length ? `${others.length} page${others.length === 1 ? "" : "s"} the owner should see ${others.length === 1 ? "is" : "are"} open` : "a kept tab is still open");
        return;
      }
      const work = await queueWork();
      if (moved()) return;
      if (!work) { keepOpen("the app does not say whether the queue holds work"); return; } // look again after the grace
      if (work.waiting !== 0 || work.leased !== 0) { keepOpen(`the app's queue holds work (${work.waiting} waiting, ${work.leased} leased)`); return; }
      lastKeep = null;
      await endChrome(moved);
    }, idleCloseMs);
    idleTimer.unref?.();
  };

  // What the app says the browser queue holds (`register.busy`: { waiting, leased }), or null when it does not say, is not
  // there or says something else: then nothing is closed.
  const queueWork = async () => {
    try {
      const work = await register.busy?.();
      return Number.isInteger(work?.waiting) && Number.isInteger(work?.leased) ? work : null;
    } catch {
      return null;
    }
  };

  // The profile's Chrome ended by closing its last tab, the bridge's own: the blank tabs of the app's window first (the
  // sweep after a session), then the first tab, the one tab Chrome has not been left without until now. The app's register is
  // told it is gone. A session that asks while Chrome ends waits for it (`connect`) and finds a fresh one, never a dying one;
  // a Chrome that stays (a tab opened meanwhile) is waited for no longer than `endWaitMs`. `moved()`: a session came after all.
  // Chrome has ended when its profile's lock is gone, not when it closes its DevTools connection (that comes first, and a
  // request in between found the lock held, started nothing and failed): `state().running`, looked at every `END_POLL_MS`
  // once the connection has closed (`lost`), within the same `endWaitMs`.
  let ending = null; // Promise while Chrome is ending
  let endedChrome = null; // called when Chrome has closed its DevTools connection (`lost`): ends that wait once the lock is gone
  const endChrome = async (moved) => {
    if (mode !== "profile") return;
    if (anchor) await closeBlanks();
    if (moved()) return;
    // The bridge's first tab is gone (its window was closed, or the bridge restarted since) and nothing but blank pages is
    // left: Chrome is ended whole, the one place the bridge asks for it (the owner, 2026-11-12).
    const gone = anchor?.targetId;
    if (gone) {
      log("the browser queue is empty: closing the app's Chrome (its last tab)");
      anchor = null;
      targets.delete(gone);
      void register.removed(gone);
    } else {
      log("the browser queue is empty: closing the app's Chrome (no first tab of the bridge's is left)");
    }
    let done = () => {};
    ending = new Promise((resolve) => {
      let look = null;
      let watching = false;
      const timer = setTimeout(() => done(), endWaitMs);
      timer.unref?.();
      done = () => { clearTimeout(timer); clearTimeout(look); ending = null; endedChrome = null; resolve(); };
      endedChrome = () => {
        if (watching) return;
        watching = true;
        const check = () => {
          if (!state().running) return done();
          look = setTimeout(check, END_POLL_MS);
          look.unref?.();
        };
        check();
      };
    });
    await (gone ? internal("Target.closeTarget", { targetId: gone }) : internal("Browser.close", {}));
  };

  const hasKept = () => [...targets.values()].includes(KEPT);
  const send = (msg) => upstream?.send(JSON.stringify(msg));
  // A command of the bridge's own; resolves with Chrome's answer ({ error } when there is no Chrome, or when Chrome does not
  // answer within `internalMs`). Every wait on Chrome ends: the opens go one at a time, so one that waited for ever on an
  // answer that never came (Chrome went, or a page that does not run) held every open after it, for as long as the bridge
  // ran.
  const internal = (method, params, sessionId) => new Promise((resolve) => {
    if (!upstream) return resolve({ error: { message: "no Chrome" } });
    const id = ++seq;
    const timer = setTimeout(() => {
      if (!pending.delete(id)) return;
      log(`Chrome did not answer ${method} in ${Math.round(internalMs / 1000)} s`);
      resolve({ error: { message: "no answer" } });
    }, internalMs);
    timer.unref?.();
    pending.set(id, { internal: true, resolve: (msg) => { clearTimeout(timer); resolve(msg); } });
    send({ id, method, params, ...(sessionId ? { sessionId } : {}) });
  });
  const deliver = (client, msg) => {
    if (!client.closed) client.socket.write(frame(0x1, JSON.stringify(msg)));
  };
  const reply = (client, id, body) => deliver(client, { id, ...body });

  // An automatic attach that is not the session's (the owner's tabs, Chrome's own pages, a second attach to the bridge's
  // tab) is undone at once, resumed first if Chrome paused it for the debugger, so a tab loads as it would.
  const letGo = (event) => {
    const sid = event.params.sessionId;
    if (event.params.waitingForDebugger) resume(sid);
    void internal("Target.detachFromTarget", { sessionId: sid });
  };
  // A tab Chrome paused for the debugger is let run, on the session Chrome attached it with: fire and forget, an error ignored.
  const resume = (sessionId) => {
    paused.delete(sessionId);
    void internal("Runtime.runIfWaitingForDebugger", {}, sessionId);
  };
  // What Chrome holds paused for the debugger now, for the log: "none", or each tab with whose it is and for how long. The
  // oldest `HELD_NAMED` are named and the rest counted ("and 2 more"), so a pile of them does not make the line a page long.
  // The map is in the order the tabs were paused (a session id is never paused twice), so the first are the oldest.
  const who = (c) => c === BRIDGE ? "the bridge" : c === KEPT ? "the owner" : c?.label ?? "nobody";
  const pausedNow = () => {
    const now = Date.now();
    const held = [...paused.values()].map((p) => `${p.targetId} of ${p.who} for ${Math.round((now - p.at) / 1000)} s`);
    if (!held.length) return "none";
    const named = held.slice(0, HELD_NAMED).join(", ");
    return held.length > HELD_NAMED ? `${named}, and ${held.length - HELD_NAMED} more` : named;
  };

  // A session's tab is put on the app's register as it is seen (the app's own code registers its own tabs).
  const tellRegister = (client, targetId, info) => {
    if (client.own || client === BRIDGE || client.closed || (info?.type && info.type !== "page")) return;
    void register.opened(targetId, client.label, "session");
  };
  const targetOf = (event) => event.params?.targetInfo?.targetId ?? event.params?.targetId;
  // Whose a target is: known, or a tab the bridge's tab opened for the first session waiting for one, or a pop-up of a
  // session's tab. undefined: no session's.
  const ownerOf = (targetId, info) => {
    if (targets.has(targetId)) return targets.get(targetId);
    let client;
    const at = info?.openerId ? opens.findIndex((o) => o.opener === info.openerId) : -1;
    if (at >= 0) {
      const [asked] = opens.splice(at, 1);
      clearTimeout(asked.timer);
      asked.show?.();
      client = asked.client;
      if (client.closed) { void internal("Target.closeTarget", { targetId }); client = null; asked.finish?.(); }
      else setImmediate(() => { reply(client, asked.id, { result: { targetId } }); asked.finish?.(); }); // nothing is asked of the window
    } else if (info?.openerId) {
      const opener = targets.get(info.openerId);
      if (opener && opener !== BRIDGE) client = opener;
    }
    if (client === undefined) return undefined;
    if (client !== null) tellRegister(client, targetId, info);
    targets.set(targetId, client);
    return client;
  };

  const route = (event) => {
    if (event.sessionId) { // inside a tab: its session's, and what auto-attaches below it is that session's too
      const client = sessions.get(event.sessionId);
      if (!client || client === BRIDGE) return;
      if (event.method === "Target.attachedToTarget") {
        sessions.set(event.params.sessionId, client);
        targets.set(event.params.targetInfo.targetId, client);
      }
      if (event.method === "Target.detachedFromTarget") sessions.delete(event.params.sessionId);
      deliver(client, event);
      return;
    }
    if (!event.method?.startsWith("Target.")) return; // nothing browser-wide reaches a session
    const targetId = targetOf(event);
    if (event.method === "Target.detachedFromTarget") {
      const client = sessions.get(event.params.sessionId);
      sessions.delete(event.params.sessionId);
      paused.delete(event.params.sessionId);
      if (client && client !== BRIDGE) deliver(client, event);
      if (anchor && event.params.sessionId === anchor.sessionId) anchor.sessionId = null;
      return;
    }
    if (!targetId) return;
    if (event.method === "Target.targetDestroyed" && anchor?.targetId === targetId) anchor = null;
    if (event.method === "Target.targetDestroyed" && targets.get(targetId)) void register.removed(targetId);
    if (event.method === "Target.targetDestroyed") {
      for (const [sid, p] of paused) if (p.targetId === targetId) paused.delete(sid);
      resumedLogged.delete(targetId);
    }
    const client = ownerOf(targetId, event.params.targetInfo);
    if (event.method === "Target.attachedToTarget" && event.params.waitingForDebugger) paused.set(event.params.sessionId, { targetId, who: who(client), at: Date.now() });
    if (client === BRIDGE) {
      if (event.method === "Target.attachedToTarget") {
        // The bridge's tab keeps one session (the first attach, automatic or its own); any other is let go.
        if (anchor && !anchor.sessionId) {
          anchor.sessionId = event.params.sessionId;
          sessions.set(event.params.sessionId, BRIDGE);
          if (event.params.waitingForDebugger) resume(event.params.sessionId);
        } else if (!anchor || event.params.sessionId !== anchor.sessionId) letGo(event);
      }
      return;
    }
    if (!client || client === KEPT) { // nobody's, or the owner's now: never attached to, never shown
      if (!client) targets.set(targetId, null);
      if (event.method === "Target.attachedToTarget") letGo(event);
      if (event.method === "Target.targetDestroyed") targets.delete(targetId);
      return;
    }
    if (event.method === "Target.attachedToTarget") sessions.set(event.params.sessionId, client);
    deliver(client, event);
    // A session resumes its own paused tabs (its DevTools server does); the app's own code ignores events and never would, and a
    // tab left paused wedges the tab that opened it. So the bridge does it for
    // the app's own code's tabs, and for no session's.
    if (event.method === "Target.attachedToTarget" && event.params.waitingForDebugger && client.own) {
      resume(event.params.sessionId);
      if (!resumedLogged.has(targetId)) {
        resumedLogged.add(targetId);
        log(`resumed a tab of ${client.label} that Chrome had paused for the debugger (${targetId}); the app's own code does not resume its own`);
      }
    }
    if (event.method === "Target.targetDestroyed") targets.delete(targetId);
  };

  const onUpstream = (text) => {
    let msg;
    try { msg = JSON.parse(text); } catch { return; }
    if (msg.id !== undefined) {
      const p = pending.get(msg.id);
      pending.delete(msg.id);
      if (!p) return;
      if (p.internal) return p.resolve?.(msg);
      const out = p.then ? p.then(msg) : msg;
      deliver(p.client, { ...out, id: p.id });
      return;
    }
    route(msg);
  };

  // The bridge's own tab, opened when a session first asks for a tab: in a window of its own in the owner's Chrome, as a
  // tab of the window that is there in the profile's Chrome. Resolves with it, or null when Chrome would not open it.
  // `fresh`: opened again, in a window of its own (`renewAnchor`).
  const ensureAnchor = (fresh = false) => {
    if (anchor?.sessionId) return Promise.resolve(anchor);
    if (anchoring) return anchoring;
    const making = anchoring = (async () => {
      if (!anchor) {
        // In Chrome the bridge started, its first window is the app's own; in Chrome already open (or with the owner's
        // Go window in it), a window of its own. Either way it stays on screen, in the normal state, and is opened in the
        // background so the owner's work keeps the focus (the owner, 2026-11-04): `background` makes Chrome show a window it
        // makes with `ShowInactive()` (Chromium 138 to main, read, not run). A window the bridge made is put at `ON_SCREEN`
        // once; Chrome's own first window only when it is found minimized or off screen.
        const newWindow = fresh || !weStarted || hasKept();
        const made = await internal("Target.createTarget", { url: firstTabUrl, newWindow, background: true });
        if (!made.result?.targetId) return null;
        anchor = { targetId: made.result.targetId, sessionId: null };
        targets.set(anchor.targetId, BRIDGE);
        // Which page it is, so a wiring slip back to the fallback shows in lanes.log (the data: URL is long, so it is named).
        log(`the browser jobs' first tab opened at ${firstTabUrl === ANCHOR_URL ? "the built-in page (no app address)" : firstTabUrl}`);
        if (await showOnScreen(anchor.targetId, !newWindow)) log("the browser jobs' window is on screen, in the normal state (opened in the background)");
      }
      if (!anchor.sessionId) {
        const attached = await internal("Target.attachToTarget", { targetId: anchor.targetId, flatten: true });
        if (anchor && !anchor.sessionId && attached.result?.sessionId) {
          anchor.sessionId = attached.result.sessionId;
          sessions.set(anchor.sessionId, BRIDGE);
        }
      }
      return anchor?.sessionId ? anchor : null;
    })().finally(() => { if (anchoring === making) anchoring = null; });
    return making;
  };

  // A session's new tab: opened from the bridge's tab, so it is a tab of that window. Answered when Chrome says the tab
  // exists (`ownerOf`).
  // `from` ({ targetId, sessionId } of a tab the session owns, `resumebotFrom`): the tab is opened from that tab instead,
  // so it lands in that tab's window (Go's window).
  //
  // The window's first tab can stop opening tabs while Chrome keeps running. A tab that does not open from it is asked for once more, from a first tab opened afresh in
  // a window of its own, on screen (`renewAnchor`), and why it failed is logged. One that never appears marks the first
  // tab stale, so the next ask renews it first.
  // The opens go one at a time (`openChain`): a burst of sessions asking at once is what `window.open` gave no tab to
  // (docs/PRD.md 52.5). One that gives none is tried again on the same tab, with a pause that grows, `OPEN_TRIES` times
  // before the first tab is replaced, and what the opener looked like is logged each time.
  const openTab = (client, msg, from = null) => new Promise((resolve) => {
    openChain = openChain.then(() => openOne(client, msg, from)).then(resolve, resolve);
  });

  const openerState = async (at) => {
    const d = await internal("Runtime.evaluate", {
      expression: "JSON.stringify({visible: document.visibilityState, focus: document.hasFocus(), activation: navigator.userActivation?.isActive, opener: !!window.opener})",
      returnByValue: true,
    }, at.sessionId);
    return d.result?.result?.value ?? "unreadable";
  };

  const openOne = async (client, msg, from) => {
    const born = generation;
    if (client.closed) return;
    if (!from && anchorStale) { anchorStale = false; await renewAnchor("a tab asked of it never appeared"); }
    let at = from || await ensureAnchor();
    if (!at) return reply(client, msg.id, { error: { code: -32000, message: "the browser jobs' window could not be opened" } });
    const url = typeof msg.params?.url === "string" && msg.params.url ? msg.params.url : "about:blank";
    let renewed = false;
    for (let attempt = 1; ; attempt++) {
      const asked = { client, id: msg.id, opener: at.targetId };
      const settled = new Promise((r) => { asked.finish = r; });
      const shown = new Promise((r) => { asked.show = r; }); // Chrome showed the tab (`ownerOf`)
      asked.timer = setTimeout(() => {
        const i = opens.indexOf(asked);
        if (i >= 0) opens.splice(i, 1);
        if (!from) anchorStale = true;
        log("a tab did not open: Chrome never showed it");
        reply(client, msg.id, { error: { code: -32000, message: "the tab did not open" } });
        asked.finish();
      }, openMs);
      opens.push(asked);
      // Chrome opens this as a foreground tab and asks to show its window (`kShowWindow`), which for a window already visible
      // activates it: on Windows that can raise the window or only flash its taskbar button. The bridge cannot change it
      // (`Target.createTarget` names no window), and nothing here asks for a raise or a focus (docs/PRD.md 52.8 (b), not
      // checked live).
      const evaluating = internal("Runtime.evaluate", { expression: `window.open(${JSON.stringify(url)}, "_blank") !== null`, userGesture: true, returnByValue: true }, at.sessionId);
      // Chrome can show the tab before the page answers the evaluate that asked for it.
      // The tab being shown is enough: the next open does not wait for the answer. One never shown still waits for it, as the
      // retries and the timeout below depend on what it says.
      const done = await Promise.race([evaluating, shown.then(() => null)]);
      if (!done || done.result?.result?.value === true || !opens.includes(asked)) { await settled; return; }
      opens.splice(opens.indexOf(asked), 1);
      clearTimeout(asked.timer);
      const why = done.error?.message || done.result?.exceptionDetails?.text || (done.result ? "window.open gave no tab" : "no answer");
      log(`a tab did not open (${why}; try ${attempt} of ${OPEN_TRIES}; the opener: ${await openerState(at)}; waiting for the debugger: ${pausedNow()})`);
      if (!upstream || client.closed || born !== generation) return;
      if (attempt < OPEN_TRIES) { await sleep(openRetryMs * attempt); continue; }
      if (from || renewed) {
        return reply(client, msg.id, { error: { code: -32000, message: "the tab did not open" } });
      }
      renewed = true;
      attempt = 0;
      await renewAnchor(why, at.targetId);
      at = await ensureAnchor();
      if (!at) return reply(client, msg.id, { error: { code: -32000, message: "the browser jobs' window could not be opened" } });
    }
  };

  // The window's first tab opened afresh: the new one first (in the profile's Chrome the old one may be its last tab, and
  // Chrome ends when its last tab closes), then the old one closed.
  let anchorStale = false;
  const renewAnchor = async (why, stale = anchor?.targetId) => {
    if (anchoring) await anchoring;
    const old = anchor;
    if (old && old.targetId !== stale) return; // renewed meanwhile, for another session
    log(`the browser jobs' window stopped opening tabs (${why}): opening its first tab again`);
    anchor = null;
    if (old?.sessionId) sessions.delete(old.sessionId);
    await ensureAnchor(true);
    if (old && old.targetId !== anchor?.targetId) {
      targets.delete(old.targetId);
      void internal("Target.closeTarget", { targetId: old.targetId });
    }
  };

  const refuse = (client, msg, why) => deliver(client, { id: msg.id, ...(msg.sessionId ? { sessionId: msg.sessionId } : {}), error: { code: -32000, message: why } });

  const onClient = (client, text) => {
    let msg;
    try { msg = JSON.parse(text); } catch { return; }
    if (typeof msg.id !== "number" || typeof msg.method !== "string") return;
    if (!upstream) return refuse(client, msg, "the browser is not connected");
    const params = msg.params || {};
    const mine = (owner) => owner === client;
    // Whose a refused tab is, said in the log: no one's (never seen, or seen with no
    // opener), the owner's (kept), the bridge's, another session's, or gone.
    const whose = (owner) => owner === undefined ? "unknown" : owner === null ? "nobody's" : owner === KEPT ? "kept" : owner === BRIDGE ? "the bridge's" : owner.closed ? "a session's that has gone" : "another session's";
    const notMine = (kind, id, owner) => {
      log(`refused ${msg.method}: ${kind} ${id} is ${whose(owner)}, not this session's`);
      return refuse(client, msg, "not a tab this session opened");
    };
    if (msg.sessionId && !mine(sessions.get(msg.sessionId))) return notMine("session", msg.sessionId, sessions.get(msg.sessionId));
    if (params.targetId && !mine(targets.get(params.targetId))) return notMine("target", params.targetId, targets.get(params.targetId));
    if (params.sessionId && !mine(sessions.get(params.sessionId))) return notMine("session", params.sessionId, sessions.get(params.sessionId));
    if (params.browserContextId) return refuse(client, msg, "the browser jobs work in the one window");
    if (!msg.sessionId && REFUSED.has(msg.method)) return refuse(client, msg, `${msg.method} reaches past this session's tabs`);
    if (msg.method === "Page.bringToFront") return reply(client, msg.id, { result: {} }); // answered, never sent: no focus is taken
    if (msg.method === "Runtime.runIfWaitingForDebugger" && msg.sessionId) paused.delete(msg.sessionId);
    let then = null;
    let out = msg;
    if (!msg.sessionId) {
      switch (msg.method) {
        case "Target.getBrowserContexts":
          return reply(client, msg.id, { result: { browserContextIds: [] } });
        case "Target.createTarget": {
          if (params.resumebotWindow === true) { // Go: a normal, visible window of its own, never minimized or raised
            then = (r) => {
              if (r.result?.targetId) {
                targets.set(r.result.targetId, client);
                void showOnScreen(r.result.targetId);
              }
              return r;
            };
            out = { ...msg, params: { url: typeof params.url === "string" && params.url ? params.url : "about:blank", newWindow: true } };
            break;
          }
          const from = params.resumebotFrom;
          if (from) {
            if (!mine(targets.get(from.targetId)) || !mine(sessions.get(from.sessionId))) return refuse(client, msg, "not a tab this session opened");
            return void openTab(client, msg, { targetId: from.targetId, sessionId: from.sessionId });
          }
          return void openTab(client, msg);
        }
        case "Target.setDiscoverTargets":
          // The bridge keeps discovery on for itself (it is how it knows a tab it opened exists); a session turning it
          // off would blind it, so that is answered here.
          if (params.discover === false) return reply(client, msg.id, { result: {} });
          break;
        case "Target.resumebotKeep": {
          // Go: hand every tab this session opened to the owner. They stay open when it goes, and no session drives,
          // closes or sweeps them again; the bridge lets go of them in Chrome.
          let kept = 0;
          for (const [targetId, c] of targets) if (c === client) { targets.set(targetId, KEPT); kept++; }
          for (const [sid, c] of sessions) {
            if (c === client) { sessions.delete(sid); void internal("Target.detachFromTarget", { sessionId: sid }); }
          }
          return reply(client, msg.id, { result: { kept } });
        }
        case "Target.getTargets":
          then = (r) => r.result ? { ...r, result: { ...r.result, targetInfos: (r.result.targetInfos || []).filter((t) => mine(targets.get(t.targetId))) } } : r;
          break;
        case "Target.setAutoAttach":
          // Pages attach at the top, never through Chrome's tab targets: a tab target cannot be told apart from the owner's
          // by anything it carries, its page can (opened from the bridge's tab, or by a session's own tab).
          out = { ...msg, params: { ...params, filter: [{ type: "page" }] } };
          break;
        case "Target.attachToTarget":
          then = (r) => { if (r.result?.sessionId) sessions.set(r.result.sessionId, client); return r; };
          break;
        default:
      }
    }
    const id = ++seq;
    pending.set(id, { client, id: msg.id, then });
    send({ ...out, id });
  };

  // What a session opened is closed when it goes, however it goes: every tab it opened (pop-ups too), and the bridge's
  // own tab when no session is left.
  const dropClient = (client) => {
    if (client.closed) return;
    client.closed = true;
    clients.delete(client);
    for (const [targetId, c] of targets) {
      if (c !== client) continue;
      void internal("Target.closeTarget", { targetId });
      targets.delete(targetId);
      if (client.own) void register.removed(targetId);
    }
    if (!client.own) void register.released(client.label);
    for (const [sid, c] of sessions) if (c === client) sessions.delete(sid);
    for (const [id, p] of pending) if (p.client === client) pending.set(id, { internal: true });
    try { client.socket.end(frame(0x8, Buffer.alloc(0))); } catch {}
    // The profile's Chrome keeps the bridge's tab while the bridge holds it (its last tab closing would end Chrome); the
    // user's everyday Chrome is given back its window.
    if (!clients.size && anchor && mode !== "profile") {
      void internal("Target.closeTarget", { targetId: anchor.targetId });
      targets.delete(anchor.targetId);
      anchor = null;
    }
    if (!clients.size) {
      armIdle();
      const sweep = setTimeout(() => { if (!clients.size && upstream) void closeBlanks(); }, 2000); // the tabs just closed are gone by then
      sweep.unref?.();
    }
  };

  const lost = () => {
    upstream = null;
    weStarted = false;
    generation++;
    lastKeep = null;
    disarmIdle();
    endedChrome?.(); // a session waiting for Chrome to end finds none, and starts it afresh
    for (const client of [...clients]) dropClient(client);
    targets.clear(); sessions.clear();
    paused.clear(); resumedLogged.clear();
    // What the bridge was waiting on Chrome for is answered ("no Chrome"), so no step stays waiting on a Chrome that is gone,
    // and the opens begun on it end, whatever comes after.
    const waiting = [...pending.values()];
    pending.clear();
    for (const p of waiting) p.resolve?.({ error: { message: "no Chrome" } });
    anchor = null;
    anchoring = null;
    anchorStale = false;
    openChain = Promise.resolve();
    for (const asked of opens.splice(0)) { clearTimeout(asked.timer); asked.finish?.(); }
  };

  // Why the browser jobs cannot run now, without trying to connect (the runners' idle word): "" when they can, or may.
  const status = () => {
    if (upstream || mode === "profile") return mode === "profile" && failed ? failed.why : "";
    const s = state();
    if (!s.running) return WHY_CLOSED;
    if (!s.endpoint) return WHY_SWITCH;
    return failed && failed.endpoint === s.endpoint ? failed.why : "";
  };

  // The profile's Chrome, started when it is not running (the lock is the word: a Chrome that closed its DevTools socket and has
  // not gone yet still holds it). Resolves with its state once it has written its port, or with why it could not be started.
  // Started once, whenever the lock is found gone, also while waiting: a request that came while a Chrome that was ending still
  // held its lock starts the next Chrome as soon as the lock is released, not never.
  // `adopt`: only a Chrome that is running is wanted; none is started (`adoptRunning`).
  const startProfileChrome = async (adopt = false) => {
    let launched = false;
    const until = Date.now() + startMs;
    let s;
    for (;;) {
      s = state();
      if (!s.running && !launched) {
        if (adopt) return { why: WHY_NOT_STARTED };
        launched = true;
        const join = process.platform === "win32" ? path.win32.join : path.posix.join;
        try { unlinkSync(join(profile, "DevToolsActivePort")); } catch { /* none left by a Chrome that is gone */ }
        weStarted = true;
        if (launch) launch();
        else {
          if (!chromeBin || !existsSync(chromeBin)) return { why: `${WHY_NOT_STARTED}: Chrome was not found (SCOUT_CHROME_BIN)` };
          mkdirSync(profile, { recursive: true });
          resetWindowPlacement(profile);
          try {
            const [cmd, args, verbatim] = detachedChromeCommand(chromeBin, profileChromeArgs(profile));
            spawn(cmd, args, { detached: true, stdio: "ignore", windowsHide: true, windowsVerbatimArguments: verbatim }).on("error", () => {}).unref();
          } catch (e) {
            return { why: `${WHY_NOT_STARTED}: ${e.message}` };
          }
          log(`started Chrome on the browser profile (${profile})`);
        }
        s = state();
      }
      if (s.running && s.endpoint) return s;
      if (Date.now() >= until) break;
      await sleep(250);
    }
    return { why: s.running ? `${WHY_NOT_STARTED}: Chrome is open on the browser profile without its DevTools port; close it` : WHY_NOT_STARTED };
  };

  // The window of a tab put at `ON_SCREEN` bounds in the normal state, once (a saved off-screen placement is never kept).
  // Go's window and a window the bridge just made are always put there; `onlyIfHidden` (Chrome's own first window, which
  // may be the owner's to have moved or sized) only when it is found minimized or off screen. Resolves with whether the
  // window was set. Nothing here raises or focuses it (no `Target.activateTarget`, no `Page.bringToFront`), and nothing
  // minimizes it: the bridge never hides the window, and a tab opened in it asks nothing of it (the owner, 2026-11-04, which replaces the 2026-10-16 rule that it stays minimized; since 1.176.1 hiding
  // never set the window to normal first, so Windows saved the minimized window at -32000,-32000 in the profile and it came
  // back off screen when the owner restored it).
  // `resetWindowPlacement` fixes the saved placement only before a Chrome the bridge starts: in `own` mode, and in a profile
  // Chrome that was already open, this is what puts the window back.
  // A window found minimized (or maximized, or full screen) is restored by one ask and put at the bounds by a second: Chrome
  // (`Browser.setWindowBounds`) only restores it and leaves out the bounds of that same ask, so one ask would bring a
  // minimized window back at the -32000,-32000 Windows parked it at (read from Chromium's source, 2026-11-04; the fake
  // Chrome of the tests does the same). The restore is the one thing here that can activate the window, once, and only for
  // a window found minimized: Windows restores a minimized window by showing it (docs/PRD.md 52.8, not checked live).
  const showOnScreen = async (targetId, onlyIfHidden = false) => {
    const w = await internal("Browser.getWindowForTarget", { targetId });
    const windowId = w.result?.windowId;
    if (windowId === undefined) return false;
    const found = (await internal("Browser.getWindowBounds", { windowId })).result?.bounds;
    if (onlyIfHidden && !hiddenWindow(found)) return false;
    if (found?.windowState && found.windowState !== "normal") await internal("Browser.setWindowBounds", { windowId, bounds: { windowState: "normal" } });
    await internal("Browser.setWindowBounds", { windowId, bounds: { windowState: "normal", ...ON_SCREEN } });
    return true;
  };

  // The profile's Chrome is the app's own, so a blank tab nobody here owns (one opened and never navigated, a failed
  // navigation's, Chrome's new tab page) is closed: in the bridge's window, or in a window that holds nothing but blank
  // tabs. Never the bridge's own tab, so Chrome always keeps one and never ends for it; never in a window that holds a tab
  // handed to the owner (Go's), nor in the user's everyday Chrome.
  const closeBlanks = async () => {
    if (mode !== "profile" || !anchor || !weStarted) return; // Chrome the owner had open is never swept
    const pages = ((await internal("Target.getTargets", {})).result?.targetInfos || []).filter((t) => t.type === "page");
    if (!anchor) return;
    const windowOf = new Map();
    for (const t of pages) windowOf.set(t.targetId, (await internal("Browser.getWindowForTarget", { targetId: t.targetId })).result?.windowId);
    const ownersWindows = new Set(pages.filter((t) => targets.get(t.targetId) === KEPT).map((t) => windowOf.get(t.targetId)));
    const ours = windowOf.get(anchor.targetId);
    for (const t of pages) {
      if (!BLANK_URLS.has(t.url) || targets.get(t.targetId) || t.targetId === anchor?.targetId) continue;
      const win = windowOf.get(t.targetId);
      if (win === undefined || ownersWindows.has(win)) continue;
      if (win === ours) { // only the app's own window: a blank tab in any other is the owner's
        void internal("Target.closeTarget", { targetId: t.targetId });
        log("closed a blank tab");
      }
    }
  };

  // Close these tabs, named by the app (`POST /tabs/close`): never the bridge's own first tab. How many closed.
  const closeTabs = async (ids) => {
    if (!upstream || !Array.isArray(ids)) return { closed: 0 };
    let closed = 0;
    for (const targetId of ids.filter((t) => typeof t === "string" && t && t !== anchor?.targetId).slice(0, 500)) {
      const r = await internal("Target.closeTarget", { targetId });
      if (!r.error && r.result?.success !== false) closed++;
    }
    return { closed };
  };

  // The app's own profile holds only what the register keeps (`keep`: target ids), the bridge's first tab and the tabs
  // of the sessions connected now; every other page tab is closed. The tabs to keep that are not in Chrome any more are
  // named in `gone`, so the app drops their rows. Never in the user's everyday Chrome, and never with no Chrome.
  const reconcileTabs = async (keep) => {
    if (mode !== "profile") return { closed: 0, gone: [], skipped: "the user's everyday Chrome is never swept" };
    if (!upstream || !Array.isArray(keep)) return { closed: 0, gone: [], skipped: "no Chrome, or no list to keep" };
    await ensureAnchor(); // Chrome always keeps one tab, so closing the rest never ends it
    const found = await internal("Target.getTargets", {});
    const pages = (found.result?.targetInfos || []).filter((t) => t.type === "page");
    const present = new Set(pages.map((t) => t.targetId));
    const keeping = new Set(keep.filter((t) => typeof t === "string"));
    for (const id of keeping) if (present.has(id) && !targets.get(id)) targets.set(id, KEPT); // the owner's, from before
    let closed = 0;
    for (const t of pages) {
      if (keeping.has(t.targetId) || t.targetId === anchor?.targetId) continue;
      const owner = targets.get(t.targetId);
      if (owner === KEPT || owner === BRIDGE || (owner && !owner.closed)) continue; // a session that is connected now
      void internal("Target.closeTarget", { targetId: t.targetId });
      closed++;
    }
    return { closed, gone: [...keeping].filter((id) => !present.has(id)) };
  };

  // On connecting to the app's own profile: ask the app what its register holds, and close the rest. The app not
  // answering closes nothing.
  const reconcileFromRegister = async () => {
    if (mode !== "profile") return;
    const keep = await register.reconcile();
    if (!Array.isArray(keep)) { log("the app did not say which tabs to keep: no tab is closed"); return; }
    const r = await reconcileTabs(keep);
    if (r.closed) log(`closed ${r.closed} tab(s) the register does not hold`);
    for (const id of r.gone || []) void register.removed(id);
  };

  // A first tab an earlier bridge left: titled resumebot, and at the data: page or at the app's page (never the app's own screens,
  // which carry the same title at any other path: the owner's tab of the app is not the bridge's).
  const isFirstTab = (t) => t.title === "resumebot"
    && (String(t.url || "").startsWith("data:text/html,") || (firstTabUrl !== ANCHOR_URL && t.url === firstTabUrl));

  const closeLeftovers = async () => {
    if (mode === "profile") await ensureAnchor(); // first, so the tabs closed below are never Chrome's last
    const found = await internal("Target.getTargets", {});
    for (const t of found.result?.targetInfos || []) {
      if (t.type === "page" && isFirstTab(t) && t.targetId !== anchor?.targetId) {
        void internal("Target.closeTarget", { targetId: t.targetId });
        log("closed the resumebot tab an earlier bridge left");
      }
    }
    await closeBlanks();
  };

  // Connected to Chrome, or why not. One try at a time; in the owner's Chrome a try asks the owner (Chrome's prompt) and
  // waits for the Allow up to `consentMs`.
  // Asking for Chrome while the bridge is closing it (`endChrome`) waits for that to be done first.
  // A request answered while no session is connected begins the grace again: the runner asks for Chrome (`/json/version`) and
  // then starts a session, which takes twenty seconds or more to connect, and Chrome must not end under it.
  const connect = async () => {
    if (ending) await ending;
    const why = await connectNow();
    if (!why && !clients.size) armIdle();
    return why;
  };
  // `adopt`: the bridge looking at a Chrome it finds running, not a request for one: it starts none, and what it cannot
  // connect to is no failure of the browser jobs for the status to report.
  const connectNow = (adopt = false) => {
    if (upstream) return Promise.resolve("");
    if (connecting) return connecting;
    connecting = (async () => {
      let s;
      if (mode === "profile") {
        s = await startProfileChrome(adopt);
        if (s.why) { if (!adopt) failed = { why: s.why, endpoint: "" }; return s.why; }
      } else {
        s = state();
        if (!s.running) return WHY_CLOSED;
        if (!s.endpoint) return WHY_SWITCH;
      }
      const asked = mode === "own" ? WHY_CONSENT : WHY_NOT_STARTED;
      const why = await new Promise((resolve) => {
        let done = false;
        let ws;
        const end = (w) => { if (!done) { done = true; clearTimeout(timer); resolve(w); } };
        const timer = setTimeout(() => { end(asked); try { ws?.close(); } catch {} }, mode === "own" ? consentMs : startMs);
        try {
          ws = new WebSocketImpl(s.endpoint);
        } catch {
          end(mode === "own" ? WHY_CLOSED : WHY_NOT_STARTED);
          return;
        }
        ws.onopen = () => {
          if (done) { try { ws.close(); } catch {} return; }
          upstream = ws;
          end("");
        };
        // Refused outright (the port is not listening: a file left by a Chrome that is gone) is a closed Chrome; a refusal
        // after Chrome answered, or no answer in time, is the prompt not allowed.
        ws.onerror = (e) => end(/ECONNREFUSED/.test(String(e?.error?.cause?.code || e?.error?.code || e?.message || ""))
          ? (mode === "own" ? WHY_CLOSED : WHY_NOT_STARTED) : asked);
        ws.onclose = () => {
          if (upstream === ws) { log("Chrome closed its DevTools connection"); lost(); }
          end(asked);
        };
        ws.onmessage = (m) => { if (upstream === ws) onUpstream(typeof m.data === "string" ? m.data : Buffer.from(m.data).toString("utf8")); };
      });
      if (!(why && adopt)) failed = why ? { why, endpoint: s.endpoint } : null;
      if (!why) {
        log(mode === "own" ? "connected to your own Chrome" : "connected to the Chrome of the browser profile");
        void internal("Browser.getVersion").then((r) => { product = r.result?.product || product; });
        // Discovery on for the bridge itself: a tab it opens from a page is known by Chrome's targetCreated, which only
        // comes with discovery on (a session such as the app's own code never turns it on, so its tabs never opened).
        await internal("Target.setDiscoverTargets", { discover: true });
        await closeLeftovers();
        await reconcileFromRegister();
        if (!clients.size) armIdle();
      }
      return why;
    })().finally(() => { connecting = null; });
    return connecting;
  };

  // The app's Chrome left running by an earlier bridge (the app restarted for a release) is connected to when the bridge
  // starts, so that it arms the same idle check as any Chrome it holds and closes it when nothing is queued: it would
  // otherwise never be looked at, since no session connects when nothing is queued. Never starts Chrome, and never in the
  // user's everyday Chrome (connecting to that asks the owner).
  let stopped = false;
  const adoptRunning = async () => {
    if (mode !== "profile" || !idleCloseMs) return;
    const found = state();
    if (!found.running || !found.endpoint || stopped) return;
    try {
      const why = await connectNow(true);
      if (why) log(`found the app's Chrome running and could not connect to it (${why}); it is looked at when a session asks`);
      else log("found the app's Chrome running: closing it when the browser queue is empty");
    } catch (e) {
      log(`found the app's Chrome running and could not connect to it (${e?.message})`);
    }
  };

  // Only this machine's own clients, never a web page: no Origin (a browser always sends one) and a loopback Host, so a
  // page cannot reach the bridge by a name that points here.
  const local = (req) => !req.headers.origin && /^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/i.test(req.headers.host || "");
  const json = (res, code, body) => res.writeHead(code, { "Content-Type": "application/json" }).end(JSON.stringify(body));

  const server = createServer(async (req, res) => {
    if (!local(req) || !carriesKey(req, key)) return json(res, 403, { why: "only this machine's own clients" });
    const url = (req.url || "").split("?")[0].replace(/\/$/, "");
    if (req.method === "POST" && (url === "/tabs/close" || url === "/tabs/reconcile")) {
      let body;
      try {
        let text = "";
        for await (const chunk of req) { text += chunk; if (text.length > MAX_BODY) return json(res, 413, {}); }
        body = JSON.parse(text || "{}");
      } catch { return json(res, 400, { why: "not JSON" }); }
      return json(res, 200, url === "/tabs/close" ? await closeTabs(body.targets) : await reconcileTabs(body.keep));
    }
    if (req.method !== "GET") return json(res, 405, {});
    if (url === "/status") return json(res, 200, { why: status() });
    if (url === "/json" || url === "/json/list") return json(res, 200, []); // never a list of the owner's tabs
    if (url !== "/json/version") return json(res, 404, {});
    const why = await connect();
    if (why) return json(res, 503, { why });
    json(res, 200, { Browser: product, "Protocol-Version": "1.3", webSocketDebuggerUrl: `ws://127.0.0.1:${port}/devtools/browser/resumebot?${BRIDGE_KEY_QUERY}=${encodeURIComponent(key)}` });
  });

  server.on("upgrade", async (req, socket) => {
    const wsKey = req.headers["sec-websocket-key"];
    if (!local(req) || !carriesKey(req, key) || !wsKey || !(req.url || "").startsWith("/devtools/browser/")) {
      socket.end("HTTP/1.1 403 Forbidden\r\n\r\n");
      return;
    }
    const why = await connect();
    if (why) {
      socket.end(`HTTP/1.1 503 Service Unavailable\r\nContent-Type: text/plain\r\n\r\n${why}`);
      return;
    }
    const accept = createHash("sha1").update(`${wsKey}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest("base64");
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
    socket.setNoDelay(true);
    // Whose it is: the name in the address (a session's id, or `code.<job>` and `go` for the app's own code); a connection
    // that gives none (the old address) is named here.
    const named = decodeURIComponent((req.url || "").split("?")[0].slice("/devtools/browser/".length)).replace(/[^A-Za-z0-9_.:-]/g, "_").slice(0, 100);
    const label = named && named !== "resumebot" ? named : `conn-${++conns}-${Date.now().toString(36)}`;
    const client = { socket, closed: false, label, own: ownsItsTabs(label) };
    clients.add(client);
    armIdle(); // a session that outlives its work is said in the log when the grace is over, never silent
    serveFrames(socket, (text) => onClient(client, text), () => dropClient(client));
  });

  return {
    server,
    status,
    connect,
    get connected() { return !!upstream; },
    // The port it listens on: the one asked for, or the one the system picked for port 0.
    get port() { return port; },
    start: () => new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(port, "127.0.0.1", () => {
        server.off("error", reject);
        port = server.address().port;
        resolve();
        void adoptRunning();
      });
    }),
    stop: () => new Promise((resolve) => {
      stopped = true;
      disarmIdle();
      for (const client of [...clients]) dropClient(client);
      setTimeout(() => {
        try { upstream?.close(); } catch {}
        upstream = null;
        server.close(() => resolve());
        server.closeAllConnections?.();
      }, 200); // the closes above reach Chrome first
    }),
  };
}
