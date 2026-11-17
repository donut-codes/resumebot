#!/usr/bin/env python3
r"""One controlled live check of the tab register (docs/PRD.md path 52), run once by the owner's say-so against the
running instance and its Chrome.

    py -3.12 app/scripts/local/tab_check.py --i-have-the-owners-ok

What it does, and nothing else: it opens three tabs, each `about:blank`, in the app's one "resumebot" window through the
real paths, and closes them again.

  1. a code tab: the app's own code path (`browser_code._DevTools`, `open_own_tab`), registered by the app as it opens;
  2. a session-style tab: a connection to the bridge named like a session, whose tab the BRIDGE registers with the app;
  3. a Launch-style tab: the app's Launch connection (`go`), registered with a role the app already holds that is not in
     Ready to Launch (it is only named, never changed), opened in the one window, not in a window of its own.

For each it checks the tab is on the register, then makes the event that ends it happen (the code closes its tab; the
session ends as the runner's heartbeat does it, `tab_register.session_ended`; the role is found to be out of Ready to
Launch, `tab_register.close_for_roles`) and checks the tab is closed in Chrome and its row is gone. At the end it checks
that none of its rows is left. It writes nothing to any role, mail, setting or page, never navigates, and touches no
other tab: the register rows it makes and removes are the only change.

A hard 2-minute limit: whatever this opened (its own list, written as each tab opens) is closed by the bridge, and its
rows are removed, however it ends. Prints one line for each check and PASS or FAIL; the exit status is 0 on PASS.

The check runs in the app's own Python environment against the running database and bridge, so the instance must be up
(`stack.py status`). Standard library only here; the part that imports the app runs under `stack.py`'s environment.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import stack  # noqa: E402  (this folder's launcher: where everything is)

LIMIT_SECONDS = 120
TAB_URL = "about:blank"
#: What the check names its tabs' owners, so it never touches a row that is not its own.
SESSION_NAME = "tabcheck-session-"
CODE_NAME = "code:tabcheck"
OPENED_FILE = "tab-check-opened.json"


def _bridge_port(config: stack.Config) -> int:
    file = config.data / "browser-port"
    raw = file.read_text(encoding="utf-8").strip() if file.exists() else ""
    if raw.isdigit() and 0 < int(raw) < 65536:
        return int(raw)
    raw = config.get("SCOUT_BROWSER_PORT")
    return int(raw) if raw.isdigit() and 0 < int(raw) < 65536 else 9333


def _bridge_key(config: stack.Config) -> str:
    """The key the bridge asks of every request (the launcher's `secrets/bridge-key`), "" when there is none."""
    file = config.secrets / "bridge-key"
    return file.read_text(encoding="utf-8").strip() if file.exists() else ""


def _post(url: str, body: dict, headers: dict[str, str] | None = None, method: str = "POST") -> dict | None:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return json.loads(reply.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _clean_up(config: stack.Config, port: int) -> int:
    """Close what the check opened (its own list) by the bridge and take its rows off the register. How many it
    named."""
    file = config.run / OPENED_FILE
    try:
        opened = [str(t) for t in json.loads(file.read_text(encoding="utf-8"))]
    except (OSError, ValueError):
        return 0
    if opened:
        _post(f"http://127.0.0.1:{port}/tabs/close", {"targets": opened}, {"X-Bridge-Key": _bridge_key(config)})
        token = (config.secrets / "agent-token").read_text(encoding="utf-8").strip()
        for target in opened:
            _post(f"{config.app_url}/api/v1/agent/browser/tabs/{target}", {}, {"Authorization": f"Bearer {token}"},
                  method="DELETE")
    try:
        file.unlink()
    except OSError:
        pass
    return len(opened)


def outer(args: argparse.Namespace) -> int:
    if not args.i_have_the_owners_ok:
        print("This opens three about:blank tabs in the app's own Chrome and closes them again (docs/PRD.md path 52).\n"
              "Run it once, with the owner's OK:  py -3.12 app/scripts/local/tab_check.py --i-have-the-owners-ok")
        return 2
    config = stack.Config()
    port = _bridge_port(config)
    found = stack.health(config.app_url)
    if not (found and found.get("status") == "ok"):
        print(f"FAIL the app does not answer /health at {config.app_url}: start it (stack.py start)")
        return 1
    if not config.python.exists():
        print(f"FAIL the app's Python environment is not at {config.venv}: run stack.py setup")
        return 1
    env = config.app_environment(hold=False)
    env["TAB_CHECK_INNER"] = "1"
    command = [str(config.python), str(Path(__file__).resolve()), "--i-have-the-owners-ok"]
    try:
        done = subprocess.run(command, cwd=str(stack.BACKEND), env=env, timeout=LIMIT_SECONDS, check=False)
        status = done.returncode
    except subprocess.TimeoutExpired:
        print(f"FAIL the check did not finish in {LIMIT_SECONDS} seconds: closing what it opened")
        status = 1
    closed = _clean_up(config, port)
    if closed:
        print(f"cleanup: named {closed} tab(s) it had opened to the bridge and removed their rows")
    return status


# --- inside the app's environment --------------------------------------------------------------------------------


async def inner() -> int:
    import asyncio
    import uuid

    from sqlalchemy import select

    from app import db
    from app.jobsearch.browser import code as browser_code
    from app.jobsearch.browser import tabs as tab_register
    from app.jobsearch.models import OpenTab, Opportunity
    from app.jobsearch.stages import READY_TO_LAUNCH
    from app.settings import settings

    config = stack.Config()
    results: list[bool] = []

    def check(passed: bool, what: str, why: str = "") -> bool:
        results.append(passed)
        note = f" -- {why}" if why and not passed else ""
        print(f"[tab-check] {'ok  ' if passed else 'FAIL'} {what}{note}", flush=True)
        return passed

    def remember(target: str) -> None:
        file = config.run / OPENED_FILE
        try:
            held = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            held = []
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps([*held, target]), encoding="utf-8")

    db.configure()
    owner = settings.owner
    port = settings.bridge_port()

    async def rows() -> dict[str, OpenTab]:
        async with db.session_factory()() as session:
            return {t.target_id: t for t in await tab_register.tabs(session, owner)}

    async def wait_for(test, seconds: float = 10.0) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if await test():
                return True
            await asyncio.sleep(0.3)
        return False

    async def listed(tools, target: str) -> bool:
        """Whether the connection still sees the tab (the bridge shows a connection only its own)."""
        found = await tools.call("Target.getTargets")
        return any(t["targetId"] == target for t in found.get("targetInfos", []))

    class Plain(browser_code._DevTools):
        """A connection that does not register for itself, so the register row is the bridge's own work."""

        async def call(self, method, params=None, session_id=None):  # pyright: ignore[reportIncompatibleMethodOverride]
            return await self._call(method, params, session_id)

    code = browser_code._DevTools(port, CODE_NAME)
    session_name = SESSION_NAME + uuid.uuid4().hex[:8]
    session_tools = Plain(port, session_name, "session")
    go_tools = browser_code._DevTools(port, "go", "go")
    opened_in_chrome: list[tuple[object, str]] = []
    try:
        why = await browser_code.start_browser()
        if not check(not why, "the app's Chrome is up (the bridge answers)", why):
            return 1

        # 1. the app's own code opens a tab
        await code.open()
        target, _sid = await browser_code.open_own_tab(code, TAB_URL)
        remember(target)
        opened_in_chrome.append((code, target))
        held = await rows()
        check(target in held and held[target].kind == "code" and held[target].owner_id == CODE_NAME,
              "a code tab is on the register as code:tabcheck", str(sorted(held)))

        # 2. a session's tab, registered by the bridge
        await session_tools.open()
        made = await session_tools.call("Target.createTarget", {"url": TAB_URL})
        session_target = made["targetId"]
        remember(session_target)
        opened_in_chrome.append((session_tools, session_target))

        async def bridge_registered() -> bool:
            row = (await rows()).get(session_target)
            return bool(row and row.kind == "session" and row.owner_id == session_name)

        check(await wait_for(bridge_registered),
              "a session-style tab is registered by the bridge under the session's id")

        # 3. a Launch-style tab, with a role the app holds that is not in Ready to Launch (only named, never changed)
        async with db.session_factory()() as session:
            role_id = await session.scalar(select(Opportunity.id).where(
                Opportunity.owner == owner, Opportunity.status != READY_TO_LAUNCH).limit(1))
        if role_id is None:
            check(False, "a role outside Ready to Launch exists to name on the Launch-style tab")
        else:
            await go_tools.open()
            go_tools.role_id = role_id
            go_target = (await go_tools.call("Target.createTarget", browser_code.own_tab(TAB_URL)))["targetId"]
            remember(go_target)
            opened_in_chrome.append((go_tools, go_target))
            held = await rows()
            check(go_target in held and held[go_target].kind == "go" and held[go_target].role_id == role_id,
                  "a Launch-style tab is on the register as go, with its role", str(sorted(held)))
            windows = {
                (await tools.call("Browser.getWindowForTarget", {"targetId": tab}))["windowId"]
                for tools, tab in opened_in_chrome}
            check(len(windows) == 1, "all three tabs are in the one resumebot window", f"windows {sorted(windows)}")

        # the events that end them
        await code.call("Target.closeTarget", {"targetId": target})
        check(target not in await rows() and not await listed(code, target),
              "the code closing its tab takes it off the register and out of Chrome")

        async with db.session_factory()() as session:
            await tab_register.session_ended(session, owner, session_name)
            await session.commit()
        await tab_register.settle()
        gone = await wait_for(lambda: _not_listed(session_tools, session_target, listed))
        check(session_target not in await rows() and gone,
              "the session ending closes its tab and takes it off the register")

        if role_id is not None:
            await tab_register.close_for_roles(owner, [role_id])
            gone = await wait_for(lambda: _not_listed(go_tools, go_target, listed))
            check(go_target not in await rows() and gone,
                  "a Launch tab whose role is not in Ready to Launch is closed and taken off the register")

        mine = {tab for _tools, tab in opened_in_chrome}
        left = [t for t, r in (await rows()).items() if r.owner_id in (CODE_NAME, session_name) or t in mine]
        check(not left, "none of this check's rows is left on the register", str(left))
    except Exception as exc:  # the check says what went wrong, and the cleanup below still runs
        check(False, "the check ran to its end", f"{type(exc).__name__}: {exc}")
    finally:
        for tools, target_id in opened_in_chrome:
            try:
                await asyncio.wait_for(tools.call("Target.closeTarget", {"targetId": target_id}), 5)
            except Exception:  # already closed, or the connection is gone: the bridge closes a connection's tabs
                pass
        for tools in (code, session_tools, go_tools):
            await tools.close()
        await asyncio.sleep(0.5)
        for target_id in [t for _tools, t in opened_in_chrome]:
            await tab_register.closed(target_id)
        (config.run / OPENED_FILE).unlink(missing_ok=True)
    print("PASS" if all(results) and results else "FAIL")
    return 0 if all(results) and results else 1


async def _not_listed(tools, target: str, listed) -> bool:
    try:
        return not await listed(tools, target)
    except Exception:
        return True  # the connection is gone, and with it the tab


def main() -> int:
    parser = argparse.ArgumentParser(description="One controlled live check of the tab register (docs/PRD.md path 52).")
    parser.add_argument("--i-have-the-owners-ok", action="store_true",
                        help="required: it opens three tabs in the app's Chrome")
    args = parser.parse_args()
    if os.environ.get("TAB_CHECK_INNER") == "1":
        import asyncio

        sys.path.insert(0, str(stack.BACKEND))
        return asyncio.run(asyncio.wait_for(inner(), LIMIT_SECONDS - 15))
    return outer(args)


if __name__ == "__main__":
    sys.exit(main())
