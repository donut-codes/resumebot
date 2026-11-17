"""The Go button on Ready to Launch: check the day's roles, build their packets, and open each role in a window of the
app's own Chrome as its packet is done.

Ready to Launch holds the best N roles. Pressing Go starts a Launch run of its own at once (the owner, 2026-10-22),
outside the pool's queue and any day rule
or peak hours, since it is the owner's action. The page shows a progress bar and asks `progress`:

1. **Check.** Before anything is built, Go's checks run (`go_checks.py`, the owner 2026-10-16): the owner's mail for an
   application already sent (moved to Applied), and the posting for whether it is still open (closed: Gone Quiet).
2. **Open what is built.** A role whose packet already passed its checks opens at once, before anything is written.
3. **Build the rest, one after another.** A packet is asked for the first role that has none (`packets.start`, marked
   Launch's), the dedicated Launch session (`session_kinds.hand_out`: the Launch rung) writes it and the reviewer reads
   it, and only then is the next asked. `progress` says which role is being written and what it waits for (`current`); a
   writer that is not picked up, or a session that died, is said in `stalled` and `notes`, never left silent. Each role
   opens as a tab in a normal, visible window of its own in the app's Chrome (`browser_code.GoWindow`: never minimized,
   never raised), straight at its address. As the tab loads, the app reads it (`go_checks.page_verdict`: a 404 or 410,
   a board's removed-posting redirect, a `validThrough` that has passed, a closed or missing phrase, or a redirect to a
   page without the posting's id). A changed posting is flagged on its history and stays open; a closed posting's role
   ends (Gone Quiet, "the posting went") and its tab is closed. A role whose packet the checks held, or the writer never
   sent, is skipped with the reason on its card.
4. **Hand over.** A role whose tab the app's Chrome confirmed open (the Target was created) stays in Ready to Launch,
   marked opened with the time (`go_opened_at`), and keeps its place in the day's list: it becomes Applied when the
   owner's mail confirms the application (`go_checks.applied_by_mail`, the matching Go's checks already use) or when the
   owner marks it. A later Go opens it again (the owner, 2026-10-22: a tab opened earlier may be gone; the mark said
   "opened" when no tab ever was, and Launch then opened nothing, silently): every Ready role that is not Applied or
   closed gets a tab on every Go. Every tab is handed to the owner: the app never navigates, closes or minimizes them
   again. The tabs open together in one new window of the app's Chrome (`resumebotWindow`: `newWindow`, the first tab,
   the rest beside it); no window or tab of the owner's is touched.

If the app's Chrome cannot be started, the pages open once every packet is done, in a new window of the owner's everyday
Chrome (`--new-window`, no `--user-data-dir`), or the default browser without Chrome, and the reply says why and what
to do about it; those roles are not marked opened, since nothing confirmed a tab. A Go that opens nothing says why,
never a silent "done". Each role
opens at the employer's own posting when sourcing found it, else its `apply_url`, else its `url` (`go_checks.link_of`);
a role with neither, or one whose link is not a web address, is skipped and said so. Nothing is clicked or filled in.
Only a request from this machine is served (`go_status.from_this_machine`). The run keeps its own sessions and
commits as it goes.

The page reads the run's row (`GoRun`, `go_status.stored`), never this module's memory, so the page program answers
while the engine works. The engine writes the row whenever what the owner sees changes (`save`): when the button is
pressed, when the run begins, after the checks, as each tab opens, when the role being built, its step or its stall
changes, after the hand-over and at the end; nothing is written while nothing changes. The check counts (`checked`,
`check_total`) move inside the checks' own transaction and are written with the end of the checks. If the engine stops
in the middle of a run, the page shows the run as it was left until the engine starts again and closes it as stopped
(`settle_runs`, a step of every start)."""

import asyncio
import copy
import logging
import os
import subprocess
import sys
import time
import uuid
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

from process_engine.engine import Engine
from process_engine.errors import ActionError
from process_engine.models import COMPLETED, RUNNING, WAITING, ProcInstance
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.jobsearch import process, stages
from app.jobsearch.actors import BY_APP
from app.jobsearch.browser import code as browser_code
from app.jobsearch.browser import go_checks, go_status
from app.jobsearch.models import GoRun, Opportunity, Packet
from app.jobsearch.pipeline import quiet
from app.jobsearch.profile import store as profile_store
from app.jobsearch.states import PacketState
from app.jobsearch.work import engine_calls, stuck
from app.jobsearch.writing import packets, seats
from app.timeutil import owner_today
from app.trail import service as trail

log = logging.getLogger(__name__)

#: Go's window in the app's Chrome (tests replace it).
go_window = browser_code.GoWindow

#: Seconds between one tab and the next.
PAUSE = 1.0


#: Said in Go's reply when Chrome is not found.
NO_CHROME = "Chrome wasn't found, so the pages opened in your default browser."


def find_chrome() -> str | None:
    """Chrome's executable. On Windows: the standard folders, else the registry's App Paths entry. On macOS: the
    Applications folder, the machine's then the user's own. None elsewhere, and when it is not found."""
    if sys.platform == "darwin":
        inside = Path("Google Chrome.app") / "Contents" / "MacOS" / "Google Chrome"
        for root in (Path("/Applications"), Path.home() / "Applications"):
            if (root / inside).is_file():
                return str(root / inside)
        return None
    if sys.platform != "win32":
        return None
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(var)
        if root:
            exe = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if exe.is_file():
                return str(exe)
    try:
        import winreg

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
                    exe = winreg.QueryValue(key, None)
            except OSError:
                continue
            if exe and Path(exe).is_file():
                return str(exe)
    except ImportError:
        pass
    return None


def chrome_command(chrome: str, links: list[str]) -> list[str]:
    """One new window, the batch's links as its tabs in order; no `--user-data-dir`, so the everyday profile."""
    return [chrome, "--new-window", *links]


async def open_links(links: list[str], pause: float | None = None) -> str | None:
    """Open the links in a new Chrome window; without Chrome, in the default browser. Returns a note, or None."""
    if not links:
        return None
    chrome = await asyncio.to_thread(find_chrome)
    if chrome is not None:
        await asyncio.to_thread(subprocess.Popen, chrome_command(chrome, links))
        return None
    gap = PAUSE if pause is None else pause
    for n, link in enumerate(links):
        if n:
            await asyncio.sleep(gap)
        await asyncio.to_thread(webbrowser.open_new if n == 0 else webbrowser.open_new_tab, link)
    return NO_CHROME


def fix_for(why: str) -> str:
    """What the owner does about a browser that could not open tabs."""
    if "remote debugging" in why:
        return ""
    return ("To have Launch open them in the app's Chrome: turn on remote debugging in Chrome at "
            "chrome://inspect/#remote-debugging if it is off, close the app's Chrome window, and press Launch in "
            "Browser again.")


def _entry(role: Opportunity) -> dict:
    return {"id": str(role.id), "title": role.title, "company": role.company}


def _line(why: str) -> str:
    return why[:1].upper() + why[1:]


#: How often the run looks at the packets it waits for, and the longest it waits (a packet not done by then is skipped,
#: and that is said).
POLL_SECONDS = 3.0
#: The longest Go waits for one packet (written and read); then it stops, and the roles after it are not built.
MAX_WAIT_SECONDS = 20 * 60.0
#: How long a packet may wait with no session holding it before the page is told the Launch writer is not picking it up.
STALL_SECONDS = 90.0

#: The engine's own working copy of each owner's run, and its task. The page reads neither: it reads the row
#: (`go_status.stored`), which `save` writes from this copy.
_runs: dict[str, dict] = {}
_tasks: dict[str, asyncio.Task] = {}


def progress(owner: str) -> dict:
    """What Go is doing now, or did last: `phase` (checking, opening, building, done), `total` roles of the day's list
    that are to open, `built` of them with a finished packet, `checked` of the `check_total` postings looked at, the
    roles `opened` and `skipped` (with why) and `notes`; while a packet is being built, `current` (the role and `step`,
    what it waits for) and, when no session has taken it or one died, `stalled` (a sentence).
    Idle when Go has not run since the app started."""
    run = _runs.get(owner)
    if run is None:
        return {**go_status.fresh(), "phase": "idle", "done": True}
    return {k: v for k, v in run.items() if not k.startswith("_")}


def running(owner: str) -> bool:
    task = _tasks.get(owner)
    return task is not None and not task.done()


def _spawn(owner: str) -> None:
    """Make Go's task for the owner and keep it in `_tasks`. This is what `engine_calls.start("go", owner)` runs where
    the local door is installed; `start` does not call it itself."""
    if running(owner):  # a second bell for a run that goes starts nothing
        return

    @db.background_work  # the background limits, though the owner's request started it (`db.background`)
    async def run() -> None:
        try:
            await go(owner)
        except Exception as exc:  # the page is told, and the run ends
            log.warning("Go failed", exc_info=True)
            _runs[owner] = {**_runs[owner], "phase": "done", "done": True,
                            "notes": [*_runs[owner]["notes"], f"Launch in Browser stopped: {type(exc).__name__}."]}
            await save(owner)

    _tasks[owner] = asyncio.create_task(run())


engine_calls.register("go", _spawn)


async def start(owner: str) -> dict:
    """Start Go in the background unless it is running already; returns what it is doing."""
    if running(owner):
        return progress(owner)
    _runs[owner] = go_status.fresh()
    engine_calls.start("go", owner)
    return progress(owner)


async def _newest_packet(session: AsyncSession, role: Opportunity) -> Packet | None:
    """The role's newest packet, a failed one included; None when it has none."""
    return (await session.scalars(select(Packet).where(Packet.role_id == role.id)
                                  .order_by(Packet.version.desc()).limit(1))).first()


def _built(newest: str | None, role: Opportunity) -> bool:
    """Whether the role's packet passed its checks (a role from the old tracker has the tracker's word,
    `packet_complete`)."""
    if newest is None:
        return role.packet_complete
    return newest == PacketState.READY


async def _ask(session: AsyncSession, owner: str, role: Opportunity) -> str | None:
    """Ask for the role's packet, marked Launch's (the dedicated Launch writer takes it): None when asked, else why it
    cannot be (an employer nobody named, research out of date, no master resume)."""
    try:
        packet = await packets.start(session, owner, role.id, by=BY_APP)
    except packets.SentBack:
        return packets.SENT_BACK_SKIP
    except packets.PacketError as exc:
        return str(exc)
    packets.mark_launched(packet)
    await session.flush()
    return None


class _Opener:
    """Opens each role as a tab in Go's window as its packet is done, reads the tab and counts what opened."""

    def __init__(self, owner: str, state: dict) -> None:
        self.owner, self.state = owner, state
        self.window = None
        self.chrome_why: str | None = None  # None: not asked for yet; "": up; else why it could not start
        self.fallback: list[tuple] = []  # (role id, link) to open in the owner's own Chrome once the packets are done

    async def _start(self) -> None:
        self.chrome_why = await go_checks.start_browser()
        if not self.chrome_why:
            try:
                self.window = go_window()
                await self.window.open()
            except browser_code.BrowserUnavailable as exc:
                self.window, self.chrome_why = None, str(exc) or "the window did not open"
        if self.chrome_why:
            self.state["notes"].append(
                f"The app's Chrome couldn't open the tabs ({self.chrome_why}), so the pages open in your own Chrome "
                f"once every packet is done, without the in-tab check. {fix_for(self.chrome_why)}".strip())

    async def open(self, session: AsyncSession, role: Opportunity, link: str) -> None:
        now = datetime.now(UTC)
        # No transaction is held while Chrome is waited on (a held writer's turn refused the owner's saves as busy for
        # most of every cycle): what is written below begins a transaction of its own after the wait.
        await db.release(session)
        if self.chrome_why is None:
            await self._start()
        if self.window is None:
            self.fallback.append((role.id, link))
            return
        try:
            at = await self.window.tab(link, role.id)
        except browser_code.BrowserUnavailable:
            self.state["notes"].append(
                "The app's Chrome stopped opening tabs part way; the rest open in your own Chrome. "
                + fix_for("the tab did not open"))
            await self._let_go()
            self.fallback.append((role.id, link))
            return
        try:
            page = await self.window.read(at, link)
            verdict, said = go_checks.page_verdict(page, link, owner_today(now), role)
        except browser_code.BrowserUnavailable:
            verdict, said = go_checks.UNKNOWN, ""
        shown = verdict if verdict in (go_checks.OPEN, go_checks.CLOSED, go_checks.CHANGED) else "unknown"
        await trail.record(session, self.owner, event=go_checks.BROWSER_EVENT, link=("opportunity", role.id),
                           sentence=f"Checked in the launched tab: {shown}.", detail=said or None)
        if verdict == go_checks.CHANGED:
            await go_checks.note_changed(session, role, link, said)
        if verdict == go_checks.CLOSED and await quiet.end_gone(session, role, link, said):
            await db.release(session)  # closing the tab waits on Chrome: the role's move is committed first
            await self.window.close_tab(at)
            self.state["skipped"].append({**_entry(role), "why": "Its tab said the posting is closed: the tab was "
                                          "closed and the role moved to Gone Quiet."})
            return
        await self._opened(session, role, link, now, "opened in the app's Chrome")

    async def _opened(self, session: AsyncSession, role: Opportunity, link: str, now: datetime, why: str,
                      confirmed: bool = True) -> None:
        """Say the role opened; `go_opened_at` is set only when the tab was confirmed (`confirmed`)."""
        role.go_check, role.go_checked_at, role.go_skip = go_checks.OPEN, now, None
        if confirmed:
            role.go_opened_at = now
        await trail.record(session, self.owner, event="go_opened", actor="app", who=trail.APP_WHO,
                           link=("opportunity", role.id),
                           sentence=f"{role.title} at {role.company} was opened in the browser, packet ready. It "
                                    "stays in Ready to Launch until your mail confirms the application or you mark it "
                                    "Applied.")
        self.state["opened"].append({**_entry(role), "url": link, "why": why[:1].upper() + why[1:] + "."})

    async def _let_go(self) -> None:
        """Hand the tabs opened so far to the owner and close the window's connection to the bridge: a connection left
        open keeps the app's Chrome from closing when the work is done (the owner, 2026-11-12)."""
        window, self.window = self.window, None
        if window is not None:
            try:
                await window.keep()
            except Exception:  # the window is gone already; its connection is closed either way
                pass

    async def finish(self) -> None:
        await self._let_go()
        if self.fallback:
            note = await open_links([link for _id, link in self.fallback])
            if note:
                self.state["notes"].append(note)
            async with db.session_factory()() as session:
                for role_id, link in self.fallback:
                    if (held := await session.get(Opportunity, role_id)) is not None:
                        await self._opened(session, held, link, datetime.now(UTC),
                                           "sent to your own Chrome; not marked opened, since the app's Chrome never "
                                           "confirmed a tab", confirmed=False)
                await session.commit()


#: What a packet being built waits for, as the page says it (by `seats.claim_state`).
STEPS = {"unclaimed": "waiting for a writer session to start", "working": "the writer is putting it together"}


def _note(state: dict, text: str) -> None:
    """Add a note to the status once: the same sentence is never listed twice."""
    if text not in state["notes"]:
        state["notes"].append(text)


def _skip(state: dict, role: Opportunity, why: str) -> None:
    role.go_skip = why
    state["skipped"].append({**_entry(role), "why": why})


async def save(owner: str) -> None:
    """Keep the run's status in the database (`GoRun`): the row is what the page reads (`go_status.stored`), in
    whichever program serves the page. A status that is the same as the one last kept is not written again, so a run
    that waits takes no turn of the writer while nothing the owner sees changes."""
    run = _runs.get(owner)
    if run is None:
        return
    shown = {k: v for k, v in run.items() if not k.startswith("_")}
    if run.get("_kept") == shown:
        return
    kept = copy.deepcopy(shown)  # the run goes on changing its lists in place: what was kept must not change with them
    async with db.session_factory()() as session:
        row = await session.get(GoRun, owner)
        if row is None:
            session.add(GoRun(owner=owner, state=kept))
        else:
            row.state, row.updated_at = kept, datetime.now(UTC)
        await session.commit()
    run["_kept"] = kept


#: What the page is told of a run that was going when the app stopped.
STOPPED = ("The app stopped while Launch in Browser was running. Press it again: what is built opens, and "
           "what is not goes on.")


async def settle_runs(session: AsyncSession, owner: str) -> None:
    """A step of every start (`loops/startup.py`), in the caller's session, which the caller commits. A run lives in
    the engine's own process, so a row left unfinished was cut short by the stop: it is closed as stopped (`phase`
    stopped, with what it had done), never left "running" for the page to watch. A row in phase `asked` is a run the
    page asked for that nothing began: it is begun, once (the row says `checking` before the run is started). The look
    is on a reader, so a start with nothing to close takes no turn here. A run's process (`playbooks/jobsearch/go.bpmn`)
    that a stop left open is cancelled first, whatever the row says: the run saves `done` before its last step is
    complete. Nothing while this program's own run is going."""
    if running(owner):
        return
    async with db.read_session_factory()() as reader:
        left_open = await _open_instances(reader, owner)
        seen = await reader.get(GoRun, owner)
    for instance_id in left_open:
        await stuck.cancel_instance(session, instance_id, "the app stopped", "Launch in Browser")
    if left_open:
        await session.flush()
    if seen is None or not seen.state or seen.state.get("done", False):
        return
    row = await session.get(GoRun, owner)  # looked at again under the turn, before it is written
    if row is None or not row.state or row.state.get("done", False):
        return
    state = {**go_status.fresh(), **row.state}
    if state["phase"] == "asked":
        _runs[owner] = go_status.fresh()
        row.state, row.updated_at = go_status.fresh(), datetime.now(UTC)
        await session.flush()
        engine_calls.start("go", owner)
        return
    state.update(phase="stopped", done=True, current=None, building=[], stalled=None)
    _note(state, STOPPED)
    row.state, row.updated_at = state, datetime.now(UTC)
    await session.flush()


#: What goes wrong with a role's packet, for the supervisor (`supervise`).
DIED_TWICE, FAILED_TWICE, REFUSED = "died_twice", "failed_twice", "refused"


async def supervise(session: AsyncSession, owner: str, role: Opportunity, problem: str,
                    dossier: list[str]) -> str | None:
    """The one place Launch deals with trouble (the owner, 2026-10-22: a supervisor only when something goes wrong, no
    session in the normal path). It is given the one problem with what is known (`dossier`: the checks' findings and the
    run's notes for the role) and either asks the writer again with those notes (returns None) or gives up with a plain
    reason for the owner (returns the reason). It asks again once; a second trouble for the same role is final."""
    packet = await _newest_packet(session, role)
    if packet is not None and packet.status == PacketState.WRITING:  # its session is gone: let it go, ask again
        packet.status = PacketState.FAILED
        await session.flush()
    if packet is not None and packet.feedback.startswith(RETRY_WORDS):
        found = dossier[0] if dossier else "the writer did not finish it"
        return f"{packets.PACKET_FAILURE_SKIPS[0]}: {found}. Ask for it from its page, or edit the master resume."
    notes = "; ".join(dossier[:3]) or "the writer's session did not finish"
    try:
        asked = await packets.start(session, owner, role.id, f"{RETRY_WORDS} {notes}", by=BY_APP)
    except (packets.PacketError, packets.SentBack) as exc:
        return f"The packet could not be built again: {exc}"
    packets.mark_launched(asked)
    await session.flush()
    return None


#: What a packet asked for again by the supervisor starts with, so a second trouble is told from a first.
RETRY_WORDS = "Launch asked again after a problem:"
#: The supervisor (tests replace it).
supervisor = supervise


class _Build:
    """One role's packet being built: what the run knows of its writer. A packet no session holds is asked about; a
    session that dies while drafting is counted, and two deaths, two failures, or a packet the checks hold are handed to
    the supervisor, nothing else is."""

    def __init__(self, role_id) -> None:
        self.role_id = role_id
        self.began = time.monotonic()
        self.unheld_since: float | None = None
        self.was_writing = False
        self.said_stall = False
        self.deaths = 0
        self.failures = 0
        self.entry: dict = {}
        self.step = STEPS["unclaimed"]

    async def look(self, state: dict, owner: str, longest: float) -> str | None:
        """One look: None while it waits, else `opened`, `skipped` or `timeout`."""
        async with db.session_factory()() as session:
            role = await session.get(Opportunity, self.role_id)
            if role is None:
                return "skipped"
            self.entry = _entry(role)
            packet = await _newest_packet(session, role)
            status = packet.status if packet is not None else None
            if _built(status, role):
                state["built"] += 1
                state["phase"] = "opening"
                # Nothing is written in this session yet: its turn is let go so the row can say the tab is opening
                # (`save` takes a turn of its own), and `open` begins a transaction of its own for what it writes.
                await db.release(session)
                await save(owner)
                await state["_opener"].open(session, role, go_checks.link_of(role) or "")
                await session.commit()
                state["phase"] = "building"
                return "opened"
            trouble = None
            if status == PacketState.FAILED:
                self.failures += 1
                if self.failures == 1:  # a first failure is asked for again by code, no supervisor
                    if (why := await _ask(session, owner, role)) is not None:
                        _skip(state, role, _line(why))
                        await session.commit()
                        return "skipped"
                    self.deaths, self.said_stall, self.was_writing, self.began = 0, False, False, time.monotonic()
                    _note(state, f"{role.title} at {role.company}: the writer did not finish it, so it was asked for "
                          "again.")
                    await session.commit()
                    return None
                trouble = FAILED_TWICE
            elif status == PacketState.HELD:
                trouble = REFUSED
            elif self.deaths >= 2:
                trouble = DIED_TWICE
            if trouble is not None:
                dossier = [f"{c.get('check', 'a check')}: {'; '.join(c.get('problems') or [])}".strip(": ")
                           for c in packets.failures(packet.checks if packet is not None else [])]
                # Only the checks' findings: the run's notes (pulled forward, asked again, a session died) describe
                # the run, not why the packet failed, and must never become the reason the owner reads.
                why = await supervisor(session, owner, role, trouble, dossier)
                if why is None:
                    self.deaths, self.said_stall, self.was_writing, self.began = 0, False, False, time.monotonic()
                    _note(state, f"{role.title} at {role.company}: the packet was asked for again after a problem.")
                    await session.commit()
                    return None
                _skip(state, role, why)
                await session.commit()
                return "skipped"
            if packet is not None and status == PacketState.WRITING:
                self._watch(state, role, await seats.claim_state(session, packet.instance_id),
                            await seats.writer_busy(session, owner))
            await session.commit()
        if time.monotonic() - self.began >= longest:
            async with db.session_factory()() as session:
                if (role := await session.get(Opportunity, self.role_id)) is not None:
                    _skip(state, role, "Its packet wasn't finished in time.")
                await session.commit()
            return "timeout"
        return None

    def _watch(self, state: dict, role: Opportunity, claim: str, busy: bool = False) -> None:
        """Say what the packet waits for; a packet no session holds long enough, or one whose drafting session died, is
        said once on the status. A packet that waits while a session is writing another one is waiting its turn, not
        stalled."""
        self.step = STEPS[claim]
        now = time.monotonic()
        if claim == "working":
            self.unheld_since, self.said_stall = None, False
            self.was_writing = True
            state["stalled"] = None
            return
        if busy and not self.was_writing:  # the writer lane works on another packet: this one waits its turn
            self.unheld_since = None
            return
        self.unheld_since = now if self.unheld_since is None else self.unheld_since
        if claim == "unclaimed" and self.was_writing and not self.said_stall:  # drafting stopped: the session died
            self.said_stall, self.was_writing = True, False
            self.deaths += 1
            why = (f"The session writing the packet for {role.title} at {role.company} stopped before it was done. "
                   "The packet is waiting for the next session to take it.")
            state["stalled"] = why
            _note(state, why)
        elif now - self.unheld_since >= STALL_SECONDS and not self.said_stall:
            self.said_stall = True
            why = (f"No writer session has picked up the packet for {role.title} at {role.company} yet. "
                   "Sessions may all be busy, paused or stopped: check Agents.")
            state["stalled"] = why
            _note(state, why)


async def _ask_next(state: dict, owner: str, role_id) -> _Build | None:
    """This role's turn: its packet is asked for (marked Launch's), or an unfinished one is joined and reclaimed from a
    dead session. None when it cannot be asked for (the role is skipped with the reason)."""
    async with db.session_factory()() as session:
        role = await session.get(Opportunity, role_id)
        if role is None:
            return None
        packet = await _newest_packet(session, role)
        if packet is not None and packet.status == PacketState.WRITING:
            packets.mark_launched(packet)  # a mission's, an earlier Go's or a dead session's: a free session takes it
        elif _built(packet.status if packet is not None else None, role):
            pass  # finished while it waited its turn: the next look opens it
        elif packet is not None and packet.status == PacketState.HELD:
            pass  # refused by the checks: the supervisor is told (`_Build.look`), the packet is not replaced here
        elif (why := await _ask(session, owner, role)) is not None:
            _skip(state, role, _line(why))
            state["total"] -= 1
            await session.commit()
            return None
        await session.commit()
    return _Build(role_id)


async def _owner_limit(owner: str) -> int:
    async with db.session_factory()() as session:
        return max(1, (await profile_store.current(session, owner)).settings.active_sessions)


#: The most rounds of pulling roles forward one run makes (each round is a rebalance after something was skipped or
#: closed); the run also stops when a round finds nothing new.
MAX_ROUNDS = 20


def _add_skips(state: dict, entries: list[dict]) -> None:
    """Add skipped roles to the status once each (a role walked again in a later round is not listed twice)."""
    have = {x["id"] for x in state["skipped"]}
    for entry in entries:
        if entry["id"] not in have:
            state["skipped"].append(entry)
            have.add(entry["id"])


async def _sort(state: dict, checked, handled: set) -> tuple[list, list]:
    """The checked roles not handled yet, sorted: built ones open at once, the rest are built. A role with no web
    address is skipped and said."""
    open_now: list = []  # ids of the roles with a finished packet, in the board's order
    to_build: list = []  # ids of the roles that need one, in the board's order
    async with db.session_factory()() as session:
        for ready in checked.ready:
            if ready.id in handled:
                continue
            role = await session.get(Opportunity, ready.id)
            if role is None:
                continue
            handled.add(role.id)
            if go_checks.link_of(role) is None:
                if not any(x["id"] == str(role.id) for x in state["skipped"]):
                    _skip(state, role, "It has no web address to open.")
                continue
            packet = await _newest_packet(session, role)
            status = packet.status if packet is not None else None
            if _built(status, role):
                open_now.append(role.id)
            else:
                to_build.append(role.id)
        await session.commit()
    return open_now, to_build


async def _say_pulled(state: dict, ids: list, paired: int) -> int:
    """Name each role pulled forward into the day's list, with the skipped or closed role it took the place of (the
    skipped roles not yet paired, in the order they dropped out). Returns how many of the skipped are paired now."""
    async with db.session_factory()() as session:
        for role_id in ids:
            role = await session.get(Opportunity, role_id)
            if role is None:
                continue
            said = f"Pulled forward: {role.title} at {role.company}"
            if paired < len(state["skipped"]):
                lost = state["skipped"][paired]
                said += f", in place of {lost['title']} at {lost['company']}"
                paired += 1
            _note(state, said + ".")
    return paired


async def _turn(owner: str) -> None:
    """A turn of the pipeline, run by its loop and waited for (`schedule.pipeline_turn`), so Launch sees the day's list
    rebalanced. Launch never runs the turn itself: two turns at once fought over the same rows."""
    from app.jobsearch.loops import schedule  # the schedule reads the Go checks, which read this module

    await schedule.pipeline_turn(owner)


def _subject(owner: str) -> str:
    """What a run's process is about (`jobsearch-go:` is not the Agents run list's `jobsearch:`)."""
    return f"jobsearch-go:{owner}"


async def _open_instances(session: AsyncSession, owner: str) -> list[uuid.UUID]:
    return list((await session.scalars(select(ProcInstance.id).where(
        ProcInstance.process_key == process.GO, ProcInstance.subject == _subject(owner),
        ProcInstance.status.in_((RUNNING, WAITING))))).all())


async def _run_step(engine: Engine, instance_id: uuid.UUID) -> bool:
    """Do the step the run's process waits at, in a session of its own, and commit it with the process's move to the
    next step. False when the process has ended (done, failed or cancelled). An action's named error goes to the engine
    as one; any other failure marks the process failed (in a session of its own, so it holds) and is raised."""
    step = None
    try:
        async with db.read_session_factory()() as reader:  # where the process waits: no writer's turn for the look
            where = await engine.where_is(reader, instance_id)
            if where["status"] not in (RUNNING, WAITING):
                return False
            waits = [w for w in where["waiting_for"] if w["kind"] == "deferred"]
            if not waits:
                raise RuntimeError(f"the run waits at '{where['step']}', which no code does")
            step = waits[0]["step"]
            action = engine.registry.get(waits[0]["payload"]["action"])
            instance = await reader.get(ProcInstance, instance_id)
            if action is None or instance is None:
                raise RuntimeError(f"the run's step '{step}' has no action to run")
            data = copy.deepcopy(instance.data or {})
        async with db.session_factory()() as session:  # the step opens sessions of its own
            try:
                result = await action.fn(session, instance, data)
            except ActionError as error:
                await session.rollback()
                await engine.complete(session, instance_id, step, error=error.code, error_message=error.message)
            else:
                await engine.complete(session, instance_id, step, result)
            await session.commit()
            return True
    except Exception as exc:
        if step is not None:
            try:
                async with db.session_factory()() as session:
                    await engine.complete(session, instance_id, step, error="step_failed",
                                          error_message=f"{type(exc).__name__}: {exc}"[:300])
                    await session.commit()
            except Exception:
                log.exception("couldn't mark Launch in Browser's process failed")
        raise


async def step_begin(owner: str, data: dict) -> dict:
    """Begin: the run's status starts fresh and is kept at once. The run's private working state (`_`-keys, never
    shown or kept) is `_runs[owner]`: the steps of `playbooks/jobsearch/go.bpmn` read and write it, as the loop's
    locals did."""
    state = _runs.get(owner) if running(owner) else None
    state = _runs[owner] = state if state is not None else go_status.fresh()
    state.update(go_status.fresh())
    for key in ("_opener", "_handing_over", "_open_now", "_to_build", "_handled", "_paired", "_laps",
                "_skipped_before"):
        state.pop(key, None)
    state["_laps"] = 0
    await save(owner)
    return {}


async def step_turn(owner: str, data: dict) -> dict:
    """Turn_first: the day's list is rebalanced."""
    await _turn(owner)
    return {}


async def step_check(owner: str, data: dict) -> dict:
    """Check_roles: Go's checks, then the roles sorted into those to open now and those to build."""
    state = _runs[owner]
    async with db.session_factory()() as session:
        await go_checks.applied_by_mail(session, owner)
        checked = await go_checks.prepare(session, owner, progress=state)
        await session.commit()
    _add_skips(state, checked.skipped)
    state["notes"].extend(checked.notes)
    handled: set = set()
    open_now, to_build = await _sort(state, checked, handled)
    state["total"] = len(open_now) + len(to_build)
    if not state["total"] and not state["skipped"] and not checked.ready:
        _note(state, "Nothing in Ready to Launch to open: no role is waiting there that is not Applied or closed.")
    state["phase"] = "opening" if open_now else "building"
    await save(owner)
    state["_handled"], state["_open_now"], state["_to_build"] = handled, open_now, to_build
    state["_paired"] = 0  # how many of the skipped roles a pulled-forward role has been named in place of
    state["_opener"] = _Opener(owner, state)  # from here a failed step still hands over what is open
    return {}


async def step_open(owner: str, data: dict) -> dict:
    """Open_built: a lap's first part. Lap 1 counts the checks' skips (`_skipped_before` 0); a later lap counts the
    skips so far."""
    state = _runs[owner]
    state["_laps"] += 1
    state["_skipped_before"] = 0 if state["_laps"] == 1 else len(state["skipped"])
    opener = state["_opener"]
    for role_id in state["_open_now"]:  # the tabs that need nothing written open right away
        async with db.session_factory()() as session:
            if (role := await session.get(Opportunity, role_id)) is not None:
                state["built"] += 1
                await opener.open(session, role, go_checks.link_of(role) or "")
            await session.commit()
        await save(owner)
    state["phase"] = "building"
    return {}


async def step_build(owner: str, data: dict) -> dict:
    """Build_rest: the packets of the roles that have none, one after another (side by side up to the owner's limit).
    `fill`: the writers did not stop and a role dropped out (was skipped or closed) while they worked."""
    state = _runs[owner]
    gap, longest = data["poll"], data["wait"]
    limit = await _owner_limit(owner)
    queue, active, stopped = list(state["_to_build"]), [], False
    while queue or active:
        while queue and len(active) < limit and not stopped:  # a session for each, side by side up to the limit
            if (build := await _ask_next(state, owner, queue.pop(0))) is not None:
                active.append(build)
        for build in list(active):
            result = await build.look(state, owner, longest)
            if result is not None:
                active.remove(build)
                stopped = stopped or result == "timeout"
        if stopped and queue:  # the writers are not working: the roles not reached are said, not built
            async with db.session_factory()() as session:
                for role_id in queue:
                    if (role := await session.get(Opportunity, role_id)) is not None:
                        _skip(state, role, "Launch stopped waiting for the writer before this one's turn: "
                              "press Launch in Browser again.")
                await session.commit()
            queue = []
        state["building"] = [{**b.entry, "step": b.step} for b in active if b.entry]
        state["current"] = state["building"][0] if state["building"] else None
        if not any(b.entry for b in active) and active:
            state["current"] = None
        if not state["building"]:
            state["stalled"] = state["stalled"] if active else None
        await save(owner)
        if active or queue:
            await asyncio.sleep(gap)
    return {"fill": not stopped and len(state["skipped"]) != state["_skipped_before"]}


async def step_refill(owner: str, data: dict) -> dict:
    """Refill: something dropped out, so the day's list is rebalanced at once and the roles that moved up into Ready to
    Launch are taken by the same run. `again`: there is something to open or build; `round`: the lap just made, counting
    from 1."""
    state = _runs[owner]
    await _turn(owner)
    async with db.session_factory()() as session:
        checked = await go_checks.prepare(session, owner, progress=state)
        for entry in state["skipped"]:  # walking the line again clears a reason: this run's stay on their cards
            held = await session.get(Opportunity, uuid.UUID(entry["id"]))
            if held is not None and held.status == stages.READY_TO_LAUNCH and not held.go_skip:
                held.go_skip = entry["why"][:300]
        await session.commit()
    _add_skips(state, checked.skipped)
    for note in checked.notes:
        _note(state, note)
    open_now, to_build = await _sort(state, checked, state["_handled"])
    state["_open_now"], state["_to_build"] = open_now, to_build
    again = bool(open_now or to_build)
    if again:
        state["_paired"] = await _say_pulled(state, [*open_now, *to_build], state["_paired"])
        state["total"] += len(open_now) + len(to_build)
        await save(owner)
    return {"again": again, "round": state["_laps"]}


async def step_hand_over(owner: str, data: dict) -> dict:
    """Hand_over: the tabs are the owner's, then a turn of the pipeline. Said to be begun first, so a failure inside it
    does not run it twice (`go`'s safety net)."""
    state = _runs[owner]
    state["_handing_over"] = True
    opener = state.pop("_opener", None)
    state["current"], state["building"] = None, []
    if opener is not None:
        await opener.finish()
    await save(owner)  # what the hand-over opened is on the row before the turn below, which can take a while
    await _turn(owner)
    return {}


async def step_finish(owner: str, data: dict) -> dict:
    """Finish: a run that opened nothing says so, and the run is done."""
    state = _runs[owner]
    if not state["opened"] and not state["skipped"] and not state["notes"]:
        _note(state, "Launch opened nothing and found nothing to say why. Press it again, or check Agents.")
    state["phase"], state["done"] = "done", True
    await save(owner)
    return {}


async def _hand_over_if_failed(owner: str) -> None:
    """The safety net of `go`: a step failed after the checks made the opener, so what is open is handed over (and the
    error that follows is the step's own). Before the opener exists, as today, nothing is handed over; once the
    hand-over has begun, it is not run again."""
    state = _runs.get(owner)
    if state is not None and state.get("_opener") is not None and not state.get("_handing_over"):
        await step_hand_over(owner, {})


async def go(owner: str, *, poll: float | None = None, wait: float | None = None) -> dict:
    """Run Go (the module's words) and return what it did, as `progress` says: `opened`, `skipped` and `notes`. `poll`
    and `wait` are the seconds between looks at the packets and the longest wait for one. When a role is skipped or
    closed, the day's list is rebalanced at once and the roles that move up into Ready to Launch are taken by the same
    run, until the day's places are filled or Second Impression has nothing left. The order of the steps is the process
    `playbooks/jobsearch/go.bpmn`, run by the engine, one step at a time, each in a session of its own. A step that
    fails ends the run by raising, after what is open has been handed over."""
    data = {"owner": owner, "poll": POLL_SECONDS if poll is None else poll,
            "wait": MAX_WAIT_SECONDS if wait is None else wait}
    try:
        engine = process.stepped_engine()
        async with db.session_factory()() as session:
            for earlier in await _open_instances(session, owner):
                await stuck.cancel_instance(session, earlier, "a later Go began", "Launch in Browser")
            instance = await engine.start(session, process.GO, data, subject=_subject(owner))
            await session.commit()
            instance_id = instance.id
        while await _run_step(engine, instance_id):
            pass
        async with db.read_session_factory()() as reader:
            ended = await reader.get(ProcInstance, instance_id)
        if ended is None or ended.status != COMPLETED:
            raise RuntimeError("the run's process did not finish")
    except BaseException:
        await _hand_over_if_failed(owner)
        raise
    return progress(owner)
