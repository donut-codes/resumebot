# Install

resumebot installs on Windows 10 and 11, for one user, with no administrator rights. Your own Claude Code does the
install by following a skill; this page says what it does, where everything goes, and how to check it.

## What you need

- Claude Code, installed and signed in, on a paid Claude subscription.
- A Windows account of your own. The install touches only your user's folders and settings.
- `git`, `uv` (with its own Python 3.12), Node 22 or newer, and Chrome. The install checks each and installs the missing
  ones with `winget` (git, uv and Node). If `winget` is missing, Claude tells you to install "App Installer" from the
  Microsoft Store and carries on when you have. Chrome is needed only for the optional browser jobs
  ([`browser-reader.md`](browser-reader.md)).

The app uses uv's own Python and no other. A Python from python.org, the Store or the system can carry an older SQLite
with a known bug that can damage a database, and the install refuses to go on with one.

## The two routes

**The plugin.** In Claude Code:

```
/plugin marketplace add EricWurth/resumebot
/plugin install resumebot-setup@resumebot
```

Then say `set up resumebot`.

**A clone.** In a terminal:

```
git clone https://github.com/EricWurth/resumebot
cd resumebot
claude
```

Then say `set up resumebot`. [`AGENTS.md`](../AGENTS.md) sends Claude to the same skill. The clone is only the place Claude
reads the instructions from; the app is installed at a release tag in its own folder, never from the clone or a branch.

## What the install does, in order

The steps are the install skill's
([`plugins/resumebot-setup/skills/install-resumebot/SKILL.md`](../plugins/resumebot-setup/skills/install-resumebot/SKILL.md)).
Claude runs every command itself; you never type or paste one. It says each step in a sentence before it runs it, and your
only actions are: answer one question up front (the name your roles are kept under, and whether the app starts when you
sign in), approve the sign-in in the browser tab that opens (if a page shows a code instead, paste it into the chat), and restart Claude Code. Rough times are from a rehearsal on a clean data folder.

| # | Step | Takes |
|---|---|---|
| 1 | Check the prerequisites and install what is missing | seconds, plus downloads |
| 2 | Download the app at the newest release (`app-v<version>`) into the code folder | seconds |
| 3 | `stack.py setup`: make the Python environment, install the locked packages (every file checked against its hash) and build the page | about a minute |
| 4 | Check the app's SQLite is clear of the known bug; stop if it is not | instant |
| 5 | `resumebot init`: make the data folder, its settings, its secret files and the database | seconds |
| 6 | `resumebot start`: the app starts outside the Claude session | 20 seconds or so |
| 7 | `resumebot smoke --lanes` and `resumebot doctor`: check the app and the five lanes | seconds |
| 8 | `resumebot signin`: a browser tab asks you to approve; that is all you do ([`connect-claude.md`](connect-claude.md)) | a minute |
| 9 | `resumebot connect`: register the app's two doors with your Claude Code | seconds |
| 9b | `resumebot shortcut`: put **resumebot** on your desktop (Windows) | instant |
| 10 | `resumebot project`: write three helper agents (career coach, master resume help, network-profile draft) into your project folder | instant |
| 11 | Restart Claude Code, in the same folder: it loads the doors and the helper agents; onboarding takes over ([`onboarding.md`](onboarding.md)) | |

The project folder is your own: start Claude Code in an empty folder and say `get me started` (the setup plugin's
`getting-started` agent follows these steps for you). The app is not in that folder; it is in the two folders below.
`resumebot project [folder]` refuses the app's own folders, writes only `.claude/agents/` in the folder, and never
changes a helper file that is already there.

You may see coloured text and a warning about a large page file while the page builds. Both are harmless.

The install never starts the app from inside the Claude session, because an app started there stops when Claude updates
or closes. It never reads or asks for your token: the sign-in window reads it off its own screen and stores it without passing it through Claude.

## Where everything goes

| What | Where |
|---|---|
| The app's code (replaced at an update) | `%LOCALAPPDATA%\Programs\resumebot` |
| Your data (never replaced) | `%LOCALAPPDATA%\resumebot` |
| Resumes and cover letters it builds | `Documents\resumebot`, one folder per role (made on the first save) |

The data folder holds `.env` (your name, time zone, the database address and a few settings), `resumebot.db` (the
database, one SQLite file), `secrets\` (the doors' tokens and the agents' sign-in), `logs\`, and `browser-profile\` if you
use the browser jobs. The two folders are separate: neither is inside the other.

The page listens on `127.0.0.1:8430`, this machine only; the engine behind it is on 8530. To use another port, set
`RESUMEBOT_APP_URL` in the data folder's `.env` (the engine takes that port plus 100). `stack.py setup` first tests every port the install will use (the page's, the engine's, their UDP notice ports, the five lanes' and, if the stack runs its own Postgres, its port); when one is taken it stops and says which port, what holds it and which setting moves it.

`resumebot init` sets `RESUMEBOT_DATA_DIR` to the data folder for your user, so later commands find it in a new terminal.
Until then, every command also takes `--data "<data folder>"`.

## Checking it

The `resumebot` command is in the app's environment, at
`%LOCALAPPDATA%\Programs\resumebot\app\backend\.venv\Scripts\resumebot.exe`. It is on your PATH only if the install put
it there; if it is not, call it by that full path. Add `--data "<data folder>"` if you did not set `RESUMEBOT_DATA_DIR`.

```
resumebot doctor
resumebot status
```

**`doctor`** prints one line for each piece, `ok` or `MISSING` with what to do, and exits 1 if anything is missing:

| Line | What it checks |
|---|---|
| Claude Code is installed | it can find `claude`, on the PATH or where Windows installs put it |
| Claude Code is signed in | `claude auth status` succeeds |
| agents sign in | a token is stored (`resumebot signin`); until that step this line is `MISSING` |
| Node is there | Node 22 or newer |
| SQLite is clear of the WAL-reset bug | the version is a safe one |
| settings | the data folder has its `.env` |
| the app answers | the page answers at its address |
| `resumebot-owner`, `resumebot-browser` | each door answers, and your Claude Code has it registered at this install's address |

The last line, `logs`, is not a check: it names the logs folder (`<data folder>\logs`) and the newest `page`, `engine` and
`supervisor` log file in it, by name only.

**When something fails**, send the last 200 lines of `page.log`, `engine.log` and `supervisor.log` from that folder, and
read them yourself first. They hold folder paths, which include your Windows user name. The app never writes tokens or
your resume text to them.

A door line that says "your Claude Code does not have it" means `resumebot connect` has not run yet: run it. One that says
your Claude Code has it registered at a different address means it points at another install: run `resumebot connect`
for this one.

**`status`** says where onboarding stands, in plain lines, and always exits 0. `resumebot status --for-claude` prints the
same lines; it changes nothing. A fresh install reads like this:

```
resumebot 1.193.0
install: set up at <data folder>
app: running at http://127.0.0.1:8430
resume: not imported yet
tracker: not imported (optional: only if they have one)
employer list: not imported (optional: only if they have one)
targets: not set yet
first search: not run yet
mail: not connected
next: import the master resume (...)
```

The `next:` line names the first step still to do. If the app is not running it says so and how to start it.

In your project folder, `.claude/agents/` should hold `career-coach.md`, `master-resume-help.md` and
`profile-draft.md`. To take a newer copy of one after an update, delete your file and run
`resumebot project` again.

`stack.py status` and `stack.py smoke --lanes` (see [Starting and stopping](#starting-and-stopping)) say what is running and run read-only
checks of the app and the five agent lanes.

## Starting and stopping

Double-click **resumebot** on the desktop (`resumebot shortcut` makes it; Windows only). It starts the app if it is not
running, waits until it answers, and opens the page in your default browser; if the app already runs it only opens the
page. It opens no console window. Pin it to the taskbar or the Start menu if you like. If it fails, the reason is written
to `logs\open.log` in the data folder.

Nothing starts the app at sign-in. `resumebot start` starts the app and the five lanes now, outside the Claude session,
and waits until the app answers; `resumebot smoke --lanes` runs the read-only checks. An older install that had the
sign-in task has it removed the next time `resumebot start` or `resumebot shortcut` runs.

`stack.py` is the app's own launcher. Run it with the app's own Python, and name the data folder every time:

```
cd %LOCALAPPDATA%\Programs\resumebot
app\backend\.venv\Scripts\python.exe app\scripts\local\stack.py --data "%LOCALAPPDATA%\resumebot" start
```

| Command | What it does |
|---|---|
| `start` | starts the page, the engine and the five lanes in the background, with no window, and returns when `/health` answers |
| `start --hold` | the same, with the scheduled searches and the lanes off: the page works, nothing runs by itself |
| `stop` | stops the lanes, then the engine, then the page |
| `status` | says what is running, and what `/health` says |
| `smoke` | read-only checks of the running app; `smoke --lanes` checks the five lanes too (use plain `smoke` on a held app) |
| `run` | like `start`, in this window, until Ctrl+C |

Start the app with the desktop shortcut or `resumebot start`, not with `stack.py start` from inside a Claude session: an
app started there stops when Claude updates or closes.

The programs' logs are in `logs\` in the data folder: `page.log`, `engine.log`, `supervisor.log`, `lanes.log` and
`open.log`. A log over 10 MB is set aside, and three are kept.

The page is at `http://localhost:8430`. A start refuses and says so if something already answers at the page's or the
engine's address (8430 and 8530 unless you changed the port).

## Updating, finishing, installing: one flow

There is one flow for a fresh install, for an install that stopped half way (the download made but not set up, set up but
not started, started but not signed in, signed in but not connected) and for an update to a newer release. Say `set up
resumebot` (or `update resumebot`) to Claude; it never stops at an existing install. The code folder holds nothing of
yours, so it is always replaced, never repaired; the data folder holds your work, so it is never deleted, overwritten or
renamed. In order:

1. Look at what is there (the code folder, the settings, the database, the agents' sign-in) and say what it found.
2. If the code folder exists, stop the app (`stack.py stop`; if that cannot run, the new code stops it in step 4).
3. If the data folder holds settings or a database, copy the whole folder (except the browser's profile) to a sibling
   `<data folder>-backup-<yyyymmdd-hhmm>`. If the copy fails, the update stops there and nothing has changed.
4. Delete the code folder and clone the newest `app-v<version>` into the same path. If a file is held open and the delete
   fails, the old folder is renamed `<code folder>-old-<yyyymmdd-hhmm>` (delete it later) and the clone goes in its
   place. The path never changes, so the desktop shortcut and the doors an earlier install made stay valid.
5. `stack.py setup`: a new Python environment and page build, and the migrations bring your database up to date when the
   app starts. A taken port is named with the program holding it.
6. `resumebot init`, only if the data folder has no settings yet (it never rewrites settings or secrets that exist).
7. `resumebot start` (it says so and changes nothing if the app already runs; it also removes the sign-in task an older
   install made), then
   `resumebot smoke --lanes`.
8. `resumebot signin`, which says "already signed in" and changes nothing when a token is stored (one the first sign-in
   flow stored is the same file). `resumebot signin --again` signs in anew and replaces the token only if that works.
9. `resumebot connect`: removes, then adds, both doors, so a stale registration is replaced.
   Then `resumebot shortcut`: writes **resumebot** on your desktop again (it replaces its own earlier one).
10. `resumebot doctor`, then one line from Claude: what it did and what is left (restart Claude Code once).

You are asked only for what is missing: your name on a fresh install, the sign-in approval when
the agents are not signed in. Every verb in the flow is safe to run twice, so a flow that was interrupted can be run again
from the top. The backup folder is yours to delete when you are happy with the update.

To go back after an update, stop the app (`stack.py --data "<data folder>" stop`) and copy the backup folder's contents
over the data folder. Between updates, `scripts\backup_local.py --data-dir "<data folder>"` (run from the code folder with
the app's own Python) makes a database backup of its own in `backups\` in the data folder.

## Removing it

1. Stop the app: `stack.py --data "<data folder>" stop`.
2. Delete **resumebot** from your desktop.
3. Remove the two doors from Claude Code:

   ```
   claude mcp remove resumebot-owner --scope user
   claude mcp remove resumebot-browser --scope user
   ```

4. Delete the code folder, the data folder and `Documents\resumebot`. The data folder holds your work: your master
   resume, every role and your settings. Keep a backup if you may come back.
5. If you or the install set `RESUMEBOT_DATA_DIR` or added the app's folder to your user PATH, remove those in Windows'
   environment variables settings.
6. If you added the plugin, remove it: `/plugin uninstall resumebot-setup@resumebot`.

`stack.py` is `%LOCALAPPDATA%\Programs\resumebot\app\scripts\local\stack.py`, run with the app's own Python,
`%LOCALAPPDATA%\Programs\resumebot\app\backend\.venv\Scripts\python.exe`.
