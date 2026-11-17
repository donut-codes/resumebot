// The browser bridge (own-chrome.mjs): a stand-in Chrome behind a fake DevTools connection, real sessions in front of it
// over WebSocket. Run: node --test (from app/scout). Checked against a real Chrome 155 with chrome-devtools-mcp 1.10.1 too
// (two sessions, the owner's tabs in two windows): every session tab opened in the one "resumebot" window, none saw the
// other's or the owner's, and each session's tabs closed when it ended.
import assert from "node:assert/strict";
import { connect, createServer } from "node:net";
import { describe, it } from "node:test";
import { existsSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ANCHOR_URL, IDLE_GRACE_MS, NO_REGISTER, ON_SCREEN, WINDOW_PAGE_PATH, anchorUrl, ownsItsTabs, carriesKey, BRIDGE_KEY_HEADER, resetWindowPlacement, WHY_CLOSED, WHY_CONSENT, WHY_SWITCH, chromeState, chromeUserData, detachedChromeCommand, ownChromeBridge, profileChromeArgs } from "./own-chrome.mjs";

// The bridge serves only clients that carry its key (docs/PRD.md path 52; the owner, 2026-11-14): every bridge below is made
// with KEY, and the `fetch` and sessions of these tests carry it. The tests of the refusal use `bare` (global fetch) instead.
const KEY = "k".repeat(64);
const bare = globalThis.fetch;
const fetch = (url, init = {}) => bare(url, { ...init, headers: { "X-Bridge-Key": KEY, ...init.headers } });
const freePort = () => new Promise((resolve) => {
  const s = createServer().listen(0, "127.0.0.1", () => { const { port } = s.address(); s.close(() => resolve(port)); });
});
const until = async (check, ms = 3000) => {
  for (const end = Date.now() + ms; Date.now() < end;) {
    if (check()) return;
    await new Promise((r) => setTimeout(r, 10));
  }
  assert.fail("timed out");
};

// Chrome, standing in: one tab of the owner's ("OWNER"), and every command the bridge sends it. A tab opened by
// `window.open` in a page is created with that page as its opener, as Chrome does.
// Like Chrome: a new target is only announced (targetCreated) with discovery on; with `endsWhenEmpty`, closing its last tab
// ends Chrome (`onEnd`, after `endDelayMs`), as Chrome does. With `lockLingerMs`, Chrome closes its DevTools socket first and
// the process, with its profile lock, goes that long after (`onEnd` then): until it does, nothing answers on its port (a new
// connection is refused), as with a Chrome that is shutting down; `chrome.dead` says so, and a Chrome started again clears it.
// With `startMinimized`, every window is found minimized until it is asked
// otherwise; with `offScreen`, every window is at -32000,-32000, the placement Windows records for a minimized window and
// Chrome saves in the profile; with `silentOpen`, a `window.open`
// shows its tab and the page never answers the evaluate that asked for it. `windowState` and `placement` are the windows'.
// With `pausePopups`, as Chrome does while any DevTools connection has auto-attach on: a tab a `window.open` makes is paused for
// the debugger, and its opener is paused with it: the evaluate that opened it is not answered, and a `window.open` in that opener
// gives no tab (`false`), until `Runtime.runIfWaitingForDebugger` arrives for the tab's own session.
function fakeChrome({ opens = true, noOpenFrom = new Set(), failOpens = 0, popupDelayMs = 0, announceDestroyed = false, leftover = false, tabs = ["OWNER"], endsWhenEmpty = false, onEnd = () => {}, startMinimized = false, offScreen = false, silentOpen = false, endDelayMs = 1, lockLingerMs = 0, pausePopups = false } = {}) {
  const chrome = { sent: [], sockets: [], nextTarget: 1, live: new Set(tabs), info: {}, windows: {}, windowState: {}, placement: {}, discover: false, order: [], failOpens, paused: {} };
  for (const t of tabs) if (t !== "OWNER") chrome.info[t] = { title: "", url: "about:blank" };
  if (leftover) { // the "resumebot" tab a bridge that was ended outright left behind: the data page, or (a string) the page it was at
    chrome.live.add("OLD");
    chrome.info.OLD = { title: "resumebot", url: leftover === true ? ANCHOR_URL : leftover };
  }
  chrome.WebSocketImpl = class {
    constructor(url) {
      this.url = url;
      chrome.sockets.push(this);
      if (opens) setTimeout(() => (chrome.dead ? this.onerror?.({ message: "connect ECONNREFUSED 127.0.0.1:1" }) : this.onopen?.()), 5);
    }
    emit(msg) { this.onmessage?.({ data: JSON.stringify(msg) }); }
    close() { this.onclose?.(); }
    send(text) {
      const msg = JSON.parse(text);
      chrome.sent.push(msg);
      const reply = (result) => setTimeout(() => this.emit({ id: msg.id, result, ...(msg.sessionId ? { sessionId: msg.sessionId } : {}) }), 1);
      const make = (info, windowId, pause = () => {}) => {
        const targetId = `T${chrome.nextTarget++}`;
        pause(targetId); // paused before anyone is told of it, as in Chrome
        chrome.live.add(targetId);
        chrome.info[targetId] = { url: info.url };
        if (windowId !== undefined) chrome.windows[targetId] = windowId;
        if (chrome.discover) this.emit({ method: "Target.targetCreated", params: { targetInfo: { targetId, type: "page", ...info } } });
        if (chrome.autoAttach) this.emit({ method: "Target.attachedToTarget", params: { sessionId: `S-${targetId}`, targetInfo: { targetId, type: "page", ...info }, waitingForDebugger: true } });
        return targetId;
      };
      switch (msg.method) {
        case "Target.setDiscoverTargets":
          chrome.discover = msg.params.discover !== false;
          this.emit({ method: "Target.targetCreated", params: { targetInfo: { targetId: "OWNER", type: "page", title: "owner's mail", url: "https://mail.example.test/" } } });
          return reply({});
        case "Target.setAutoAttach":
          chrome.autoAttach = true;
          this.emit({ method: "Target.attachedToTarget", params: { sessionId: "S-OWNER", targetInfo: { targetId: "OWNER", type: "page" }, waitingForDebugger: true } });
          return reply({});
        case "Target.createTarget":
          return reply({ targetId: make({ url: msg.params.url }, msg.params.newWindow ? 100 + chrome.nextTarget : undefined) });
        case "Target.attachToTarget":
          this.emit({ method: "Target.attachedToTarget", params: { sessionId: `X-${msg.params.targetId}`, targetInfo: { targetId: msg.params.targetId, type: "page" }, waitingForDebugger: false } });
          return reply({ sessionId: `X-${msg.params.targetId}` });
        case "Runtime.evaluate": {
          const opener = msg.sessionId.replace(/^[SX]-/, "");
          if (chrome.hang && /window\.open\(/.test(msg.params.expression)) return; // Chrome never answers it
          if (/window\.open\(/.test(msg.params.expression)) chrome.order.push(`open@${opener}`);
          if (/window\.open\(/.test(msg.params.expression) && (noOpenFrom.has(opener) || chrome.failOpens-- > 0)) return reply({ result: { type: "boolean", value: false } });
          if (/window\.open\(/.test(msg.params.expression) && Object.values(chrome.paused).some((p) => p.opener === opener)) return reply({ result: { type: "boolean", value: false } });
          if (/window\.open\(/.test(msg.params.expression)) {
            const info = { url: JSON.parse(/window\.open\(("[^"]*")/.exec(msg.params.expression)[1]), openerId: opener };
            const holds = pausePopups && chrome.autoAttach; // the tab is made paused: the opener's evaluate waits for its resume
            const popup = () => {
              chrome.order.push("created");
              make(info, chrome.windows[opener], (id) => {
                if (holds) chrome.paused[`S-${id}`] = { opener, release: () => reply({ result: { type: "boolean", value: true } }) };
              });
            };
            if (popupDelayMs) setTimeout(popup, popupDelayMs);
            else popup();
            if (silentOpen || holds) return;
          }
          return reply({ result: { type: "boolean", value: true } });
        }
        case "Runtime.runIfWaitingForDebugger": {
          const held = chrome.paused[msg.sessionId];
          delete chrome.paused[msg.sessionId];
          held?.release();
          return reply({});
        }
        case "Browser.getWindowForTarget":
          return reply({ windowId: chrome.windows[msg.params.targetId] ?? 7 });
        case "Browser.setWindowBounds": {
          (chrome.bounds ??= []).push(msg.params);
          const { windowState, ...where } = msg.params.bounds;
          const now = chrome.windowState[msg.params.windowId] || (startMinimized ? "minimized" : "normal");
          // As Chrome does (BrowserHandler::SetWindowBounds): asking a minimized, maximized or full-screen window for the normal
          // state only restores it, and the bounds in that same ask are left out. Bounds are set only on a window already normal.
          if (windowState === "normal" && now !== "normal") chrome.windowState[msg.params.windowId] = "normal";
          else {
            if (windowState) chrome.windowState[msg.params.windowId] = windowState;
            if (Object.keys(where).length) chrome.placement[msg.params.windowId] = where;
          }
          return reply({});
        }
        case "Browser.getWindowBounds": {
          const at = chrome.placement[msg.params.windowId] || (offScreen ? { left: -32000, top: -32000, width: 1280, height: 860 } : { left: 200, top: 120, width: 1100, height: 700 });
          return reply({ bounds: { windowState: chrome.windowState[msg.params.windowId] || (startMinimized ? "minimized" : "normal"), ...at } });
        }
        case "Target.closeTarget":
          chrome.live.delete(msg.params.targetId);
          delete chrome.paused[`S-${msg.params.targetId}`]; // a tab that is closed is no longer paused, and its opener is no longer held
          if (announceDestroyed && chrome.discover) setTimeout(() => this.emit({ method: "Target.targetDestroyed", params: { targetId: msg.params.targetId } }), 2);
          if (endsWhenEmpty && chrome.live.size === 0) {
            chrome.ended = true;
            setTimeout(() => {
              if (lockLingerMs) chrome.dead = true;
              this.onclose?.();
              if (lockLingerMs) setTimeout(onEnd, lockLingerMs); else onEnd();
            }, endDelayMs);
          }
          return reply({ success: true });
        case "Browser.close":
          chrome.live.clear();
          chrome.ended = true;
          setTimeout(() => { this.onclose?.(); onEnd(); }, endDelayMs);
          return reply({});
        case "Target.getTargets":
          if (chrome.hangTargets) return; // Chrome never says what is open
          return reply({ targetInfos: [...chrome.live].map((targetId) => ({ targetId, type: "page", ...chrome.info[targetId] })) });
        default:
          return reply({});
      }
    }
  };
  return chrome;
}

const RUNNING = () => ({ running: true, endpoint: "ws://127.0.0.1:1/devtools/browser/x" });

// The app's register, standing in: what the bridge told it, what it answers when asked what to keep, and what it says the
// browser queue holds (`work`: { waiting, leased }, or null for an app that does not say).
function fakeRegister({ keep = [], work = { waiting: 0, leased: 0 } } = {}) {
  const told = { opened: [], removed: [], released: [], reconciles: 0, asked: 0 };
  return {
    told, keep, work,
    busy: async function () { told.asked++; return this.work; },
    opened: async (targetId, ownerId, kind) => { told.opened.push([targetId, ownerId, kind]); },
    removed: async (targetId) => { told.removed.push(targetId); },
    released: async (ownerId) => { told.released.push(ownerId); },
    reconcile: async function () { told.reconciles++; return this.keep; },
  };
}

// A session of the bridge: a real WebSocket client, its messages kept. `name`: the name in the address (a session's id).
async function session(port, name = "resumebot") {
  const ws = new WebSocket(`ws://127.0.0.1:${port}/devtools/browser/${name}?key=${KEY}`);
  const got = [];
  let id = 0;
  const waiting = new Map();
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    got.push(msg);
    if (msg.id !== undefined) waiting.get(msg.id)?.(msg);
  };
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  return {
    ws, got,
    call: (method, params = {}, sessionId) => new Promise((resolve) => {
      const n = ++id;
      waiting.set(n, resolve);
      ws.send(JSON.stringify({ id: n, method, params, ...(sessionId ? { sessionId } : {}) }));
    }),
    events: () => got.filter((m) => m.method),
  };
}

async function bridgeWith(chrome, options = {}) {
  const port = await freePort();
  const bridge = ownChromeBridge({ key: KEY, port, mode: "own", state: RUNNING, WebSocketImpl: chrome.WebSocketImpl, ...options });
  await bridge.start();
  return { port, bridge };
}

describe("the browser bridge", () => {
  it("finds the owner's profile and reads whether Chrome runs and remote debugging is on", () => {
    assert.equal(chromeUserData({ LOCALAPPDATA: "D:\\local" }, "win32"), "D:\\local\\Google\\Chrome\\User Data");
    assert.equal(chromeUserData({}, "darwin", "/h/o"), "/h/o/Library/Application Support/Google/Chrome");
    assert.equal(chromeUserData({ SCOUT_CHROME_USER_DATA: "/p" }, "linux"), "/p");
    const files = { "/u/SingletonLock": "", "/u/DevToolsActivePort": "9222\n/devtools/browser/abc\n" };
    const io = { platform: "linux", exists: (f) => f in files, read: (f) => { if (!(f in files)) throw new Error("ENOENT"); return files[f]; } };
    assert.deepEqual(chromeState("/u", io), { running: true, endpoint: "ws://127.0.0.1:9222/devtools/browser/abc" });
    delete files["/u/DevToolsActivePort"];
    assert.deepEqual(chromeState("/u", io), { running: true, endpoint: "" });
    assert.ok(profileChromeArgs("/p").includes("--remote-debugging-port=0") && !profileChromeArgs("/p").some((a) => a.startsWith("--headless")));
  });

  it("in the owner's own Chrome, says in one line why the browser jobs cannot run: closed, remote debugging off, or the prompt not allowed", async () => {
    let state = { running: false, endpoint: "" };
    const chrome = fakeChrome({ opens: false });
    const { port, bridge } = await bridgeWith(chrome, { consentMs: 100, state: () => state });
    try {
      const version = async () => { const r = await fetch(`http://127.0.0.1:${port}/json/version`); return [r.status, (await r.json()).why]; };
      assert.deepEqual(await version(), [503, WHY_CLOSED]);
      state = { running: true, endpoint: "" };
      assert.deepEqual(await version(), [503, WHY_SWITCH]);
      assert.equal(chrome.sockets.length, 0, "nothing is tried before remote debugging is on");
      state = RUNNING();
      assert.equal((await (await fetch(`http://127.0.0.1:${port}/status`)).json()).why, "", "may: nothing tried yet");
      assert.deepEqual(await version(), [503, WHY_CONSENT]);
      assert.equal((await (await fetch(`http://127.0.0.1:${port}/status`)).json()).why, WHY_CONSENT, "the status remembers the prompt was not allowed");
    } finally {
      await bridge.stop();
    }
  });

  it("starts the profile's Chrome when a session needs it (SCOUT_BROWSER=profile)", async () => {
    let state = { running: false, endpoint: "" };
    let started = 0;
    const chrome = fakeChrome();
    const { port, bridge } = await bridgeWith(chrome, {
      mode: "profile", profile: "/nowhere/profile", state: () => state,
      launch: () => { started++; setTimeout(() => { state = RUNNING(); }, 50); },
    });
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`);
      assert.equal(r.status, 200);
      assert.equal(started, 1);
      assert.equal((await fetch(`http://127.0.0.1:${port}/json/version`)).status, 200);
      assert.equal(started, 1, "started once, and left running");
    } finally {
      await bridge.stop();
    }
  });

  // Edited 2026-11-04 (the owner: "close it when the queue is empty"). It pinned "with no other tab Chrome stays as it is" (the
  // 2026-10-22 "be kind"); that case moved to "Chrome only while the browser has work" below, where an empty queue ends Chrome.
  // What stays: tabs the owner should see keep Chrome open, whatever the app says of the queue, and only the bridge's own
  // window goes.
  it("with the owner's tabs open when the queue is done, only the bridge's own window goes: Chrome is not ended", async () => {
    const chrome = fakeChrome({ tabs: ["OWNER"], endsWhenEmpty: true });
    const register = fakeRegister({ keep: ["OWNER"] }); // the app says the queue is empty, and holds the tab
    const { port, bridge } = await bridgeWith(chrome, {
      mode: "profile", profile: "/nowhere/profile", state: RUNNING, idleCloseMs: 100, launch: () => {}, register,
    });
    try {
      const s = await session(port);
      s.ws.close();
      await new Promise((r) => setTimeout(r, 450));
      assert.ok(!chrome.sent.some((m) => m.method === "Browser.close"), "the bridge never asks Chrome to close");
      assert.equal(chrome.ended, undefined, "Chrome is not ended");
      assert.equal(chrome.live.has("OWNER"), true, "the owner's tab stays");
      const anchors = chrome.sent.filter((m) => m.method === "Target.createTarget" && m.params.url === ANCHOR_URL).length;
      assert.equal(anchors, 1);
      assert.ok(chrome.sent.some((m) => m.method === "Target.closeTarget"), "its own window's tab goes");
      assert.equal(register.told.asked, 0, "the owner's tab settles it: the queue is not even asked");
    } finally {
      await bridge.stop();
    }
  });

  it("opens Chrome once for a whole queue: sessions that follow one another within the grace period reuse it", async () => {
    let state = { running: false, endpoint: "" };
    let started = 0;
    const chrome = fakeChrome();
    const { port, bridge } = await bridgeWith(chrome, {
      mode: "profile", profile: "/nowhere/profile", state: () => state, idleCloseMs: 300,
      launch: () => { started++; state = RUNNING(); },
    });
    try {
      for (let i = 0; i < 3; i++) {
        const s = await session(port);
        await new Promise((r) => setTimeout(r, 100));
        s.ws.close();
        await new Promise((r) => setTimeout(r, 100)); // the next item, inside the grace
      }
      assert.equal(started, 1, "started once for the whole queue");
      assert.ok(!chrome.sent.some((m) => m.method === "Browser.close"), "not closed between items");
      await new Promise((r) => setTimeout(r, 500));
      assert.ok(!chrome.sent.some((m) => m.method === "Browser.close"), "and never closed after it");
    } finally {
      await bridge.stop();
    }
  });

  it("never takes the owner's focus: Chrome is not started minimized (2026-11-04), its first tab opens in the background, no session can raise a tab", async () => {
    assert.ok(!profileChromeArgs("/p").includes("--start-minimized"), "the window is visible, never started hidden");
    assert.ok(!profileChromeArgs("/p").includes("--start-maximized"));
    const chrome = fakeChrome();
    const { port, bridge } = await bridgeWith(chrome);
    try {
      const s = await session(port);
      const made = await s.call("Target.createTarget", { url: "https://jobs.example.test/1" });
      assert.ok(made.result?.targetId, "a session's tab opens");
      const raised = await s.call("Target.activateTarget", { targetId: made.result.targetId });
      assert.ok(raised.error, "Target.activateTarget is refused");
      await s.call("Page.bringToFront", {}, undefined);
      const sent = chrome.sent.map((m) => m.method);
      assert.ok(!sent.includes("Target.activateTarget") && !sent.includes("Page.bringToFront"), "nothing that raises a window reaches Chrome");
      const anchor = chrome.sent.find((m) => m.method === "Target.createTarget");
      assert.equal(anchor.params.background, true, "the window's first tab is a background target");
    } finally {
      await bridge.stop();
    }
  });

  it("Chrome the owner already had open: a window of new-tab pages is the owner's and is never swept, and stopping closes only the app's own tabs", async () => {
    const chrome = fakeChrome({ tabs: ["NTP1", "NTP2"], endsWhenEmpty: true });
    chrome.info.NTP1 = { title: "New Tab", url: "chrome://newtab/" };
    chrome.info.NTP2 = { title: "New Tab", url: "chrome://newtab/" };
    chrome.windows.NTP1 = chrome.windows.NTP2 = 55;
    const port = await freePort();
    const bridge = ownChromeBridge({ key: KEY,
      port, mode: "profile", idleCloseMs: 0, WebSocketImpl: chrome.WebSocketImpl,
      state: RUNNING, launch: () => {},
    });
    await bridge.start();
    try {
      const s = await session(port);
      const made = await s.call("Target.createTarget", { url: "https://jobs.example.test/1" });
      assert.ok(made.result?.targetId);
      s.ws.close();
      await new Promise((r) => setTimeout(r, 2600));
      assert.ok(chrome.live.has("NTP1") && chrome.live.has("NTP2"), "the owner's new-tab pages stay");
    } finally {
      await bridge.stop();
    }
    assert.ok(chrome.live.has("NTP1") && chrome.live.has("NTP2") && !chrome.ended, "stopping the bridge leaves them and Chrome");
    const closed = chrome.sent.filter((m) => m.method === "Target.closeTarget").map((m) => m.params.targetId);
    assert.ok(!closed.includes("NTP1") && !closed.includes("NTP2") && !closed.includes("OWNER"), "only tabs the app made were closed");
    assert.ok(!chrome.sent.some((m) => m.method === "Browser.close"));
  });

  it("starts Chrome as nobody's child on Windows (taskkill /T of the launcher cannot reach it) and plainly elsewhere", () => {
    const bs = String.fromCharCode(92);
    const exe = ["C:", "Program Files", "Chrome", "chrome.exe"].join(bs);
    const [cmd, args, verbatim] = detachedChromeCommand(exe, ["--user-data-dir=" + ["C:", "a b"].join(bs), "--no-first-run"], "win32");
    assert.equal(cmd, "cmd.exe");
    assert.equal(verbatim, true);
    assert.equal(args.join(" "), `/d /s /c "start "" /b "${exe}" "--user-data-dir=C:${bs}a b" --no-first-run"`);
    assert.deepEqual(detachedChromeCommand("/usr/bin/chrome", ["--x"], "linux"), ["/usr/bin/chrome", ["--x"], false]);
  });

  it("never closes the owner's own Chrome, whatever the queue holds (2026-11-04)", async () => {
    const chrome = fakeChrome({ endsWhenEmpty: true });
    const register = fakeRegister(); // the queue is empty
    const { port, bridge } = await bridgeWith(chrome, { idleCloseMs: 50, register });
    try {
      const s = await session(port);
      s.ws.close();
      await new Promise((r) => setTimeout(r, 400));
      assert.ok(!chrome.sent.some((m) => m.method === "Browser.close"));
      assert.equal(chrome.ended, undefined);
      assert.equal(chrome.live.has("OWNER"), true, "the owner's tab stays");
      assert.equal(register.told.asked, 0, "the idle close is the app's own Chrome's only: the queue is not even asked");
      assert.equal(bridge.connected, true);
    } finally {
      await bridge.stop();
    }
  });

  it("never lists Chrome's tabs and refuses web pages", async () => {
    const chrome = fakeChrome();
    const { port, bridge } = await bridgeWith(chrome);
    try {
      assert.deepEqual(await (await fetch(`http://127.0.0.1:${port}/json/list`)).json(), []);
      assert.equal((await fetch(`http://127.0.0.1:${port}/json/version`, { headers: { Origin: "https://evil.example.test" } })).status, 403);
      const ok = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
      assert.equal(ok.webSocketDebuggerUrl, `ws://127.0.0.1:${port}/devtools/browser/resumebot?key=${KEY}`);
    } finally {
      await bridge.stop();
    }
  });

  it("says in its log whose a refused tab is, so a refusal that should not have been is found", async () => {
    const chrome = fakeChrome();
    const lines = [];
    const { port, bridge } = await bridgeWith(chrome, { log: (line) => lines.push(line) });
    try {
      const a = await session(port);
      const b = await session(port);
      await a.call("Target.setDiscoverTargets", { discover: true });
      const b1 = (await b.call("Target.createTarget", { url: "about:blank" })).result.targetId;
      assert.equal((await a.call("Target.attachToTarget", { targetId: b1, flatten: true })).error.message, "not a tab this session opened");
      assert.equal((await a.call("Target.attachToTarget", { targetId: "OWNER", flatten: true })).error.message, "not a tab this session opened");
      assert.ok(lines.some((l) => l.includes(`target ${b1} is another session's`)), lines.join(", "));
      assert.ok(lines.some((l) => l.includes("target OWNER is nobody's") || l.includes("target OWNER is unknown")), lines.join(", "));
    } finally {
      await bridge.stop();
    }
  });

  it("opens every session's pages as tabs of one window, shows each only its own, and refuses the rest", async () => {
    const chrome = fakeChrome();
    const { port, bridge } = await bridgeWith(chrome);
    try {
      const a = await session(port);
      const b = await session(port);
      await a.call("Target.setDiscoverTargets", { discover: true });
      await a.call("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true, filter: [{ type: "page", exclude: true }, {}] });
      // The owner's tab, auto-attached and paused, is let go at once and never shown.
      await until(() => chrome.sent.some((m) => m.method === "Target.detachFromTarget" && m.params.sessionId === "S-OWNER"));
      assert.ok(chrome.sent.some((m) => m.method === "Runtime.runIfWaitingForDebugger" && m.sessionId === "S-OWNER"), "resumed, so it loads as it would");
      assert.deepEqual(chrome.sent.find((m) => m.method === "Target.setAutoAttach").params.filter, [{ type: "page" }], "pages attach at the top, never through tabs");

      const a1 = (await a.call("Target.createTarget", { url: "about:blank" })).result.targetId;
      const b1 = (await b.call("Target.createTarget", { url: "about:blank" })).result.targetId;
      const a2 = (await a.call("Target.createTarget", { url: "https://jobs.example.test/" })).result.targetId;
      // One window: the bridge's own tab opened once, in a window of its own; every session page is a tab opened from it.
      const creates = chrome.sent.filter((m) => m.method === "Target.createTarget");
      assert.equal(creates.length, 1, "Chrome is asked for one new target only: the window's first tab");
      assert.equal(creates[0].params.url, ANCHOR_URL);
      assert.equal(creates[0].params.newWindow, true);
      const opened = chrome.sent.filter((m) => m.method === "Runtime.evaluate" && /window\.open/.test(m.params.expression));
      assert.equal(opened.length, 3);
      assert.ok(opened.every((m) => m.sessionId === "X-T1" && m.params.userGesture), "each from the window's first tab");
      assert.match(opened[2].params.expression, /jobs\.example\.test/);

      await until(() => a.events().filter((e) => e.method === "Target.attachedToTarget").length === 2);
      const seen = (s) => s.events().map((e) => e.params?.targetInfo?.targetId ?? e.params?.targetId);
      assert.ok(seen(a).includes(a1) && seen(a).includes(a2) && !seen(a).includes(b1), "a sees its own, not b's");
      assert.ok(seen(b).includes(b1) && !seen(b).includes(a1), "b sees its own, not a's");
      assert.ok(![...seen(a), ...seen(b)].includes("OWNER") && ![...seen(a), ...seen(b)].includes("T1"), "neither the owner's tab nor the bridge's");
      assert.ok(!JSON.stringify([...a.got, ...b.got]).includes("owner's mail"), "nor the owner's tab's title");

      assert.deepEqual((await a.call("Target.getTargets")).result.targetInfos.map((t) => t.targetId).sort(), [a1, a2].sort());
      for (const [method, params, sessionId] of [
        ["Target.attachToTarget", { targetId: "OWNER", flatten: true }], ["Target.closeTarget", { targetId: b1 }],
        ["Target.activateTarget", { targetId: "OWNER" }], ["Runtime.evaluate", { expression: "document.cookie" }, "S-OWNER"],
        ["Page.navigate", { url: "https://example.com" }, `S-${b1}`], ["Browser.close"], ["Storage.getCookies"],
        ["Target.createBrowserContext"], ["Target.createTarget", { url: "about:blank", browserContextId: "C1" }],
      ]) {
        const r = await a.call(method, params, sessionId);
        assert.ok(r.error, `${method} is refused`);
      }
      assert.ok(!chrome.sent.some((m) => ["Browser.close", "Storage.getCookies", "Target.createBrowserContext"].includes(m.method)), "never reaches Chrome");
      assert.ok((await a.call("Page.navigate", { url: "https://example.com" }, `S-${a1}`)).result, "its own tab it drives");
    } finally {
      await bridge.stop();
    }
  });

  // The runner closes every page a session opened when the session ends, however it ends (the owner, 2026-10-16).
  it("closes every tab a session opened when it ends, however it ends, and the window's first tab with the last session", async () => {
    const chrome = fakeChrome();
    const { port, bridge } = await bridgeWith(chrome);
    try {
      const a = await session(port);
      const b = await session(port);
      await a.call("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
      const a1 = (await a.call("Target.createTarget", { url: "about:blank" })).result.targetId;
      const a2 = (await a.call("Target.createTarget", { url: "about:blank" })).result.targetId;
      const b1 = (await b.call("Target.createTarget", { url: "about:blank" })).result.targetId;
      // A pop-up a's tab opens is a's too.
      await a.call("Runtime.evaluate", { expression: "window.open(\"https://apply.example.test/\")" }, `S-${a1}`);
      await until(() => a.events().some((e) => e.params?.targetInfo?.openerId === a1));
      const popup = a.events().find((e) => e.params?.targetInfo?.openerId === a1).params.targetInfo.targetId;
      const closed = () => chrome.sent.filter((m) => m.method === "Target.closeTarget").map((m) => m.params.targetId);

      a.ws.close(); // ends cleanly
      await until(() => [a1, a2, popup].every((t) => closed().includes(t)));
      assert.ok(!closed().includes(b1) && !closed().includes("OWNER") && !closed().includes("T1"), "only what a opened");
      b.ws.close(); // and b's process is gone without a word: its socket simply ends
      await until(() => closed().includes(b1) && closed().includes("T1"));
      assert.ok(!closed().includes("OWNER"), "the owner's tab is never closed");
      assert.deepEqual([...chrome.live], ["OWNER"], "nothing is left open but the owner's");
    } finally {
      await bridge.stop();
    }
  });

  it("a window whose first tab stops opening tabs gets a fresh first tab after three tries on it, and the session's tab still opens (2026-10-17, 2026-10-29)", async () => {
    const chrome = fakeChrome({ noOpenFrom: new Set(["T1"]) }); // T1, the first tab, gives no tab after hours minimized
    const lines = [];
    const { port, bridge } = await bridgeWith(chrome, { log: (l) => lines.push(l), openRetryMs: 1 });
    try {
      const a = await session(port);
      const opened = await a.call("Target.createTarget", { url: "https://jobs.example.test/" });
      assert.ok(opened.result?.targetId, `the tab opened: ${JSON.stringify(opened)}`);
      const anchors = chrome.sent.filter((m) => m.method === "Target.createTarget" && m.params.url === ANCHOR_URL);
      assert.equal(anchors.length, 2, "the first tab was opened again");
      assert.equal(anchors[1].params.newWindow, true, "in a window of its own");
      assert.equal(anchors[1].params.background, true, "opened in the background");
      assert.ok(!(chrome.bounds || []).some((m) => m.bounds.windowState === "minimized"), "no window of it is minimized (2026-11-04)");
      await until(() => chrome.sent.some((m) => m.method === "Target.closeTarget" && m.params.targetId === "T1"));
      assert.ok(lines.some((l) => /stopped opening tabs \(window\.open gave no tab\)/.test(l)), "why is logged");
      assert.equal(chrome.order.filter((o) => o === "open@T1").length, 3, "three tries on the same first tab before it was replaced");
      assert.ok(lines.some((l) => /try 3 of 3; the opener: /.test(l)), "what the opener looked like is logged");
      const later = await a.call("Target.createTarget", { url: "https://jobs.example.test/2" });
      assert.ok(later.result?.targetId, "the next opens from the fresh one");
      assert.equal(chrome.sent.filter((m) => m.method === "Target.createTarget" && m.params.url === ANCHOR_URL).length, 2);
    } finally {
      await bridge.stop();
    }
  });

  it("Chrome the app starts is worked in on screen; Chrome already open is left alone: the app opens a window of its own, in the normal state on screen (2026-11-04)", async () => {
    for (const started of [true, false]) {
      const chrome = fakeChrome();
      chrome.live.add("BLANK");
      chrome.info.BLANK = { title: "", url: "about:blank" };
      let running = !started;
      const port = await freePort();
      const bridge = ownChromeBridge({ key: KEY,
        port, mode: "profile", idleCloseMs: 0, WebSocketImpl: chrome.WebSocketImpl,
        state: () => ({ running, endpoint: running ? "ws://127.0.0.1:1/x" : "" }), launch: () => { running = true; },
      });
      await bridge.start();
      try {
        const s1 = await session(port);
        await s1.call("Target.createTarget", { url: "https://jobs.example.test/1" });
        await new Promise((r) => setTimeout(r, 100));
        const anchor = chrome.sent.find((m) => m.method === "Target.createTarget" && m.params.url === ANCHOR_URL);
        assert.equal(anchor.params.newWindow, !started, `started=${started}: the anchor's own window`);
        assert.equal(anchor.params.background, true, `started=${started}: opened in the background`);
        const asks = (chrome.bounds || []).map((m) => m.bounds);
        assert.equal(asks.some((b) => b.windowState === "minimized"), false, `started=${started}: never minimized`);
        // The first window of Chrome the app started is on screen already: left as it is. A window of its own is put on screen, once.
        assert.deepEqual(asks, started ? [] : [{ windowState: "normal", ...ON_SCREEN }], `started=${started}: the window's placement`);
        assert.equal(chrome.live.has("BLANK"), !started, started ? "the blank tab in the app's window is closed" : "a blank tab beside a page in a window not the app's stays");
        assert.equal(chrome.live.has("OWNER"), true, "the owner's tab stays");
        assert.equal(chrome.sent.some((m) => m.method === "Page.bringToFront"), false);
        s1.ws.close();
      } finally {
        await bridge.stop();
      }
    }
    assert.ok(profileChromeArgs("/p").includes("--disable-renderer-backgrounding"));
  });

  it("closes the resumebot tab an earlier bridge left when it connects, and nothing else", async () => {
    const chrome = fakeChrome({ leftover: true });
    const { port, bridge } = await bridgeWith(chrome);
    try {
      await session(port);
      await until(() => !chrome.live.has("OLD"));
      assert.ok(chrome.live.has("OWNER"), "the owner's tab stays");
    } finally {
      await bridge.stop();
    }
  });

  // The profile's Chrome held one blank tab; the sweep closed it, Chrome ended, the next
  // request started it again, and round it went, so every page the pre-check looked at was unknown.
  const profileBridge = async (chrome, extra = {}) => {
    const box = { running: false, launched: 0 }; // the bridge starts this Chrome, so its blank tabs are the app's to sweep
    const port = await freePort();
    const bridge = ownChromeBridge({ key: KEY,
      port, mode: "profile", idleCloseMs: 0, WebSocketImpl: chrome.WebSocketImpl, ...extra,
      state: () => ({ running: box.running, endpoint: box.running ? "ws://127.0.0.1:1/x" : "" }),
      launch: () => { box.launched++; box.running = true; },
    });
    await bridge.start();
    return { port, bridge, box };
  };

  it("keeps a tab of its own in the profile's Chrome, so the blank-tab sweep never closes Chrome's last tab", async () => {
    let box;
    const chrome = fakeChrome({ tabs: ["BLANK"], endsWhenEmpty: true, onEnd: () => { box.running = false; } });
    const made = await profileBridge(chrome);
    box = made.box;
    try {
      assert.equal((await fetch(`http://127.0.0.1:${made.port}/json/version`)).status, 200);
      await until(() => !chrome.live.has("BLANK"));
      assert.equal(chrome.ended, undefined, "Chrome never lost its last tab");
      assert.equal(chrome.live.size, 1, "one tab left: the bridge's");
      const anchorAt = chrome.sent.findIndex((m) => m.method === "Target.createTarget" && m.params.url === ANCHOR_URL);
      const closeAt = chrome.sent.findIndex((m) => m.method === "Target.closeTarget");
      assert.ok(anchorAt >= 0 && anchorAt < closeAt, "the bridge's tab is opened before anything is closed");
      const s = await session(made.port);
      await s.call("Target.createTarget", { url: "about:blank" });
      s.ws.close();
      await new Promise((r) => setTimeout(r, 2300)); // past the sweep after the last session
      assert.equal(chrome.ended, undefined);
      assert.equal(chrome.live.size, 1, "the session's tab closed, the bridge's stays");
    } finally {
      await made.bridge.stop();
    }
  });

  it("does not start Chrome again and again: one start, one connection, however often it is asked", async () => {
    let box;
    const chrome = fakeChrome({ tabs: ["BLANK"], endsWhenEmpty: true, onEnd: () => { box.running = false; } });
    const made = await profileBridge(chrome);
    box = made.box;
    try {
      for (let i = 0; i < 6; i++) {
        assert.equal((await fetch(`http://127.0.0.1:${made.port}/json/version`)).status, 200);
        await new Promise((r) => setTimeout(r, 100));
      }
      assert.equal(box.launched, 1, "started once, then left open");
      assert.equal(chrome.sockets.length, 1, "one connection to Chrome");
      assert.equal(made.bridge.connected, true);
    } finally {
      await made.bridge.stop();
    }
  });

  it("opens a tab for a session that never turns discovery on (the app's own code), straight at its address", async () => {
    const chrome = fakeChrome();
    const made = await profileBridge(chrome);
    try {
      const s = await session(made.port);
      const r = await s.call("Target.createTarget", { url: "https://jobs.example.test/9" });
      assert.ok(r.result?.targetId, JSON.stringify(r));
      assert.equal(chrome.info[r.result.targetId].url, "https://jobs.example.test/9");
      assert.ok((await s.call("Target.attachToTarget", { targetId: r.result.targetId, flatten: true })).result?.sessionId);
    } finally {
      await made.bridge.stop();
    }
  });

  it("Go's window: visible and its own, its tabs opened from its first, handed to the owner and never touched again", async () => {
    const chrome = fakeChrome();
    const made = await profileBridge(chrome, { idleCloseMs: 150 });
    try {
      const go = await session(made.port);
      const first = (await go.call("Target.createTarget", { url: "https://jobs.example.test/a", resumebotWindow: true })).result.targetId;
      const ask = chrome.sent.find((m) => m.method === "Target.createTarget" && m.params.url === "https://jobs.example.test/a");
      assert.equal(ask.params.newWindow, true, "a window of its own");
      const win = chrome.windows[first];
      const sid = (await go.call("Target.attachToTarget", { targetId: first, flatten: true })).result.sessionId;
      const second = (await go.call("Target.createTarget", { url: "https://jobs.example.test/b", resumebotFrom: { targetId: first, sessionId: sid } })).result.targetId;
      assert.equal(chrome.windows[second], win, "in Go's window");
      assert.notEqual(chrome.windowState[win], "minimized", "Go's window is never minimized");
      const other = await session(made.port);
      assert.ok((await other.call("Target.createTarget", { url: "https://x.test/", resumebotFrom: { targetId: first, sessionId: sid } })).error, "no other session opens from Go's tabs");
      assert.equal((await go.call("Target.resumebotKeep")).result.kept, 2);
      go.ws.close();
      other.ws.close();
      await new Promise((r) => setTimeout(r, 2600)); // the sweep and the idle time pass
      assert.ok(chrome.live.has(first) && chrome.live.has(second), "the owner's tabs stay open");
      assert.ok(!chrome.sent.some((m) => m.method === "Browser.close"), "Chrome is not closed under the owner's window");
      const third = await session(made.port);
      assert.ok((await third.call("Target.closeTarget", { targetId: first })).error, "no session closes them");
      assert.ok((await third.call("Target.attachToTarget", { targetId: second, flatten: true })).error, "or attaches to them");
    } finally {
      await made.bridge.stop();
    }
  });
});

describe("window placement stays on-screen (2026-10-20)", () => {
  const profileBridge = async (chrome, extra = {}) => {
    const port = await freePort();
    const bridge = ownChromeBridge({ key: KEY,
      port, mode: "profile", idleCloseMs: 0, WebSocketImpl: chrome.WebSocketImpl, ...extra,
      state: () => ({ running: true, endpoint: "ws://127.0.0.1:1/x" }), launch: () => {},
    });
    await bridge.start();
    return { port, bridge };
  };
  const onScreen = (b) => b.left >= 0 && b.top >= 0 && b.width > 300 && b.height > 300;
  it("Go's window gets explicit on-screen normal bounds right after it is created", async () => {
    const chrome = fakeChrome();
    const made = await profileBridge(chrome, { idleCloseMs: 150 });
    try {
      const go = await session(made.port);
      const first = (await go.call("Target.createTarget", { url: "https://jobs.example.test/a", resumebotWindow: true })).result.targetId;
      await until(() => (chrome.bounds || []).some((m) => m.windowId === chrome.windows[first]));
      const set = chrome.bounds.find((m) => m.windowId === chrome.windows[first]);
      assert.equal(set.bounds.windowState, "normal");
      assert.ok(onScreen(set.bounds));
      go.ws.close();
    } finally { await made.bridge.stop(); }
  });
  // The browser jobs' window stays on screen (the owner, 2026-11-04). It stays visible: it is never
  // minimized or moved off screen, and is never raised or focused. This replaces the 2026-11-03 tests that pinned hiding it
  // (it stays minimized, 2026-10-16): a window is only ever put in the normal state at on-screen bounds.
  const ON_SCREEN_ASK = { windowState: "normal", ...ON_SCREEN };
  const asked = (chrome) => (chrome.bounds || []).map((m) => m.bounds);
  const raised = (chrome) => chrome.sent.filter((m) => m.method === "Target.activateTarget" || m.method === "Page.bringToFront");
  // A Chrome the bridge starts: the window its first tab goes in is Chrome's own first window, not one the bridge makes.
  const startedBridge = async (chrome, extra = {}) => {
    const box = { running: false };
    const port = await freePort();
    const bridge = ownChromeBridge({ key: KEY,
      port, mode: "profile", idleCloseMs: 0, WebSocketImpl: chrome.WebSocketImpl, ...extra,
      state: () => ({ running: box.running, endpoint: box.running ? "ws://127.0.0.1:1/x" : "" }), launch: () => { box.running = true; },
    });
    await bridge.start();
    return { port, bridge };
  };
  it("no minimize call is made: a session's tabs ask nothing of the window, and no ask is ever for the minimized state", async () => {
    const chrome = fakeChrome();
    const made = await profileBridge(chrome, { idleCloseMs: 150 });
    try {
      const a = await session(made.port);
      await a.call("Target.createTarget", { url: "https://x.test/1" });
      const afterFirst = asked(chrome).length;
      await a.call("Target.createTarget", { url: "https://x.test/2" });
      await a.call("Target.createTarget", { url: "https://x.test/3" });
      assert.equal(afterFirst, 1, "the window the bridge made is put on screen once");
      assert.deepEqual(asked(chrome), [ON_SCREEN_ASK], "and no tab asks anything of it again");
      assert.equal(chrome.sent.some((m) => m.method === "Browser.setWindowBounds" && JSON.stringify(m.params).includes("minimized")), false);
      a.ws.close();
    } finally { await made.bridge.stop(); }
  });
  it("a window found minimized when the bridge attaches is put in the normal state at on-screen bounds once, and nothing focuses it", async () => {
    const lines = [];
    const chrome = fakeChrome({ startMinimized: true });
    const made = await startedBridge(chrome, { log: (l) => lines.push(l) });
    try {
      const a = await session(made.port);
      await a.call("Target.createTarget", { url: "https://x.test/1" });
      await a.call("Target.createTarget", { url: "https://x.test/2" });
      // Chrome restores a minimized window and leaves the bounds of that same ask out, so the restore goes first and the
      // bounds in a second ask: the window is restored once, and set at the on-screen bounds once.
      assert.deepEqual(asked(chrome), [{ windowState: "normal" }, ON_SCREEN_ASK], "restored, then at the on-screen bounds");
      assert.deepEqual(Object.values(chrome.windowState), ["normal"]);
      assert.deepEqual(Object.values(chrome.placement), [ON_SCREEN], "and it is at the on-screen bounds");
      assert.deepEqual(raised(chrome), [], "not activated, not brought to the front");
      assert.ok(lines.some((l) => /on screen/.test(l)), "said in the log, once");
      assert.equal(lines.filter((l) => /on screen/.test(l)).length, 1);
      a.ws.close();
    } finally { await made.bridge.stop(); }
  });
  it("a window found minimized AND parked at -32000 (the owner's window after weeks minimized) comes back on screen, not where it was parked", async () => {
    const chrome = fakeChrome({ startMinimized: true, offScreen: true });
    const made = await startedBridge(chrome);
    try {
      const a = await session(made.port);
      await a.call("Target.createTarget", { url: "https://x.test/1" });
      assert.deepEqual(Object.values(chrome.windowState), ["normal"]);
      assert.deepEqual(Object.values(chrome.placement), [ON_SCREEN], "a restore alone would have brought it back at -32000");
      assert.deepEqual(raised(chrome), []);
      a.ws.close();
    } finally { await made.bridge.stop(); }
  });
  it("a minimized window the bridge did not start is restored and put on screen the same way, once, in the owner's own Chrome", async () => {
    const chrome = fakeChrome({ startMinimized: true, offScreen: true });
    const { port, bridge } = await bridgeWith(chrome);
    try {
      const a = await session(port);
      await a.call("Target.createTarget", { url: "https://x.test/1" });
      await a.call("Target.createTarget", { url: "https://x.test/2" });
      assert.deepEqual(Object.values(chrome.windowState), ["normal"]);
      assert.deepEqual(Object.values(chrome.placement), [ON_SCREEN]);
      assert.equal(asked(chrome).filter((b) => b.windowState === "normal" && b.left === undefined).length, 1, "restored once");
      assert.equal(asked(chrome).filter((b) => b.left === ON_SCREEN.left).length, 1, "bounds set once");
      assert.deepEqual(raised(chrome), []);
      a.ws.close();
    } finally { await bridge.stop(); }
  });
  it("a window found off screen (-32000, where Windows records a minimized one) is put on screen once, the same way", async () => {
    const chrome = fakeChrome({ offScreen: true });
    const made = await startedBridge(chrome);
    try {
      const a = await session(made.port);
      await a.call("Target.createTarget", { url: "https://x.test/1" });
      await a.call("Target.createTarget", { url: "https://x.test/2" });
      assert.deepEqual(asked(chrome), [ON_SCREEN_ASK]);
      assert.deepEqual(Object.values(chrome.placement), [ON_SCREEN]);
      assert.deepEqual(raised(chrome), []);
      a.ws.close();
    } finally { await made.bridge.stop(); }
  });
  it("in the owner's own Chrome the bridge's window opens in the background, in the normal state at on-screen bounds, once", async () => {
    const chrome = fakeChrome();
    const { port, bridge } = await bridgeWith(chrome);
    try {
      const a = await session(port);
      await a.call("Target.createTarget", { url: "https://x.test/1" });
      await a.call("Target.createTarget", { url: "https://x.test/2" });
      const made = chrome.sent.find((m) => m.method === "Target.createTarget" && m.params.url === ANCHOR_URL);
      assert.equal(made.params.newWindow, true, "a window of its own");
      assert.equal(made.params.background, true, "not raised");
      assert.deepEqual(asked(chrome), [ON_SCREEN_ASK]);
      assert.deepEqual(raised(chrome), []);
      a.ws.close();
    } finally { await bridge.stop(); }
  });
  it("a profile whose saved window placement is off-screen is reset before Chrome starts; an on-screen one is left", () => {
    const dir = mkdtempSync(join(tmpdir(), "rb-prof-"));
    mkdirSync(join(dir, "Default"));
    const file = join(dir, "Default", "Preferences");
    writeFileSync(file, JSON.stringify({ other: 1, browser: { window_placement: { left: -32000, top: -32000, right: -31000, bottom: -31200, maximized: false } } }));
    resetWindowPlacement(dir);
    const p = JSON.parse(readFileSync(file, "utf8"));
    assert.equal(p.other, 1);
    assert.ok(p.browser.window_placement.left >= 0 && p.browser.window_placement.top >= 0 && p.browser.window_placement.right > p.browser.window_placement.left);
    const good = JSON.stringify({ browser: { window_placement: { left: 10, top: 20, right: 900, bottom: 700 } } });
    writeFileSync(file, good);
    resetWindowPlacement(dir);
    assert.equal(readFileSync(file, "utf8"), good);
    resetWindowPlacement(join(dir, "missing")); // no profile yet: nothing happens
  });
});

// The tab register (docs/PRD.md path 52): the bridge tells the app whose each tab is, and the app names the tabs to close.
describe("the tab register (2026-10-29)", () => {
  const profileBridge = async (chrome, extra = {}) => {
    const port = await freePort();
    const bridge = ownChromeBridge({ key: KEY,
      port, mode: "profile", idleCloseMs: 0, WebSocketImpl: chrome.WebSocketImpl,
      state: () => ({ running: true, endpoint: "ws://127.0.0.1:1/x" }), launch: () => {}, ...extra,
    });
    await bridge.start();
    return { port, bridge };
  };
  const post = (port, path, body) => fetch(`http://127.0.0.1:${port}${path}`, { method: "POST", body: JSON.stringify(body) }).then((r) => r.json());

  it("knows which addresses are the app's own code, which registers its own tabs", () => {
    assert.equal(ownsItsTabs("go"), true);
    assert.equal(ownsItsTabs("code.board"), true);
    assert.equal(ownsItsTabs("reader-ab12"), false);
    assert.equal(NO_REGISTER.reconcile instanceof Function, true);
  });

  it("tells the app whose each session tab is (the name in the address), and nothing of the app's own code's tabs", async () => {
    const chrome = fakeChrome();
    const register = fakeRegister();
    const { port, bridge } = await bridgeWith(chrome, { register });
    try {
      const reader = await session(port, "reader-ab12");
      await reader.call("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
      const code = await session(port, "code.board");
      const r1 = (await reader.call("Target.createTarget", { url: "about:blank" })).result.targetId;
      await code.call("Target.createTarget", { url: "about:blank" });
      await until(() => register.told.opened.length === 1);
      assert.deepEqual(register.told.opened, [[r1, "reader-ab12", "session"]], "the bridge's own first tab and the code job's are not told");
      // a pop-up of a session's tab is that session's too
      await reader.call("Runtime.evaluate", { expression: "window.open(\"https://apply.example.test/\")" }, `S-${r1}`);
      await until(() => register.told.opened.length === 2);
      assert.equal(register.told.opened[1][1], "reader-ab12");
    } finally { await bridge.stop(); }
  });

  it("names a connection that gives no name of its own, and two are told apart", async () => {
    const chrome = fakeChrome();
    const register = fakeRegister();
    const { port, bridge } = await bridgeWith(chrome, { register });
    try {
      const a = await session(port);
      const b = await session(port);
      await a.call("Target.createTarget", { url: "about:blank" });
      await b.call("Target.createTarget", { url: "about:blank" });
      await until(() => register.told.opened.length === 2);
      assert.notEqual(register.told.opened[0][1], register.told.opened[1][1]);
      assert.match(register.told.opened[0][1], /^conn-/);
    } finally { await bridge.stop(); }
  });

  it("tells the app a tab is gone when Chrome says so, and releases a session's rows when its connection ends", async () => {
    const chrome = fakeChrome({ announceDestroyed: true });
    const register = fakeRegister();
    const { port, bridge } = await bridgeWith(chrome, { register });
    try {
      const reader = await session(port, "reader-ab12");
      await reader.call("Target.setDiscoverTargets", { discover: true });
      const t1 = (await reader.call("Target.createTarget", { url: "about:blank" })).result.targetId;
      await reader.call("Target.closeTarget", { targetId: t1 });
      await until(() => register.told.removed.includes(t1));
      reader.ws.close();
      await until(() => register.told.released.includes("reader-ab12"));
    } finally { await bridge.stop(); }
  });

  it("the app's own code ending takes its tabs off the register one by one and closes them", async () => {
    const chrome = fakeChrome();
    const register = fakeRegister();
    const { port, bridge } = await bridgeWith(chrome, { register });
    try {
      const code = await session(port, "code.read");
      const t1 = (await code.call("Target.createTarget", { url: "about:blank" })).result.targetId;
      code.ws.close();
      await until(() => register.told.removed.includes(t1));
      await until(() => !chrome.live.has(t1));
      assert.deepEqual(register.told.released, [], "no owner id of a code job is released as a session");
    } finally { await bridge.stop(); }
  });

  it("closes the tabs the app names, never the bridge's own first tab, and says how many", async () => {
    const chrome = fakeChrome();
    const { port, bridge } = await bridgeWith(chrome);
    try {
      const a = await session(port, "reader-1");
      const t1 = (await a.call("Target.createTarget", { url: "about:blank" })).result.targetId;
      const t2 = (await a.call("Target.createTarget", { url: "about:blank" })).result.targetId;
      const said = await post(port, "/tabs/close", { targets: [t1, "T1", "NOPE", 7] });
      assert.equal(said.closed, 2, "the one asked for and the one that is not there (Chrome answers success for it); not the first tab");
      assert.ok(!chrome.live.has(t1) && chrome.live.has(t2) && chrome.live.has("T1"));
      assert.deepEqual(await post(port, "/tabs/close", { targets: "nope" }), { closed: 0 });
    } finally { await bridge.stop(); }
  });

  it("refuses the control addresses to a web page, and a body that is not JSON", async () => {
    const chrome = fakeChrome();
    const { port, bridge } = await bridgeWith(chrome);
    try {
      const page = await fetch(`http://127.0.0.1:${port}/tabs/close`, { method: "POST", body: "{}", headers: { Origin: "https://evil.example" } });
      assert.equal(page.status, 403);
      const odd = await fetch(`http://127.0.0.1:${port}/tabs/reconcile`, { method: "POST", body: "{not json" });
      assert.equal(odd.status, 400);
    } finally { await bridge.stop(); }
  });

  it("reconcile closes every page of the app's profile the register does not hold, keeps its first tab, the held tabs and a connected session's, and names the held ones that are gone", async () => {
    const chrome = fakeChrome({ tabs: ["OWNER", "KEEP", "OLD1", "OLD2"] });
    for (const t of ["KEEP", "OLD1", "OLD2"]) chrome.info[t] = { title: "", url: "https://x.test/" };
    const { port, bridge } = await profileBridge(chrome);
    try {
      const a = await session(port, "reader-1");
      const live = (await a.call("Target.createTarget", { url: "about:blank" })).result.targetId;
      const said = await post(port, "/tabs/reconcile", { keep: ["KEEP", "MISSING"] });
      assert.deepEqual(said.gone, ["MISSING"]);
      await until(() => !chrome.live.has("OLD1") && !chrome.live.has("OLD2"));
      assert.ok(chrome.live.has("KEEP") && chrome.live.has(live) && chrome.live.has("T1"), "the held tab, the session's tab and the first tab stay");
      assert.ok(!chrome.live.has("OWNER"), "a tab in the app's own profile that the register does not hold is closed: the profile is the app's");
      // a tab kept from before is the owner's now: no session drives or closes it
      const refused = await a.call("Target.closeTarget", { targetId: "KEEP" });
      assert.ok(refused.error);
    } finally { await bridge.stop(); }
  });

  it("reconcile never sweeps the owner's own Chrome", async () => {
    const chrome = fakeChrome();
    const { port, bridge } = await bridgeWith(chrome); // mode own
    try {
      await session(port, "reader-1");
      const said = await post(port, "/tabs/reconcile", { keep: [] });
      assert.equal(said.closed, 0);
      assert.match(said.skipped, /never swept/);
      assert.ok(chrome.live.has("OWNER"));
    } finally { await bridge.stop(); }
  });

  it("on connecting to the app's profile it asks the app what to keep and closes the rest; the app not answering closes nothing", async () => {
    for (const answer of [["KEEP"], null]) {
      const chrome = fakeChrome({ tabs: ["OWNER", "KEEP", "OLD"] });
      for (const t of ["KEEP", "OLD"]) chrome.info[t] = { title: "", url: "https://x.test/" };
      const register = fakeRegister({ keep: answer });
      const lines = [];
      const { port, bridge } = await profileBridge(chrome, { register, log: (l) => lines.push(l) });
      try {
        await session(port, "reader-1");
        await until(() => register.told.reconciles === 1);
        if (answer) {
          await until(() => !chrome.live.has("OLD") && !chrome.live.has("OWNER"));
          assert.ok(chrome.live.has("KEEP"));
        } else {
          await new Promise((r) => setTimeout(r, 100));
          assert.ok(chrome.live.has("OLD") && chrome.live.has("OWNER") && chrome.live.has("KEEP"), "nothing is closed without the register's word");
          assert.ok(lines.some((l) => /did not say which tabs to keep/.test(l)));
        }
      } finally { await bridge.stop(); }
    }
  });

  it("opens the tabs of a burst one at a time: each tab is seen before the next is asked for", async () => {
    const chrome = fakeChrome({ popupDelayMs: 25 });
    const { port, bridge } = await bridgeWith(chrome);
    try {
      const sessions = [await session(port, "a"), await session(port, "b"), await session(port, "c")];
      const made = await Promise.all(sessions.map((s) => s.call("Target.createTarget", { url: "about:blank" })));
      assert.ok(made.every((m) => m.result?.targetId));
      assert.deepEqual(chrome.order.filter((o) => o === "created" || o.startsWith("open@")).map((o) => o.split("@")[0]),
        ["open", "created", "open", "created", "open", "created"]);
    } finally { await bridge.stop(); }
  });

  it("a tab that gives none is tried again on the same first tab, and the first tab is not replaced when a later try works", async () => {
    const chrome = fakeChrome({ failOpens: 2 });
    const lines = [];
    const { port, bridge } = await bridgeWith(chrome, { log: (l) => lines.push(l), openRetryMs: 1 });
    try {
      const a = await session(port, "a");
      const opened = await a.call("Target.createTarget", { url: "https://jobs.example.test/" });
      assert.ok(opened.result?.targetId);
      assert.equal(chrome.sent.filter((m) => m.method === "Target.createTarget" && m.params.url === ANCHOR_URL).length, 1, "no second first tab");
      assert.equal(lines.filter((l) => /a tab did not open/.test(l)).length, 2);
      assert.ok(!lines.some((l) => /stopped opening tabs/.test(l)));
    } finally { await bridge.stop(); }
  });

  it("one open Chrome never answers does not stop every open after it: the asks are answered and the next tab opens (2026-10-30)", async () => {
    const chrome = fakeChrome();
    chrome.hang = true; // the first tab's window.open is never answered
    const lines = [];
    const { port, bridge } = await bridgeWith(chrome, { log: (l) => lines.push(l), openMs: 60, internalMs: 80, openRetryMs: 1 });
    try {
      const a = await session(port, "a");
      const first = await a.call("Target.createTarget", { url: "https://jobs.example.test/1" });
      assert.ok(first.error, "an open Chrome does not answer is answered with an error, not left");
      chrome.hang = false;
      const next = await Promise.race([
        a.call("Target.createTarget", { url: "https://jobs.example.test/2" }),
        new Promise((r) => setTimeout(() => r({ hung: true }), 3000)),
      ]);
      assert.ok(next.result?.targetId, `the next tab opens: ${JSON.stringify(next)}`);
      assert.ok(lines.some((l) => /did not answer/.test(l)), "that Chrome did not answer is logged");
    } finally { await bridge.stop(); }
  });

  it("a tab Chrome shows before the opener answers is enough: the next open does not wait for that answer (2026-11-03)", async () => {
    const chrome = fakeChrome({ silentOpen: true }); // the tab shows, the evaluate that asked for it is never answered
    const { port, bridge } = await bridgeWith(chrome, { openMs: 60_000, internalMs: 60_000 }); // only the tab being seen can move the opens on
    try {
      const a = await session(port, "a");
      const b = await session(port, "b");
      const asks = [a.call("Target.createTarget", { url: "https://jobs.example.test/a" }), b.call("Target.createTarget", { url: "https://jobs.example.test/b" })];
      await until(() => chrome.order.filter((o) => o.startsWith("open@")).length === 2, 1000);
      const [first, second] = await Promise.all(asks);
      assert.equal(chrome.info[first.result.targetId].url, "https://jobs.example.test/a", "the first session's tab");
      assert.equal(chrome.info[second.result.targetId].url, "https://jobs.example.test/b", "the second session's own tab");
      assert.notEqual(first.result.targetId, second.result.targetId);
    } finally { await bridge.stop(); }
  });

  // Chrome pauses the tabs a page opens while any DevTools connection has auto-attach on (the bridge has one connection, and a
  // session's auto-attach is on it), and the opener gives no tab until each is resumed. A session resumes its own tabs; the app's
  // own code does not (it asks for no auto-attach and ignores events), so its first tab stayed paused and the first tab of the
  // window gave no tab to anyone.
  it("resumes a tab Chrome paused for the debugger when it is the app's own code's, which never does, so the next open still gives a tab (2026-11-05)", async () => {
    const chrome = fakeChrome({ pausePopups: true });
    const lines = [];
    const { port, bridge } = await bridgeWith(chrome, { log: (l) => lines.push(l), openRetryMs: 1 });
    try {
      const reader = await session(port, "reader-ab12");
      await reader.call("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
      const code = await session(port, "code.board");
      const first = await code.call("Target.createTarget", { url: "https://jobs.example.test/1" });
      const second = await code.call("Target.createTarget", { url: "https://jobs.example.test/2" });
      assert.ok(first.result?.targetId, `the first tab opened: ${JSON.stringify(first)}`);
      assert.ok(second.result?.targetId, `the second tab opened: ${JSON.stringify(second)}`);
      assert.ok(!lines.some((l) => /a tab did not open/.test(l)), lines.join("; "));
      assert.equal(chrome.sent.filter((m) => m.method === "Target.createTarget").length, 1, "the window's first tab was never replaced");
      for (const t of [first.result.targetId, second.result.targetId]) {
        assert.ok(chrome.sent.some((m) => m.method === "Runtime.runIfWaitingForDebugger" && m.sessionId === `S-${t}`), `${t} was resumed`);
      }
      assert.equal(lines.filter((l) => /resumed a tab of code.board/.test(l)).length, 2, "each is said once");
      assert.deepEqual(chrome.paused, {}, "nothing is left paused");
    } finally { await bridge.stop(); }
  });

  it("leaves a session's own paused tab to the session, which resumes it itself (2026-11-05)", async () => {
    const chrome = fakeChrome({ pausePopups: true });
    const { port, bridge } = await bridgeWith(chrome);
    try {
      const reader = await session(port, "reader-ab12");
      await reader.call("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
      const tab = (await reader.call("Target.createTarget", { url: "https://jobs.example.test/1" })).result.targetId;
      await until(() => reader.events().some((e) => e.method === "Target.attachedToTarget" && e.params.targetInfo.targetId === tab));
      assert.equal(reader.events().find((e) => e.method === "Target.attachedToTarget" && e.params.targetInfo.targetId === tab).params.waitingForDebugger, true,
        "the session is told it is paused, and is left to resume it");
      await new Promise((r) => setTimeout(r, 60));
      assert.ok(!chrome.sent.some((m) => m.method === "Runtime.runIfWaitingForDebugger" && m.sessionId === `S-${tab}`), "the bridge does not resume it");
      assert.ok((await reader.call("Runtime.runIfWaitingForDebugger", {}, `S-${tab}`)).result, "the session's own resume goes through");
      assert.deepEqual(chrome.paused, {});
    } finally { await bridge.stop(); }
  });

  it("says in the log, when a window.open gives no tab, which tabs Chrome is holding paused for the debugger and for how long (2026-11-05)", async () => {
    const chrome = fakeChrome({ pausePopups: true, failOpens: 1 });
    const lines = [];
    const { port, bridge } = await bridgeWith(chrome, { log: (l) => lines.push(l), openRetryMs: 1 });
    try {
      const reader = await session(port, "reader-ab12"); // a session resumes its own tabs; this one does not, so its tab stays paused
      await reader.call("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
      const first = await reader.call("Target.createTarget", { url: "https://jobs.example.test/1" }); // one try gives no tab, with nothing paused
      assert.ok(first.result?.targetId);
      const none = lines.find((l) => /a tab did not open/.test(l));
      assert.match(none, /waiting for the debugger: none\)/, none);
      await reader.call("Target.createTarget", { url: "https://jobs.example.test/2" }); // its opener is paused now: no tab
      const held = lines.find((l) => /a tab did not open/.test(l) && l !== none);
      assert.match(held, new RegExp(`waiting for the debugger: ${first.result.targetId} of reader-ab12 for \\d+ s\\)`), held);
    } finally { await bridge.stop(); }
  });

  // A pop-up of `opener`, a tab of `reader`, that Chrome paused for the debugger: said to the bridge on its one connection, and
  // waited for until it reaches the session, so the bridge has noted it by then.
  const popupPaused = async (chrome, reader, id, opener) => {
    chrome.sockets[0].emit({ method: "Target.attachedToTarget", params: { sessionId: `S-${id}`, targetInfo: { targetId: id, type: "page", openerId: opener }, waitingForDebugger: true } });
    await until(() => reader.events().some((e) => e.method === "Target.attachedToTarget" && e.params.sessionId === `S-${id}`));
  };

  it("names at most the oldest five held tabs in that line, then how many more (2026-11-05)", async () => {
    const chrome = fakeChrome({ pausePopups: true });
    const lines = [];
    const { port, bridge } = await bridgeWith(chrome, { log: (l) => lines.push(l), openRetryMs: 1 });
    try {
      const reader = await session(port, "reader-ab12"); // it does not resume its tabs, so each stays held
      await reader.call("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
      const first = (await reader.call("Target.createTarget", { url: "https://jobs.example.test/1" })).result.targetId;
      const held = [first];
      for (let n = 2; n <= 7; n++) { held.push(`P${n}`); await popupPaused(chrome, reader, `P${n}`, first); }
      await reader.call("Target.createTarget", { url: "https://jobs.example.test/2" }); // its opener is paused: no tab
      const line = lines.find((l) => /a tab did not open/.test(l));
      assert.deepEqual([...line.matchAll(/(\S+) of reader-ab12 for \d+ s/g)].map((m) => m[1]), held.slice(0, 5), line);
      assert.match(line, /for \d+ s, and 2 more\)$/, line);
    } finally { await bridge.stop(); }
  });

  it("a held tab that is closed or detached is no longer named: the next failure line says none (2026-11-05)", async () => {
    const chrome = fakeChrome({ pausePopups: true, announceDestroyed: true });
    const lines = [];
    const { port, bridge } = await bridgeWith(chrome, { log: (l) => lines.push(l), openRetryMs: 1 });
    try {
      const reader = await session(port, "reader-ab12");
      await reader.call("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });
      const a = (await reader.call("Target.createTarget", { url: "https://jobs.example.test/1" })).result.targetId;
      await popupPaused(chrome, reader, "P2", a);
      chrome.failOpens = 1; // one try from the first held tab gives no tab, with both held
      const c = (await reader.call("Target.createTarget", { url: "https://jobs.example.test/2", resumebotFrom: { targetId: a, sessionId: `S-${a}` } })).result.targetId;
      const named = lines.find((l) => /a tab did not open/.test(l));
      assert.match(named, new RegExp(`waiting for the debugger: ${a} of reader-ab12 for \\d+ s, P2 of reader-ab12 for \\d+ s\\)`), named);
      await reader.call("Target.closeTarget", { targetId: a });
      await reader.call("Target.closeTarget", { targetId: c });
      chrome.sockets[0].emit({ method: "Target.detachedFromTarget", params: { sessionId: "S-P2" } });
      await until(() => reader.events().filter((e) => e.method === "Target.targetDestroyed").length === 2 && reader.events().some((e) => e.method === "Target.detachedFromTarget"));
      lines.length = 0;
      chrome.failOpens = 1;
      assert.ok((await reader.call("Target.createTarget", { url: "https://jobs.example.test/3" })).result?.targetId);
      const none = lines.find((l) => /a tab did not open/.test(l));
      assert.match(none, /waiting for the debugger: none\)/, none);
    } finally { await bridge.stop(); }
  });

  it("Chrome going while a tab is being opened does not stop the tabs of the Chrome that comes after it (2026-10-30)", async () => {
    const chrome = fakeChrome();
    chrome.hang = true;
    const { port, bridge } = await bridgeWith(chrome, { openMs: 60_000, internalMs: 60_000 }); // only the loss can end the wait
    try {
      const a = await session(port, "a");
      void a.call("Target.createTarget", { url: "https://jobs.example.test/1" });
      await until(() => chrome.sent.some((m) => m.method === "Runtime.evaluate" && /window\.open\(/.test(m.params.expression)));
      chrome.sockets[0].close(); // Chrome went, the open unanswered
      await until(() => !bridge.connected);
      chrome.hang = false; // the Chrome that comes after it works
      const b = await session(port, "b");
      const next = await Promise.race([
        b.call("Target.createTarget", { url: "https://jobs.example.test/2" }),
        new Promise((r) => setTimeout(() => r({ hung: true }), 3000)),
      ]);
      assert.ok(next.result?.targetId, `the next Chrome's tab opens: ${JSON.stringify(next)}`);
    } finally { await bridge.stop(); }
  });
});

// The owner, 2026-11-04: a "resumebot" tab stayed open for as long as the app ran ("close it when the queue is empty"). The app's
// Chrome (SCOUT_BROWSER=profile) is started by the first browser job that asks and closed, with its first tab, once no session is
// connected, the app says no browser item waits or is leased, and no tab the owner should see is open, for the grace.
describe("Chrome only while the browser has work (2026-11-04)", () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  // The bridge starts this Chrome (`launch`), and it ends when its last tab closes, as Chrome does (`box.running` goes false).
  // `staleEndpoint`: a Chrome that closed its socket and still holds its lock leaves the file with its port behind (else Chrome
  // has removed it): the two ways a Chrome that is shutting down can look from the profile's folder.
  // `running`: a Chrome an earlier bridge left running (the app restarted), found when the bridge starts.
  const world = async ({ chromeOpts = {}, register = fakeRegister(), staleEndpoint = false, running = false, ...extra } = {}) => {
    const box = { running, launched: 0 };
    const chrome = fakeChrome({ tabs: [], endsWhenEmpty: true, onEnd: () => { box.running = false; }, ...chromeOpts });
    const port = await freePort();
    const lines = [];
    const bridge = ownChromeBridge({ key: KEY,
      port, mode: "profile", idleCloseMs: 150, endWaitMs: 400, register, log: (l) => lines.push(l), WebSocketImpl: chrome.WebSocketImpl,
      state: () => ({ running: box.running, endpoint: box.running && (!chrome.dead || staleEndpoint) ? "ws://127.0.0.1:1/x" : "" }),
      launch: () => { box.launched++; box.running = true; chrome.dead = false; }, ...extra,
    });
    await bridge.start();
    return { chrome, box, port, bridge, register, lines };
  };
  const anchors = (chrome) => chrome.sent.filter((m) => m.method === "Target.createTarget" && m.params.url === ANCHOR_URL);
  // A session that opens a tab and ends: the queue's last item done.
  const oneItem = async (w) => {
    const s = await session(w.port);
    assert.ok((await s.call("Target.createTarget", { url: "https://jobs.example.test/1" })).result?.targetId);
    s.ws.close();
  };

  it("names the grace: about two minutes, so a queue that refills at once does not start Chrome again", () => {
    assert.equal(IDLE_GRACE_MS, 120_000);
  });

  it("is not started with the bridge: with nothing asked for, no Chrome is started or connected, and the status does not start it", async () => {
    const w = await world();
    try {
      await sleep(400); // well past the grace
      assert.equal(w.box.launched, 0, "no Chrome started");
      assert.equal(w.chrome.sockets.length, 0, "none connected");
      assert.equal(w.bridge.connected, false);
      assert.equal((await (await fetch(`http://127.0.0.1:${w.port}/status`)).json()).why, "");
      assert.equal(w.box.launched, 0, "asking how it is does not start it");
      assert.equal(anchors(w.chrome).length, 0, "no first tab");
      assert.equal(w.register.told.asked, 0);
    } finally { await w.bridge.stop(); }
  });

  // The owner, 2026-11-04: the "resumebot" window sat on screen after the app started. A release restarts the app and its bridge,
  // and the Chrome the earlier bridge started goes on running; the idle check was only armed after a connection, which this
  // bridge never gets when nothing is queued (docs/PRD.md 52.9).
  it("a Chrome an earlier bridge left running is looked at when the bridge starts, and closed after the grace when nothing is queued", async () => {
    const w = await world({ running: true, chromeOpts: { leftover: true } });
    try {
      const left = Date.now();
      await until(() => w.chrome.ended === true);
      assert.ok(Date.now() - left >= 140, "not before the grace");
      assert.equal(w.box.launched, 0, "Chrome was found running, never started");
      assert.equal(w.chrome.live.has("OLD"), false, "the tab the earlier bridge left is gone, as for any connection");
      assert.ok(!w.chrome.sent.some((m) => m.method === "Browser.close"), "by its last tab closing, never a Browser.close");
      assert.ok(w.register.told.asked >= 1, "the app was asked what the queue holds");
      await until(() => !w.bridge.connected);
      assert.equal(w.box.running, false, "Chrome has ended");
    } finally { await w.bridge.stop(); }
  });

  // The app's own Chrome closes when its work is done (the owner, 2026-11-12). With the
  // bridge's first tab gone (the window was closed, or the bridge restarted since), `endChrome` had no tab to close and the idle
  // check never looked again. Only blank pages left: the app's own Chrome is ended, whole.
  const withoutFirstTab = async (extra = {}) => {
    const w = await world({ running: true, idleCloseMs: 300, register: fakeRegister({ keep: null }), chromeOpts: { tabs: ["BLANK"], announceDestroyed: true, ...extra } });
    await until(() => w.lines.some((l) => /found the app.s Chrome running: closing/.test(l)));
    w.chrome.live.delete("T1");
    w.chrome.sockets[0].emit({ method: "Target.targetDestroyed", params: { targetId: "T1" } });
    return w;
  };

  it("a Chrome with no first tab of the bridge's and nothing but blank pages is ended when the queue is empty (2026-11-12)", async () => {
    const w = await withoutFirstTab();
    try {
      await until(() => w.chrome.ended === true);
      assert.ok(w.chrome.sent.some((m) => m.method === "Browser.close"), "the whole Chrome is ended");
      assert.ok(w.lines.some((l) => /the browser queue is empty: closing the app's Chrome/.test(l)));
      await until(() => !w.bridge.connected);
      assert.equal(w.box.running, false, "Chrome has ended");
      const again = await session(w.port);
      assert.equal(w.box.launched, 1, "the next request starts it afresh");
      assert.ok((await again.call("Target.createTarget", { url: "https://jobs.example.test/2" })).result?.targetId);
    } finally { await w.bridge.stop(); }
  });

  it("a Chrome with no first tab of the bridge's but a page the owner should see is left open (2026-11-12)", async () => {
    const w = await withoutFirstTab();
    try {
      w.chrome.live.add("MAIL");
      w.chrome.info.MAIL = { title: "mail", url: "https://mail.example.test/" };
      await sleep(700);
      assert.equal(w.chrome.ended, undefined);
      assert.ok(!w.chrome.sent.some((m) => m.method === "Browser.close"));
    } finally { await w.bridge.stop(); }
  });

  it("a session that connects before the grace is over keeps a Chrome with no first tab (2026-11-12)", async () => {
    const w = await withoutFirstTab();
    try {
      const s = await session(w.port);
      await sleep(500);
      assert.equal(w.chrome.ended, undefined);
      assert.ok(!w.chrome.sent.some((m) => m.method === "Browser.close"));
      s.ws.close();
    } finally { await w.bridge.stop(); }
  });

  // The owner, 2026-11-12: the idle check kept Chrome open without a word, so the log could not say what held it. One line when
  // it keeps Chrome open, saying why, only when the reason changes; no address or title of a tab in it.
  const stays = (w) => w.lines.filter((l) => /Chrome stays open/.test(l));

  it("a page the owner should see, with no first tab of the bridge's, keeps Chrome open and says so once, over several rounds (2026-11-12)", async () => {
    const w = await withoutFirstTab();
    try {
      w.chrome.live.add("MAIL");
      w.chrome.info.MAIL = { title: "private inbox", url: "https://mail.example.test/secret" };
      await until(() => stays(w).length >= 1);
      await sleep(1000); // several graces more
      assert.equal(stays(w).length, 1, stays(w).join(" | "));
      assert.match(stays(w)[0], /1 page the owner should see is open/);
      assert.ok(!/mail\.example|private inbox/.test(stays(w)[0]), "no address or title in the line");
      assert.equal(w.chrome.ended, undefined);
      w.chrome.live.delete("MAIL");
      await until(() => w.chrome.ended === true);
    } finally { await w.bridge.stop(); }
  });

  it("a connected session keeps Chrome open, is named once in the log, and Chrome closes after it drops (2026-11-12)", async () => {
    const w = await world({ running: true, idleCloseMs: 150, register: fakeRegister(), chromeOpts: { leftover: true } });
    try {
      const s = await session(w.port, "code.read");
      await until(() => stays(w).length >= 1);
      await sleep(600);
      assert.equal(stays(w).length, 1, stays(w).join(" | "));
      assert.match(stays(w)[0], /a session is connected \(code\.read\)/);
      assert.equal(w.chrome.ended, undefined);
      s.ws.close();
      await until(() => w.chrome.ended === true);
    } finally { await w.bridge.stop(); }
  });

  // The owner, 2026-11-14: a Chrome kept sessions "connected" that were gone. The app's Python
  // client closes its TCP socket without a WebSocket close frame; the bridge's upgraded socket then gets `end`, never `close`.
  it("a session that hangs up without a close frame is dropped, and Chrome closes after it (2026-11-14)", async () => {
    const w = await world({ running: true, idleCloseMs: 150, register: fakeRegister(), chromeOpts: { leftover: true } });
    try {
      const sock = connect({ host: "127.0.0.1", port: w.port });
      await new Promise((r, j) => { sock.once("connect", r); sock.once("error", j); });
      sock.write([`GET /devtools/browser/code.read?key=${KEY} HTTP/1.1`, "Host: 127.0.0.1", "Upgrade: websocket", "Connection: Upgrade",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version: 13", "", ""].join("\r\n"));
      await new Promise((r) => sock.once("data", r)); // the 101
      await until(() => stays(w).length >= 1);
      assert.match(stays(w)[0], /a session is connected \(code\.read\)/);
      sock.end(); // no close frame
      await until(() => w.chrome.ended === true);
    } finally { await w.bridge.stop(); }
  });

  it("work in the queue, then the app not saying, are each said once, and Chrome closes when the work clears (2026-11-12)", async () => {
    const register = fakeRegister({ work: { waiting: 2, leased: 1 } });
    const w = await world({ register, running: true, idleCloseMs: 100, chromeOpts: { leftover: true } });
    try {
      await until(() => stays(w).length >= 1);
      await sleep(400);
      assert.equal(stays(w).length, 1, "the same reason is not said again");
      assert.match(stays(w)[0], /the app's queue holds work \(2 waiting, 1 leased\)/);
      register.work = null;
      await until(() => stays(w).length >= 2);
      assert.match(stays(w)[1], /the app does not say whether the queue holds work/);
      await sleep(400);
      assert.equal(stays(w).length, 2, "a changed reason is said once more, and no more");
      register.work = { waiting: 0, leased: 0 };
      await until(() => w.chrome.ended === true);
    } finally { await w.bridge.stop(); }
  });

  it("a reason said before Chrome ended is said again for the next Chrome (2026-11-12)", async () => {
    const register = fakeRegister({ work: { waiting: 1, leased: 0 } });
    const w = await world({ register, running: true, idleCloseMs: 100, chromeOpts: { leftover: true } });
    try {
      await until(() => stays(w).length >= 1);
      register.work = { waiting: 0, leased: 0 };
      await until(() => w.chrome.ended === true);
      await until(() => !w.bridge.connected);
      register.work = { waiting: 1, leased: 0 };
      await oneItem(w); // the next request starts a Chrome afresh; its queue is done and its work is waiting again
      await until(() => stays(w).length >= 2);
      assert.equal(stays(w)[1], stays(w)[0]);
    } finally { await w.bridge.stop(); }
  });

  it("a Chrome an earlier bridge left running stays while an item is leased or waiting, or the app does not say, and closes once the queue is empty", async () => {
    for (const work of [{ waiting: 0, leased: 1 }, { waiting: 1, leased: 0 }, null]) {
      const register = fakeRegister({ work });
      const w = await world({ register, running: true, idleCloseMs: 50, chromeOpts: { leftover: true } });
      try {
        await until(() => register.told.asked >= 3);
        assert.equal(w.chrome.ended, undefined, `held open by ${JSON.stringify(work)}`);
        register.work = { waiting: 0, leased: 0 };
        await until(() => w.chrome.ended === true);
        assert.equal(w.box.launched, 0);
      } finally { await w.bridge.stop(); }
    }
  });

  it("a Chrome an earlier bridge left running stays while a tab the owner should see is open, without asking the queue", async () => {
    const w = await world({ running: true, idleCloseMs: 50, register: fakeRegister({ keep: ["MINE"] }), chromeOpts: { tabs: ["MINE"] } });
    w.chrome.info.MINE = { title: "mine", url: "https://mail.example.test/" }; // the app's register holds it: kept, not swept
    try {
      await sleep(400); // several graces
      assert.equal(w.chrome.ended, undefined);
      assert.equal(w.chrome.live.has("MINE"), true, "the owner's tab stays");
      assert.equal(w.register.told.asked, 0);
      assert.equal(w.box.launched, 0);
    } finally { await w.bridge.stop(); }
  });

  it("looks at a Chrome left running only: with none running the bridge connects nothing and starts nothing, in the owner's own Chrome too", async () => {
    const own = fakeChrome();
    const { bridge } = await bridgeWith(own, { idleCloseMs: 50 }); // mode own, Chrome running: it asks the owner, so it waits to be asked
    try {
      await sleep(300);
      assert.equal(own.sockets.length, 0, "the owner's Chrome is not connected to at start");
    } finally { await bridge.stop(); }
    const w = await world({ running: false });
    try {
      await sleep(400);
      assert.equal(w.box.launched, 0);
      assert.equal(w.chrome.sockets.length, 0);
    } finally { await w.bridge.stop(); }
  });

  it("the owner's own Chrome is never closed and the idle check says nothing about it (2026-11-12)", async () => {
    const own = fakeChrome({ tabs: ["BLANK"] });
    const lines = [];
    const { bridge, port } = await bridgeWith(own, { idleCloseMs: 50, log: (l) => lines.push(l), register: fakeRegister({ work: { waiting: 1, leased: 0 } }) });
    try {
      const s = await session(port, "code.read");
      s.ws.close();
      await sleep(400);
      assert.ok(!own.sent.some((m) => m.method === "Browser.close"));
      assert.deepEqual(lines.filter((l) => /Chrome stays open/.test(l)), []);
    } finally { await bridge.stop(); }
  });

  it("a Chrome left running that cannot be connected to is not a failure of the browser jobs: the status stays clear", async () => {
    const w = await world({ running: true, chromeOpts: { opens: false }, startMs: 100 });
    try {
      await sleep(300);
      assert.equal(w.box.launched, 0);
      assert.equal((await (await fetch(`http://127.0.0.1:${w.port}/status`)).json()).why, "");
    } finally { await w.bridge.stop(); }
  });

  it("is started by the first request for the browser (a session or a code read connecting), and its first tab opens then", async () => {
    const w = await world({ idleCloseMs: 0 });
    try {
      const s = await session(w.port);
      assert.equal(w.box.launched, 1, "the first connection started it");
      assert.equal(anchors(w.chrome).length, 1, "and its first tab opened with it, the window's label");
      assert.ok((await s.call("Target.createTarget", { url: "https://jobs.example.test/1" })).result?.targetId);
      assert.equal(anchors(w.chrome).length, 1, "the session's tab opens from it, not beside it");
      assert.equal(w.box.launched, 1);
    } finally { await w.bridge.stop(); }
  });

  it("closes Chrome after the grace when nothing is queued: its last tab closes, the app's register is told, and the next request starts it afresh", async () => {
    const w = await world();
    try {
      await oneItem(w);
      const left = Date.now();
      await until(() => w.chrome.ended === true);
      assert.ok(Date.now() - left >= 140, "not before the grace");
      await until(() => !w.bridge.connected);
      assert.equal(w.box.running, false, "Chrome has ended");
      assert.ok(!w.chrome.sent.some((m) => m.method === "Browser.close"), "by its last tab closing, never a Browser.close");
      assert.ok(w.register.told.asked >= 1, "the app was asked what the queue holds");
      assert.ok(w.register.told.removed.includes("T1"), "the bridge's own first tab is on the register as gone");
      assert.ok(w.lines.some((l) => /the browser queue is empty: closing the app's Chrome/.test(l)));
      // The next request starts it again, and the first tab opens afresh.
      const again = await session(w.port);
      assert.equal(w.box.launched, 2, "started again");
      assert.ok((await again.call("Target.createTarget", { url: "https://jobs.example.test/2" })).result?.targetId);
      assert.equal(anchors(w.chrome).length, 2, "the first tab opened afresh");
      assert.equal(w.bridge.connected, true);
    } finally { await w.bridge.stop(); }
  });

  it("does not close Chrome while a session is connected", async () => {
    const w = await world();
    try {
      const s = await session(w.port);
      assert.ok((await s.call("Target.createTarget", { url: "https://jobs.example.test/1" })).result?.targetId);
      await sleep(500); // three graces
      assert.equal(w.chrome.ended, undefined);
      assert.equal(w.bridge.connected, true);
      assert.equal(w.register.told.asked, 0, "a connected session settles it: the queue is not asked");
    } finally { await w.bridge.stop(); }
  });

  it("does not close Chrome while an item is leased or waiting, or while the app does not say, and looks again each grace", async () => {
    for (const work of [{ waiting: 0, leased: 1 }, { waiting: 2, leased: 0 }, null, { waiting: "none", leased: 0 }]) {
      const register = fakeRegister({ work });
      const w = await world({ register, idleCloseMs: 50 });
      try {
        await oneItem(w);
        await until(() => register.told.asked >= 3);
        assert.equal(w.chrome.ended, undefined, `held open by ${JSON.stringify(work)}`);
        assert.equal(w.bridge.connected, true);
        register.work = { waiting: 0, leased: 0 };
        await until(() => w.chrome.ended === true);
      } finally { await w.bridge.stop(); }
    }
  });

  it("does not close Chrome when Chrome does not say what is open: it is looked at again, and closed once it does", async () => {
    const w = await world({ internalMs: 100, idleCloseMs: 50 });
    try {
      await oneItem(w);
      w.chrome.hangTargets = true;
      await sleep(500); // several graces, and Chrome's answers each time out after 100 ms
      assert.equal(w.chrome.ended, undefined, "what may be open there is not known");
      assert.equal(w.register.told.asked, 0, "the queue is not asked before Chrome has said");
      w.chrome.hangTargets = false;
      await until(() => w.chrome.ended === true);
    } finally { await w.bridge.stop(); }
  });

  it("does not close Chrome while a Launch window, a kept tab or any other tab the owner should see is open, though the app says the queue is empty", async () => {
    const scenarios = {
      "Go's window, handed to the owner": async (w) => {
        const go = await session(w.port, "go");
        const first = (await go.call("Target.createTarget", { url: "https://jobs.example.test/a", resumebotWindow: true })).result.targetId;
        assert.equal((await go.call("Target.resumebotKeep")).result.kept, 1);
        go.ws.close();
        return first;
      },
      "a page the owner opened": async (w) => {
        const s = await session(w.port);
        await s.call("Target.createTarget", { url: "https://jobs.example.test/1" });
        w.chrome.live.add("MINE");
        w.chrome.info.MINE = { title: "mine", url: "https://mail.example.test/" };
        s.ws.close();
        return "MINE";
      },
    };
    for (const [name, make] of Object.entries(scenarios)) {
      const w = await world({ idleCloseMs: 50 });
      try {
        const kept = await make(w);
        await sleep(400); // several graces
        assert.equal(w.chrome.ended, undefined, `${name}: Chrome is not ended`);
        assert.ok(w.chrome.live.has(kept), `${name}: the tab stays`);
        assert.ok(!w.chrome.sent.some((m) => m.method === "Browser.close"));
        assert.equal(w.bridge.connected, true);
        assert.equal(w.register.told.asked, 0, `${name}: it settles it without asking the queue`);
      } finally { await w.bridge.stop(); }
    }
  });

  // docs/PRD.md 52.9: the bridge looks again after each grace while any condition fails. It returned without looking again when
  // a kept or non-blank tab was open, so a stale tab that closed later left Chrome open until some other session came and went.
  it("looks again each grace while a kept tab is open, and closes Chrome once that tab is gone", async () => {
    const w = await world({ idleCloseMs: 50, chromeOpts: { announceDestroyed: true } });
    try {
      const go = await session(w.port, "go");
      const first = (await go.call("Target.createTarget", { url: "about:blank", resumebotWindow: true })).result.targetId;
      assert.equal((await go.call("Target.resumebotKeep")).result.kept, 1);
      go.ws.close();
      await sleep(400); // several graces
      assert.equal(w.chrome.ended, undefined, "Chrome stays while the tab is open");
      assert.equal(w.register.told.asked, 0, "the kept tab settles it without asking the queue");
      w.chrome.live.delete(first); // the owner closes it
      w.chrome.sockets[0].emit({ method: "Target.targetDestroyed", params: { targetId: first } });
      await until(() => w.chrome.ended === true);
      assert.ok(w.register.told.asked >= 1, "then the queue is asked, and Chrome closes");
    } finally { await w.bridge.stop(); }
  });

  it("a queue that refills within the grace does not close Chrome or start it again", async () => {
    const w = await world({ idleCloseMs: 300 });
    try {
      for (let i = 0; i < 3; i++) {
        await oneItem(w);
        await sleep(120); // the next item, inside the grace
      }
      assert.equal(w.box.launched, 1, "started once for the whole queue");
      assert.equal(w.chrome.ended, undefined);
      assert.equal(w.register.told.asked, 0, "the grace never ran out");
      await until(() => w.chrome.ended === true); // and then the queue is empty for the grace
    } finally { await w.bridge.stop(); }
  });

  it("a request that arrives while Chrome is ending waits for it and finds a fresh one", async () => {
    const w = await world({ chromeOpts: { endDelayMs: 250 } });
    try {
      await oneItem(w);
      await until(() => w.lines.some((l) => /closing the app's Chrome/.test(l)));
      const reply = await fetch(`http://127.0.0.1:${w.port}/json/version`); // Chrome has been asked to end, and is still going
      assert.equal(reply.status, 200, "answered by the Chrome that came after");
      assert.equal(w.box.launched, 2, "a fresh Chrome, never the dying one");
      assert.equal(w.bridge.connected, true);
    } finally { await w.bridge.stop(); }
  });

  it("is not waited on for ever when Chrome does not end: a tab opened meanwhile keeps it", async () => {
    const w = await world({ chromeOpts: { endsWhenEmpty: false } });
    try {
      await oneItem(w);
      await until(() => w.lines.some((l) => /closing the app's Chrome/.test(l)));
      const asked = fetch(`http://127.0.0.1:${w.port}/json/version`);
      const early = await Promise.race([asked, sleep(150).then(() => "waiting")]);
      assert.equal(early, "waiting", "held back while Chrome may be ending");
      assert.equal((await asked).status, 200, "answered once the wait is over");
      assert.equal(w.box.launched, 1, "the same Chrome, still open");
      const s = await session(w.port);
      assert.ok((await s.call("Target.createTarget", { url: "https://jobs.example.test/3" })).result?.targetId, "and its first tab opens again");
    } finally { await w.bridge.stop(); }
  });

  // Found by review: the DevTools socket can close before the process goes, and the profile's lock with it, and a request in
  // that gap found the lock held, started nothing and failed after the start wait ("could not be started"), which the browser
  // sessions then kept until the owner pressed Run now or Wake up.
  it("a request that arrives after Chrome closed its socket and before its profile lock is gone waits for the lock, then finds a fresh Chrome", async () => {
    for (const staleEndpoint of [false, true]) {
      const w = await world({ staleEndpoint, startMs: 500, endWaitMs: 2000, chromeOpts: { endDelayMs: 30, lockLingerMs: 200 } });
      try {
        await oneItem(w);
        await until(() => w.lines.some((l) => /closing the app's Chrome/.test(l)));
        const reply = await fetch(`http://127.0.0.1:${w.port}/json/version`);
        assert.equal(reply.status, 200, `the Chrome that came after answered (the old port file ${staleEndpoint ? "left" : "removed"})`);
        assert.equal(w.box.launched, 2, "a fresh Chrome started once the lock was gone");
        assert.equal(w.bridge.connected, true);
        const again = await session(w.port);
        assert.ok((await again.call("Target.createTarget", { url: "https://jobs.example.test/2" })).result?.targetId);
      } finally { await w.bridge.stop(); }
    }
  });

  it("a lock that outlives even the wait for Chrome to end still lets it start once the lock is gone", async () => {
    const w = await world({ startMs: 3000, endWaitMs: 60, chromeOpts: { endDelayMs: 30, lockLingerMs: 300 } });
    try {
      await oneItem(w);
      await until(() => w.lines.some((l) => /closing the app's Chrome/.test(l)));
      const reply = await fetch(`http://127.0.0.1:${w.port}/json/version`); // the wait is over at 60 ms; the lock goes at 330
      assert.equal(reply.status, 200, "started when the lock went, not left waiting for a Chrome that never starts");
      assert.equal(w.box.launched, 2);
    } finally { await w.bridge.stop(); }
  });

  // Found by review: a runner asks the bridge for Chrome (`/json/version`) and then starts a session, which takes Claude and
  // npx twenty seconds or more to connect; no session is connected in between, so the grace could end Chrome under it.
  it("a request for the browser's address begins the grace again, so Chrome stays up for the session that is starting", async () => {
    const w = await world({ idleCloseMs: 300 });
    try {
      await oneItem(w); // the grace begins as the last session goes
      await sleep(180);
      const asked = Date.now();
      assert.equal((await fetch(`http://127.0.0.1:${w.port}/json/version`)).status, 200, "the runner's check, then the session starts");
      await until(() => w.chrome.ended === true);
      assert.ok(Date.now() - asked >= 300 - 30, `Chrome went ${Date.now() - asked} ms after the request: a full grace, not what was left of the one before`);
      assert.equal(w.box.launched, 1);
    } finally { await w.bridge.stop(); }
  });
});

// The window's first tab is a page the app serves itself, not a temporary page (the owner, 2026-11-05). The window's first tab was a data: URL with one line of unstyled text; with the app's address (the one
// the launcher holds, `appUrl`) it is a page the app itself serves, built from app/frontend/browser-window.html (docs/PRD.md
// 52.8). Everything else about the first tab is as before: the bridge's own tab, the opener of every other tab, never
// minimized or raised, closed with the window, found again as its own. Without an address, today's data: page.
describe("the browser jobs' window's first tab is a page of the app (2026-11-05)", () => {
  const APP = "http://127.0.0.1:8430";
  const PAGE = `${APP}/browser-window.html`;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const creates = (chrome) => chrome.sent.filter((m) => m.method === "Target.createTarget");
  const firstTabs = (chrome, url = PAGE) => creates(chrome).filter((m) => m.params.url === url);
  const opensFrom = (chrome) => chrome.sent.filter((m) => m.method === "Runtime.evaluate" && /window\.open/.test(m.params.expression));

  it("names the app's own page at the address it is given, whatever else that address holds", () => {
    assert.equal(WINDOW_PAGE_PATH, "/browser-window.html");
    assert.equal(anchorUrl("http://127.0.0.1:8430"), "http://127.0.0.1:8430/browser-window.html");
    assert.equal(anchorUrl("http://localhost:9911/"), "http://localhost:9911/browser-window.html", "from the address, never a fixed one");
    assert.equal(anchorUrl("http://127.0.0.1:8430/pipeline?x=1#top"), "http://127.0.0.1:8430/browser-window.html", "the origin alone");
    assert.equal(anchorUrl("https://app.example.test"), "https://app.example.test/browser-window.html");
  });

  it("falls back to today's data: page when there is no address to name, or it is not an http one", () => {
    assert.match(ANCHOR_URL, /^data:text\/html,<title>resumebot<\/title>/, "today's page: a data: URL titled resumebot");
    for (const none of [undefined, null, "", "   ", "not an address", "ftp://x.test/", "javascript:alert(1)", "data:text/html,x", "file:///c:/x"]) {
      assert.equal(anchorUrl(none), ANCHOR_URL, JSON.stringify(none));
    }
  });

  it("is the file the frontend builds: app/frontend/browser-window.html, titled resumebot", { skip: !existsSync(new URL("../frontend/index.html", import.meta.url)) }, () => {
    const file = new URL(`../frontend${WINDOW_PAGE_PATH}`, import.meta.url);
    assert.ok(existsSync(file), `${WINDOW_PAGE_PATH} is a file of the frontend, so the build writes the page the bridge opens`);
    assert.match(readFileSync(file, "utf8"), /<title>resumebot<\/title>/);
  });

  it("opens the app's page as the window's first tab: one new background window, once, and not the data: page", async () => {
    const chrome = fakeChrome();
    const { port, bridge } = await bridgeWith(chrome, { appUrl: APP });
    try {
      const s = await session(port);
      await s.call("Target.createTarget", { url: "https://jobs.example.test/1" });
      await s.call("Target.createTarget", { url: "https://jobs.example.test/2" });
      assert.equal(creates(chrome).length, 1, "Chrome is asked for one new target only: the window's first tab");
      assert.equal(creates(chrome)[0].params.url, PAGE);
      assert.equal(creates(chrome)[0].params.newWindow, true);
      assert.equal(creates(chrome)[0].params.background, true);
      assert.equal(firstTabs(chrome, ANCHOR_URL).length, 0, "no data: page");
      s.ws.close();
    } finally { await bridge.stop(); }
  });

  it("is still the bridge's own tab and the opener: sessions never see, attach to or close it, and every tab they open is window.open from it", async () => {
    const chrome = fakeChrome();
    const register = fakeRegister();
    const { port, bridge } = await bridgeWith(chrome, { appUrl: APP, register });
    try {
      const a = await session(port);
      await a.call("Target.setDiscoverTargets", { discover: true });
      const t1 = (await a.call("Target.createTarget", { url: "https://jobs.example.test/1" })).result.targetId;
      const t2 = (await a.call("Target.createTarget", { url: "https://jobs.example.test/2" })).result.targetId;
      const opened = opensFrom(chrome);
      assert.equal(opened.length, 2);
      assert.ok(opened.every((m) => m.sessionId === "X-T1" && m.params.userGesture === true), "each from the first tab, as a user's click");
      assert.match(opened[0].params.expression, /jobs\.example\.test\/1/);
      assert.match(opened[1].params.expression, /jobs\.example\.test\/2/);
      assert.deepEqual((await a.call("Target.getTargets")).result.targetInfos.map((t) => t.targetId).sort(), [t1, t2].sort(), "its own tabs, not the first");
      assert.ok((await a.call("Target.attachToTarget", { targetId: "T1", flatten: true })).error, "not attached to");
      assert.ok((await a.call("Target.closeTarget", { targetId: "T1" })).error, "not closed");
      assert.ok((await a.call("Runtime.evaluate", { expression: "document.title" }, "X-T1")).error, "not driven");
      assert.ok(chrome.live.has("T1"));
      assert.deepEqual(register.told.opened.map((o) => o[0]), [t1, t2], "the register is told of the session's tabs, never the first");
      a.ws.close();
    } finally { await bridge.stop(); }
  });

  it("is closed with the window: the last session ending closes the first tab in the owner's own Chrome", async () => {
    const chrome = fakeChrome();
    const { port, bridge } = await bridgeWith(chrome, { appUrl: APP });
    try {
      const a = await session(port);
      const tab = (await a.call("Target.createTarget", { url: "https://jobs.example.test/1" })).result.targetId;
      assert.ok(chrome.live.has("T1") && chrome.live.has(tab));
      a.ws.close();
      await until(() => !chrome.live.has("T1") && !chrome.live.has(tab));
      assert.ok(chrome.live.has("OWNER"), "the owner's tab stays");
    } finally { await bridge.stop(); }
  });

  it("is still put on screen in the normal state once, and never raised or moved off, when the app's page is the first tab", async () => {
    const chrome = fakeChrome({ startMinimized: true, offScreen: true });
    const { port, bridge } = await bridgeWith(chrome, { appUrl: APP });
    try {
      const a = await session(port);
      await a.call("Target.createTarget", { url: "https://x.test/1" });
      await a.call("Target.createTarget", { url: "https://x.test/2" });
      assert.equal(firstTabs(chrome).length, 1);
      assert.deepEqual(Object.values(chrome.windowState), ["normal"]);
      assert.deepEqual(Object.values(chrome.placement), [ON_SCREEN]);
      assert.equal(chrome.sent.filter((m) => m.method === "Browser.setWindowBounds" && m.params.bounds.left === ON_SCREEN.left).length, 1, "set once");
      assert.deepEqual(chrome.sent.filter((m) => m.method === "Target.activateTarget" || m.method === "Page.bringToFront"), []);
      a.ws.close();
    } finally { await bridge.stop(); }
  });

  it("opens the data: page when it is not told the app's address, and the data: page is as much the opener", async () => {
    for (const options of [{}, { appUrl: "" }, { appUrl: "not an address" }, { appUrl: "ftp://x.test/" }]) {
      const chrome = fakeChrome();
      const { port, bridge } = await bridgeWith(chrome, options);
      try {
        const a = await session(port);
        await a.call("Target.createTarget", { url: "https://jobs.example.test/1" });
        assert.equal(creates(chrome).length, 1, JSON.stringify(options));
        assert.equal(creates(chrome)[0].params.url, ANCHOR_URL, JSON.stringify(options));
        assert.equal(opensFrom(chrome).filter((m) => m.sessionId === "X-T1" && m.params.userGesture).length, 1, "still opened from it");
        a.ws.close();
      } finally { await bridge.stop(); }
    }
  });

  it("logs which page the first tab opened at: the app's page when given an address, the built-in page (never the long data: URL) when not", async () => {
    for (const [options, expected] of [
      [{ appUrl: APP }, `the browser jobs' first tab opened at ${PAGE}`],
      [{}, "the browser jobs' first tab opened at the built-in page (no app address)"],
    ]) {
      const chrome = fakeChrome();
      const lines = [];
      const { port, bridge } = await bridgeWith(chrome, { ...options, log: (l) => lines.push(l) });
      try {
        const a = await session(port);
        await a.call("Target.createTarget", { url: "https://jobs.example.test/1" });
        const opened = lines.filter((l) => l.includes("first tab opened at"));
        assert.deepEqual(opened, [expected], JSON.stringify(options));
        assert.ok(lines.every((l) => !l.includes("data:text/html")), "the data: URL is never printed");
        a.ws.close();
      } finally { await bridge.stop(); }
    }
  });

  it("a first tab that stops opening tabs is replaced by the app's page again, and the session's tab still opens", async () => {
    const chrome = fakeChrome({ noOpenFrom: new Set(["T1"]) });
    const { port, bridge } = await bridgeWith(chrome, { appUrl: APP, openRetryMs: 5 });
    try {
      const a = await session(port);
      const made = await a.call("Target.createTarget", { url: "https://jobs.example.test/1" });
      assert.ok(made.result?.targetId, "the tab opened from the fresh first tab");
      assert.equal(firstTabs(chrome).length, 2, "the first tab was opened again, both times the app's page");
      assert.equal(creates(chrome).length, 2, "and nothing else was created");
      assert.ok(!chrome.live.has("T1"), "the stale one is closed");
      a.ws.close();
    } finally { await bridge.stop(); }
  });

  it("finds an earlier bridge's first tab as its own, whichever page it was: the app's page or the data: page, and no other tab titled resumebot", async () => {
    for (const left of [PAGE, true]) {
      const chrome = fakeChrome({ leftover: left });
      chrome.live.add("APP"); // the owner's own tab of the app: the same title, any other path
      chrome.info.APP = { title: "resumebot", url: `${APP}/pipeline` };
      chrome.live.add("ROOT");
      chrome.info.ROOT = { title: "resumebot", url: `${APP}/` };
      const { port, bridge } = await bridgeWith(chrome, { appUrl: APP });
      try {
        await session(port);
        await until(() => !chrome.live.has("OLD"));
        assert.ok(chrome.live.has("OWNER") && chrome.live.has("APP") && chrome.live.has("ROOT"), `the owner's tabs stay (leftover ${left === true ? "data" : "app"} page)`);
      } finally { await bridge.stop(); }
    }
  });

  it("leaves a tab at the app's page alone when it is not titled resumebot (a page that did not load is not ours to judge)", async () => {
    const chrome = fakeChrome({ leftover: PAGE });
    chrome.info.OLD.title = "an error page";
    const { port, bridge } = await bridgeWith(chrome, { appUrl: APP });
    try {
      await session(port);
      await sleep(100);
      assert.ok(chrome.live.has("OLD"), "titled something else: left alone");
    } finally { await bridge.stop(); }
  });

  // The app's own Chrome (SCOUT_BROWSER=profile), as in "Chrome only while the browser has work".
  describe("in the app's own Chrome", () => {
    const world = async ({ chromeOpts = {}, register = fakeRegister(), running = false, ...extra } = {}) => {
      const box = { running, launched: 0 };
      const chrome = fakeChrome({ tabs: [], endsWhenEmpty: true, onEnd: () => { box.running = false; }, ...chromeOpts });
      const port = await freePort();
      const bridge = ownChromeBridge({ key: KEY,
        port, mode: "profile", appUrl: APP, idleCloseMs: 150, endWaitMs: 400, register, WebSocketImpl: chrome.WebSocketImpl,
        state: () => ({ running: box.running, endpoint: box.running ? "ws://127.0.0.1:1/x" : "" }),
        launch: () => { box.launched++; box.running = true; chrome.dead = false; }, ...extra,
      });
      await bridge.start();
      return { chrome, box, port, bridge, register };
    };

    it("opens the app's page as its first tab when the first session asks, and the session's tab opens from it", async () => {
      const w = await world({ idleCloseMs: 0 });
      try {
        const s = await session(w.port);
        assert.equal(firstTabs(w.chrome).length, 1, "the first tab, opened with Chrome");
        assert.ok((await s.call("Target.createTarget", { url: "https://jobs.example.test/1" })).result?.targetId);
        assert.equal(firstTabs(w.chrome).length, 1, "not another beside it");
        assert.equal(firstTabs(w.chrome, ANCHOR_URL).length, 0);
        assert.ok(opensFrom(w.chrome).every((m) => m.sessionId === "X-T1" && m.params.userGesture));
      } finally { await w.bridge.stop(); }
    });

    it("is closed by the idle close, as the last tab: Chrome ends, the register is told it is gone, and the next request opens the page afresh", async () => {
      const w = await world();
      try {
        const s = await session(w.port);
        await s.call("Target.createTarget", { url: "https://jobs.example.test/1" });
        s.ws.close();
        await until(() => w.chrome.ended === true);
        assert.ok(w.chrome.sent.some((m) => m.method === "Target.closeTarget" && m.params.targetId === "T1"), "the first tab was closed by name");
        assert.ok(w.register.told.removed.includes("T1"));
        await until(() => !w.bridge.connected);
        const again = await session(w.port);
        assert.ok((await again.call("Target.createTarget", { url: "https://jobs.example.test/2" })).result?.targetId);
        assert.equal(firstTabs(w.chrome).length, 2, "the app's page again, both times");
      } finally { await w.bridge.stop(); }
    });

    it("is adopted at start: a Chrome an earlier bridge left running has its leftover first tab, the app's page, closed, and it closes once the queue is empty", async () => {
      const w = await world({ running: true, chromeOpts: { leftover: PAGE } });
      try {
        await until(() => w.chrome.ended === true);
        assert.equal(w.box.launched, 0, "found running, never started");
        assert.equal(w.chrome.live.has("OLD"), false, "the earlier bridge's first tab is gone");
        assert.ok(w.register.told.asked >= 1);
      } finally { await w.bridge.stop(); }
    });

    it("keeps its first tab, Chrome's last, while the blank tabs and what the register does not hold are swept", async () => {
      const w = await world({ idleCloseMs: 0, chromeOpts: { tabs: ["BLANK"] }, register: fakeRegister({ keep: [] }) });
      try {
        const s = await session(w.port);
        await until(() => !w.chrome.live.has("BLANK"));
        assert.ok(w.chrome.live.has("T1"), "the first tab stays: it is the window's label");
        assert.ok((await s.call("Target.createTarget", { url: "https://jobs.example.test/1" })).result?.targetId);
        assert.equal(w.chrome.ended, undefined);
      } finally { await w.bridge.stop(); }
    });
  });
});

// The owner, 2026-11-14 (docs/PRD.md path 52): the bridge serves only clients that carry the app's key.
describe("the bridge serves only clients that carry the app's key (2026-11-14)", () => {
  // A bridge that would start Chrome and connect to it if it were asked: what it does is counted.
  const counted = async (options = {}) => {
    const chrome = fakeChrome();
    const seen = { launches: 0, connects: 0 };
    const WebSocketImpl = function (...args) { seen.connects++; return new chrome.WebSocketImpl(...args); };
    const port = await freePort();
    const bridge = ownChromeBridge({ port, mode: "profile", state: () => ({ running: false, endpoint: "" }), launch: () => { seen.launches++; },
      WebSocketImpl, idleCloseMs: 0, key: KEY, ...options });
    await bridge.start();
    return { port: bridge.port, bridge, seen };
  };
  const upgrade = (port, query) => new Promise((resolve) => {
    const sock = connect({ host: "127.0.0.1", port });
    sock.once("connect", () => sock.write([`GET /devtools/browser/resumebot${query} HTTP/1.1`, `Host: 127.0.0.1:${port}`, "Upgrade: websocket",
      "Connection: Upgrade", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version: 13", "", ""].join("\r\n")));
    sock.once("data", (d) => { sock.destroy(); resolve(String(d).split("\r\n")[0]); });
    sock.once("error", () => resolve("error"));
  });

  it("answers 403 to the upgrade without the key or with a wrong one, and starts and connects nothing", async () => {
    const { port, bridge, seen } = await counted();
    try {
      for (const query of ["", "?key=", `?key=${"x".repeat(64)}`, `?key=${KEY}0`, `?key=${KEY.slice(1)}`]) {
        assert.match(await upgrade(port, query), /403/, `refused: ${query || "no key"}`);
      }
      assert.deepEqual(seen, { launches: 0, connects: 0 });
    } finally { await bridge.stop(); }
  });

  it("answers 403 on every HTTP route without the key or with a wrong one, and starts and connects nothing", async () => {
    const { port, bridge, seen } = await counted();
    try {
      const wrong = { [BRIDGE_KEY_HEADER]: "x".repeat(64) };
      for (const route of ["/json/version", "/json", "/json/list", "/status"]) {
        assert.equal((await bare(`http://127.0.0.1:${port}${route}`)).status, 403, `${route} with no key`);
        assert.equal((await bare(`http://127.0.0.1:${port}${route}`, { headers: wrong })).status, 403, `${route} with a wrong key`);
      }
      for (const route of ["/tabs/close", "/tabs/reconcile"]) {
        assert.equal((await bare(`http://127.0.0.1:${port}${route}`, { method: "POST", body: "{}" })).status, 403, `${route} with no key`);
        assert.equal((await bare(`http://127.0.0.1:${port}${route}`, { method: "POST", body: "{}", headers: wrong })).status, 403, route);
      }
      assert.deepEqual(seen, { launches: 0, connects: 0 }, "a refused request never wakes Chrome");
    } finally { await bridge.stop(); }
  });

  it("a bridge made with no key refuses everything, even a request with an empty key", async () => {
    const { port, bridge, seen } = await counted({ key: "" });
    try {
      assert.equal((await bare(`http://127.0.0.1:${port}/json/version`)).status, 403);
      assert.equal((await bare(`http://127.0.0.1:${port}/status`, { headers: { [BRIDGE_KEY_HEADER]: "" } })).status, 403);
      assert.match(await upgrade(port, "?key="), /403/);
      assert.deepEqual(seen, { launches: 0, connects: 0 });
    } finally { await bridge.stop(); }
  });

  it("serves the right key: in the header or the query on HTTP, in the query on the upgrade, and the DevTools address it gives carries it", async () => {
    const chrome = fakeChrome();
    const { port, bridge } = await bridgeWith(chrome, { key: KEY });
    try {
      assert.equal((await bare(`http://127.0.0.1:${port}/status`, { headers: { [BRIDGE_KEY_HEADER]: KEY } })).status, 200);
      assert.equal((await bare(`http://127.0.0.1:${port}/status?key=${KEY}`)).status, 200);
      const said = await (await bare(`http://127.0.0.1:${port}/json/version`, { headers: { [BRIDGE_KEY_HEADER]: KEY } })).json();
      assert.equal(said.webSocketDebuggerUrl, `ws://127.0.0.1:${port}/devtools/browser/resumebot?key=${KEY}`);
      assert.match(await upgrade(port, `?key=${KEY}`), /101/);
      const s = await session(port, "a-session"); // a client that read the address connects
      assert.ok((await s.call("Browser.getVersion")).result);
      s.ws.close();
    } finally { await bridge.stop(); }
  });

  it("compares in constant time over equal digests, whatever the lengths", () => {
    const req = (headers, url = "/") => ({ headers, url });
    assert.equal(carriesKey(req({ [BRIDGE_KEY_HEADER]: KEY }), KEY), true);
    assert.equal(carriesKey(req({}, `/x?key=${KEY}`), KEY), true);
    assert.equal(carriesKey(req({ [BRIDGE_KEY_HEADER]: "short" }), KEY), false);
    assert.equal(carriesKey(req({ [BRIDGE_KEY_HEADER]: KEY }), ""), false);
    assert.equal(carriesKey(req({}), KEY), false);
  });
});
