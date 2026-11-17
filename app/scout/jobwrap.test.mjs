// jobwrap.py (docs/PRD.md path 52.6): a session's whole process tree ends with the wrapper, however the wrapper ends.
// Only node processes are started here (hidden, no window, no browser); the tests that need a Windows Job Object are
// skipped elsewhere. Run: node --test jobwrap.test.mjs (from app/scout). Needs `py -3` (or SCOUT_JOBWRAP_PYTHON).
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), "jobwrap.py");
const WINDOWS = process.platform === "win32";
const PYTHON = process.env.SCOUT_JOBWRAP_PYTHON ? [process.env.SCOUT_JOBWRAP_PYTHON] : ["py", "-3"];
const havePython = spawnSync(PYTHON[0], [...PYTHON.slice(1), "-c", "pass"], { windowsHide: true, stdio: "ignore" }).status === 0;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch (e) { return e.code === "EPERM"; } };
const until = async (check, ms = 10_000) => {
  for (const end = Date.now() + ms; Date.now() < end;) { if (check()) return; await sleep(50); }
  assert.fail("timed out");
};

const wrap = (...command) => spawn(PYTHON[0], [...PYTHON.slice(1), SCRIPT, "--", ...command], { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
const output = (child) => new Promise((resolve) => {
  let out = "";
  child.stdout.on("data", (d) => { out += d; });
  child.stdout.on("close", () => resolve(out));
});

// A command that starts a grandchild, says both pids, and waits.
const TREE = "const c = require('child_process').spawn(process.execPath, ['-e', 'setInterval(()=>{},1000)'], {stdio:'ignore', windowsHide:true});" +
  "console.log(JSON.stringify({child: process.pid, grandchild: c.pid})); setInterval(()=>{},1000);";

describe("jobwrap.py", { skip: !havePython && "no Python" }, () => {
  it("runs the command with its output and gives its exit code back", async () => {
    const child = wrap(process.execPath, "-e", "console.log('hello'); process.exit(3)");
    const out = output(child);
    const code = await new Promise((resolve) => child.on("close", resolve));
    assert.equal((await out).trim(), "hello");
    assert.equal(code, 3);
  });

  it("says how to use it when it is given no command", () => {
    const run = spawnSync(PYTHON[0], [...PYTHON.slice(1), SCRIPT], { windowsHide: true, encoding: "utf8" });
    assert.equal(run.status, 2);
    assert.match(run.stderr, /usage/);
  });

  it("a command that cannot start is said, not a crash", () => {
    const run = spawnSync(PYTHON[0], [...PYTHON.slice(1), SCRIPT, "--", "no-such-program-here"], { windowsHide: true, encoding: "utf8" });
    assert.equal(run.status, 127);
    assert.match(run.stderr, /could not start/);
  });

  it("the whole tree ends when the wrapper is killed outright, with no walk of the tree", { skip: !WINDOWS && "a Windows Job Object" }, async () => {
    const wrapper = wrap(process.execPath, "-e", TREE);
    const first = await new Promise((resolve) => wrapper.stdout.once("data", (d) => resolve(JSON.parse(String(d).trim()))));
    assert.ok(alive(first.child) && alive(first.grandchild));
    // Only the wrapper is ended (no /T): the job is what takes the rest.
    spawnSync(join(process.env.SystemRoot || "C:\\Windows", "System32", "taskkill.exe"), ["/PID", String(wrapper.pid), "/F"], { windowsHide: true, stdio: "ignore" });
    await until(() => !alive(first.child) && !alive(first.grandchild));
  });

  it("the whole tree ends when the command ends, so what it started and left running does not stay", { skip: !WINDOWS && "a Windows Job Object" }, async () => {
    const script = "const c = require('child_process').spawn(process.execPath, ['-e', 'setInterval(()=>{},1000)'], {stdio:'ignore', windowsHide:true});" +
      "console.log(c.pid); setTimeout(()=>process.exit(0), 300);";
    const child = wrap(process.execPath, "-e", script);
    const grandchild = Number((await output(child)).trim());
    await new Promise((resolve) => child.on("close", resolve));
    await until(() => !alive(grandchild));
  });
});
