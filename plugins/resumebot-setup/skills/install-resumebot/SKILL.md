---
name: install-resumebot
description: Install the resumebot app on this computer, finish an install that stopped half way, or update it to the newest release, and connect it to this Claude Code. Use when the user says "set up resumebot", "install resumebot", "update resumebot", "connect resumebot", or when the session-start status says resumebot is not installed, not set up or not running.
---

# Set up resumebot (install it, finish it, or update it, and connect it)

You put the newest release of the user's own copy of the app on their own computer and connect it to this Claude Code.
**One flow does a fresh install, an install that stopped half way and an update**: the app's code folder holds nothing
of the user's, so it is always replaced, never repaired; the data folder holds all of their work, so it is never
deleted, overwritten or renamed. **You run every command yourself and never give the user a command to type or paste.**
Do the steps in order, say each in one sentence before you do it, and never print a token or a password. Never stop at
"already installed": an existing install is a reason to back up and replace, not to ask. The app keeps everything in one
SQLite file in the user's data folder; there is no database server and nothing here uses Postgres.

## What the user does

Only these things; tell them so up front:

1. **Answer one question up front, only when the install has no settings yet:** their name (the roles are kept under
   it). An update asks nothing: it keeps the name.
2. **Approve the sign-in, only if the agents are not signed in yet:** approve once in the browser tab that opens. Only if
   no tab opens: open the link Claude gives, and if that page shows a code, paste the code into this chat.
3. **Restart Claude Code** at the end.

To start the app later, double-click **resumebot** on the desktop: it starts the app if it is not running and opens its
page. They can pin it wherever they like (the taskbar, the Start menu) from there.

Everything else is yours. Get one yes to the plan: install missing programs, back up the data folder, download the app
at the newest release over the old copy, start the app, register it with this Claude Code. Say that their data stays.

| What | Where (Windows; macOS and Linux in brackets) |
|---|---|
| The app's code, `$CODE` | `%LOCALAPPDATA%\Programs\resumebot` [`~/.local/share/resumebot-app`] |
| The user's data, `$DATA` | the user variable `RESUMEBOT_DATA_DIR` if set, else `%LOCALAPPDATA%\resumebot` [`~/.local/share/resumebot`] |

Two separate folders, neither inside the other: the code is replaced at an update, the data never is. The code folder's
path never changes, so the desktop shortcut and the registered doors an earlier install made keep pointing at it. An install
made earlier at `%LOCALAPPDATA%\resumebot-app` is left alone; this skill never moves it.

## How the commands are written

Every command runs as written in Windows PowerShell 5, in Git Bash and in macOS and Linux shells: no `&&`, no pipes, no
shell-only tricks. The one difference is how a quoted program is called: in PowerShell put `& ` before it
(`& "$PY" ...`), in sh not (`"$PY" ...`). Python does the work that differs between shells, with the code in one
double-quoted argument that holds only single quotes. Each command block is its own shell: set the variables again at
the top of each. Use the install's own Python and `resumebot` by these absolute paths, never from PATH.

PowerShell:

```powershell
$CODE = "$env:LOCALAPPDATA\Programs\resumebot"
$DATA = [Environment]::GetEnvironmentVariable('RESUMEBOT_DATA_DIR', 'User')
if (-not $DATA) { $DATA = "$env:LOCALAPPDATA\resumebot" }
$PY = uv python find --managed-python 3.12
$VPY = "$CODE\app\backend\.venv\Scripts\python.exe"
$RB = "$CODE\app\backend\.venv\Scripts\resumebot.exe"
```

sh (on macOS and Linux the folders are the bracketed ones and the environment is `.venv/bin/python` and
`.venv/bin/resumebot`; in Git Bash `$LOCALAPPDATA` is the Windows folder):

```sh
CODE="$LOCALAPPDATA/Programs/resumebot"
DATA="${RESUMEBOT_DATA_DIR:-$LOCALAPPDATA/resumebot}"
PY=$(uv python find --managed-python 3.12)
VPY="$CODE/app/backend/.venv/Scripts/python.exe"
RB="$CODE/app/backend/.venv/Scripts/resumebot.exe"
```

sh on macOS (and Linux):

```sh
CODE="$HOME/.local/share/resumebot-app"
DATA="${RESUMEBOT_DATA_DIR:-$HOME/.local/share/resumebot}"
PY=$(uv python find --managed-python 3.12)
VPY="$CODE/app/backend/.venv/bin/python"
RB="$CODE/app/backend/.venv/bin/resumebot"
```

## 1. Prerequisites

Check each; install only what is missing, and say what you install. Do not pipe a downloaded script into a shell.

| Need | Check | Install |
|---|---|---|
| Claude Code | `claude --version` | nothing |
| winget (Windows) | `winget --version` | if missing, say "Install 'App Installer' from the Microsoft Store, then I will carry on." and wait |
| git | `git --version` | `winget install --id Git.Git -e` [brew, apt] |
| uv | `uv --version` | `winget install --id astral-sh.uv -e` [`brew install uv`] |
| Python 3.12, uv's own | `uv python find --managed-python 3.12` | `uv python install 3.12 --upgrade --no-bin` (run it every time: the newest build carries the newest SQLite) |
| Node 22 or newer | `node --version` | `winget install OpenJS.NodeJS.LTS` [brew, nvm] |

Never use a Python from python.org, the Store or the system for the app: its SQLite may carry a known bug. A program just
installed may not be on this shell's PATH yet; call it by its full path.

## 2. Look, then make it safe

**Look.** One command says what is already here (it changes nothing):

```
& "$PY" -c "import os,sys; c,d=sys.argv[1:3]; print('code folder:',os.path.isdir(c)); print('settings:',os.path.isfile(os.path.join(d,'.env'))); print('database:',os.path.isfile(os.path.join(d,'resumebot.db'))); print('agent token:',os.path.isfile(os.path.join(d,'secrets','claude-oauth-token')))" "$CODE" "$DATA"
```

Say in one sentence what you found ("a fresh install", "an install that stopped before it started", "an update from an
older release"). The answers decide only which later steps have work: the code folder is replaced whenever it is there,
`init` runs only when there are no settings, the sign-in only when there is no agent token.

**Stop the app, if the code folder exists.** Only if `$CODE/app/scripts/local/stack.py` is there:

```
& "$PY" "$CODE/app/scripts/local/stack.py" --data "$DATA" stop
```

It stops the app and its lanes by the process files in the data folder, and is safe when nothing runs. If it fails, or the folder has no `stack.py` (half set up), say so in a
sentence and go on: if the delete in step 3 then fails because the app still runs, step 3 stops it with the new code.

**Back up the data folder first.** If the data folder holds settings or a database, this copies it whole to a sibling
`<data>-backup-<yyyymmdd-hhmm>` (the browser's profile is left out: it is a cache of web sessions, and it can be huge or
held open). It prints the folder it made, or says there is nothing to back up. **If it prints an error, stop the whole
update, say why in plain words, and change nothing.**

```
& "$PY" -c "import os,sys,shutil,time; d=os.path.normpath(sys.argv[1]); has=any(os.path.exists(os.path.join(d,n)) for n in ('.env','resumebot.db')); b=d+'-backup-'+time.strftime('%Y%m%d-%H%M'); has and shutil.copytree(d,b,dirs_exist_ok=True,ignore=shutil.ignore_patterns('browser-profile')); print('backed up to '+b if has else 'no data yet, nothing to back up')" "$DATA"
```

Never delete, overwrite or rename the data folder, or this backup.

## 3. Get the newest release into the code folder

Only ever at a release tag (`app-v<version>`), never a branch. Find the repository address, in this order, and call it
`$REPO`:

1. **The folder Claude was started in,** if it is a git clone whose origin serves resumebot (it has `AGENTS.md` and
   `plugins/resumebot-setup`): the output of `git remote get-url origin` run in that folder. If `git ls-remote --tags
   --refs "<that address>" 'app-v*'` lists no tag, go on to 2 as if the folder were not a clone.
2. **The marketplace this plugin came from:** `claude plugin marketplace list`, the source of the one holding
   `resumebot-setup` (`owner/repo` means `https://github.com/owner/repo`); else `~/.claude/plugins/known_marketplaces.json`.
3. Only if both fail, ask the user.

Set `REPO` (PowerShell `$REPO = "<address>"`, sh `REPO="<address>"`). The newest tag is the highest version number
(not the latest by date); this prints it, and exits with an error when there is none:

```
& "$PY" -c "import re,subprocess,sys; out=subprocess.run(['git','ls-remote','--tags','--refs',sys.argv[1],'app-v*'],capture_output=True,text=True).stdout; tags=re.findall(r'refs/tags/(app-v[0-9.]+)\s',out); print(max(tags,key=lambda t:[int(n) for n in t[5:].split('.')]))" "$REPO"
```

If the download fails, or no `app-v*` tag exists, say "Git could not download resumebot. Check the repository address and
your network, or ask whoever gave you the address, then tell me and I will try again." and stop; the old code folder and
the data are untouched. Ask for no password or token and look for no other copy.

**Delete the code folder, if it exists.** The code holds nothing of the user's. This takes the folder's link to the page's
packages off first and clears read-only files, and it refuses (deleting nothing) if the data folder is inside the code
folder or the other way round:

```
& "$PY" -c "import os,sys,shutil,stat; c=os.path.normcase(os.path.abspath(sys.argv[1])); d=os.path.normcase(os.path.abspath(sys.argv[2])); assert not (d+os.sep).startswith(c+os.sep) and not (c+os.sep).startswith(d+os.sep), 'the data folder and the code folder are inside each other: nothing deleted'; j=os.path.join(c,'app','node_modules'); os.path.isjunction(j) and os.rmdir(j); shutil.rmtree(c,onexc=lambda f,p,e:(os.chmod(p,stat.S_IWRITE),f(p)))" "$CODE" "$DATA"
```

If the delete fails (a file is held open), in this order:

1. Clone the release into `$CODE-new` (the clone command below with that path), stop what runs with that copy's
   `stack.py ... stop` (the same command as above, its path), and run the delete again.
2. If it still fails, set the old folder aside and clone into the path. Tell the user the old folder can be deleted later:

```
& "$PY" -c "import os,sys,time; c=os.path.normpath(sys.argv[1]); o=c+'-old-'+time.strftime('%Y%m%d-%H%M'); os.rename(c,o); print('the old copy is now '+o+' and can be deleted later')" "$CODE"
```

3. If that fails too, say which program holds the folder (a window of it, or the app itself), ask the user to close it or
   sign out and in again, and stop. Nothing of theirs is changed.

**Clone.** `<TAG>` is the tag the command above printed. If step 1 already made `$CODE-new` and the delete then worked,
move it into place instead (`& "$PY" -c "import os,sys; os.rename(sys.argv[1], sys.argv[2])" "$CODE-new" "$CODE"`).

```
git clone --depth 1 --branch <TAG> "$REPO" "$CODE"
```

Then stop whatever the old install may still have running, with the new code (the process files are in the data folder, so
this works whatever the old environment was; it changes nothing when nothing runs):

```
& "$PY" "$CODE/app/scripts/local/stack.py" --data "$DATA" stop
```

## 4. Install it

`stack.py` makes the Python environment, installs the locked packages and builds the page (a few minutes; the one time
Node is needed). It runs the migrations when the app starts, on the data that is there. This is the only step that calls
it by path.

```
& "$PY" "$CODE/app/scripts/local/stack.py" --data "$DATA" setup
& "$RB" --version
```

Ignore setup's advice to edit settings by hand; `resumebot init` makes them. If setup stops with a sentence, fix that and
run it again (it is safe to repeat). If it says a port is taken, and the program holding it is a `python` or `node`
process whose path is in the code folder (or in a `-old-` folder set aside above), it is this install's own leftover:
end that process (`Stop-Process -Id <pid>`; sh `kill <pid>`) and run setup again; any other program is not ours:
tell the user which one and stop. `--version` must print `resumebot` and a number; if not, stop and say what it printed.

**Check the SQLite:** `& "$VPY" -c "import sqlite3; print(sqlite3.sqlite_version)"`. Safe is 3.51.3 or later, 3.50.7 or
later below 3.51, or 3.44.6 or later below 3.45. Otherwise say "This copy of Python carries a SQLite with a known bug
that can damage your data, so I will not go on. I will install the right one and start over.", run
`uv python install 3.12 --upgrade --no-bin`, delete the code folder as above, clone again, run `setup` again. Do not run
`init` or start the app until it is safe.

**Put `resumebot` on the user's PATH** (user only, never the machine; safe to run again, it adds nothing twice).
Windows, in PowerShell (not `setx`):

```powershell
$dir = "$env:LOCALAPPDATA\Programs\resumebot\app\backend\.venv\Scripts"
$old = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($old -split ';') -notcontains $dir) { [Environment]::SetEnvironmentVariable('Path', "$old;$dir", 'User') }
```

macOS and Linux: add `export PATH="$HOME/.local/share/resumebot-app/app/backend/.venv/bin:$PATH"` to the shell profile
unless it is there. Read the stored value back to confirm. It reaches new terminals and the restarted Claude Code; this
shell keeps using `$RB`.

## 5. The rest: one command each

From here every step is `resumebot <verb>`, run as `& "$RB" <verb> ... --data "$DATA"` (sh: `"$RB" <verb> ... --data
"$DATA"`). Pass `--data "$DATA"` on each: this shell may not have `RESUMEBOT_DATA_DIR` yet; after the restart it is not
needed. Each verb is safe to run twice.

| Step | Verb and arguments | What it does |
|---|---|---|
| Make the settings, **only if the look found no settings** | `init --owner "<name>" --data "$DATA"` | settings, secrets, the database, the machine's time zone; sets `RESUMEBOT_DATA_DIR` for the user. It never rewrites settings or secrets that exist and never touches a database that is there. It refuses a blank name or a bad zone: fix the input (if it cannot tell the zone, pick the IANA name `Area/City`, say which, pass `--timezone`) |
| Start it | `start --data "$DATA"` | starts the app and the five lanes now, outside this session, and waits until it answers; if the app already runs it says so and changes nothing; it also removes the sign-in task an older install made |
| Check the lanes | `smoke --lanes --data "$DATA"` | read-only checks of the app and the five lanes |
| Sign the agents in | `signin --data "$DATA"` | see below; says "already signed in" and changes nothing when a token is stored |
| Connect to this Claude Code | `connect --data "$DATA"` | removes, then adds, both doors `resumebot-owner` and `resumebot-browser` (tokens masked in its output), so a stale registration is replaced, and checks each answers |
| Put resumebot on the desktop | `shortcut --data "$DATA"` | on macOS writes `resumebot.command` (a shell script Finder runs in a Terminal window) on `~/Desktop`; on Windows writes `resumebot.lnk` on the user's desktop (found as Windows' own Desktop folder, so a OneDrive one works), replacing an earlier one; double-clicking it starts the app if needed and opens its page, with no console window. On macOS and Linux it says it is not built and exits 0 |
| Check everything | `doctor --data "$DATA"` | one line per piece, `ok` or `MISSING` with what to do |
| Final check | `status --for-claude --data "$DATA"` | prints the plain status lines; changes nothing |

Run `shortcut` after `connect`, in a fresh install and in an update alike. Never start the app with `stack.py start`
from this session: an app started there stops when Claude updates or closes. `start` runs it outside. If `smoke` fails, read the
logs `doctor` names and fix that before going on.

**Sign in.** Run `& "$RB" signin --data "$DATA"` in the background (in Claude Code, `run_in_background`). The answer is the
check: if it prints `already signed in` (a token is stored, whether this flow or the first one made it), nothing was
changed and there is nothing to do. Otherwise tell the user, in one sentence: "A browser tab opened: approve the sign-in
there." It runs `claude setup-token` out of sight and stores the token itself, so the token never passes through you:
never ask for it, never read `secrets\claude-oauth-token`, never let the user paste a token into this chat. Wait until it
prints `signed in`. It prints one line with a link first. If the user says no tab opened, give them that link; if its page
shows a code, they paste the code into this chat and you run `& "$RB" signin --data "$DATA" --code <code>`, which prints
`signed in` or the reason. The token lasts about a year; when the agents stop signing in, `signin --again` signs in anew
and replaces the token only if that works. Browser jobs are off by default and are not part of this.

**Connect.** The doors appear in Claude Code after it restarts. (When the `getting-started` agent runs this skill, do not
say so: the agent says it after it has written the helper agents into the project.)

## 6. Done when

`doctor` prints no MISSING line and `status --for-claude` says the app is running. Then say one line to the user: what
you did ("updated to <version>; your data was backed up to <folder> and kept"), and what is left (restart Claude Code,
and anything `doctor` still names). Mention an `-old-` folder if you set one aside, and the backup folder, which can be
deleted once they are happy with the update. When the `getting-started` agent runs this skill, stop there and return to
it: it writes the helper agents, asks for the restart and does the hand-over. Otherwise tell the user to restart Claude
Code (the new session's start-up status is the next step), then hand over to `resumebot-setup:onboarding` and say in one
sentence what its first step is.

If a step fails, say which, what the command printed (no secrets) and what you will try next. Every step can be run
again from the top: the look finds what is done. To go back after an update, stop the app, and copy the backup folder's
contents over the data folder (ask the user first). To back out entirely: `stack.py stop`, delete `resumebot` from the desktop, `claude mcp remove resumebot-owner --scope user` and the same for `resumebot-browser`, and delete
`$CODE`. Delete `$DATA` only if the user asks: it holds their work.
