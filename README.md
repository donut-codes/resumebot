# resumebot

resumebot is a job search where every step is visible. It runs on your own PC. Agents (Claude Code sessions on your own
Claude subscription) find roles on public job boards and employers' career pages and score them against your own match
profile. The app de-duplicates them, applies your hard gates, and keeps every role: kept, or screened out with the
reason. For the roles you choose it builds a tailored resume and cover letter, and it exports to a spreadsheet. It
applies to nothing and submits nothing: you apply.

Every target, gate, weight, place and pay figure is yours, set in the app. It is alpha software.

## Quick start

You need a Windows PC or a Mac, and [Claude Code](https://claude.com/claude-code) on a paid Claude subscription. Your Claude does
the install. Pick one route.

**Route 1: the plugin.** In Claude Code:

```
/plugin marketplace add EricWurth/resumebot
/plugin install resumebot-setup@resumebot
```

Then tell Claude:

```
set up resumebot
```

**Route 2: a clone.** In a terminal:

```
git clone https://github.com/EricWurth/resumebot
cd resumebot
claude
```

Then tell Claude: `set up resumebot`. The clone only gives Claude the install instructions: [`AGENTS.md`](AGENTS.md)
points it to the install skill. The app itself is installed at a release, in its own folders, not in the clone.

**Route 3: an empty project folder.** Make a folder, start `claude` in it, install the setup plugin as in route 1 and say
`get me started`. The `getting-started` agent does the install, then writes three helper agents (a career coach, master
resume help and a network-profile draft) into that folder with `resumebot project`; the app itself stays in its own
folders.

Either way Claude asks you for one yes and the name your roles are kept under, installs what is missing, downloads the
app, starts it, and registers it with Claude Code. It also signs the agents in: you approve once in the browser that
opens, and nothing is typed or pasted. Restart Claude Code when it says so, then say `continue onboarding`: Claude
walks you through your resume, your targets and your first search. The app opens in your browser at `http://localhost:8430`.

The install takes a few minutes, most of it downloads and the one-time build of the page. [`docs/install.md`](docs/install.md)
has every step.

## Updating

Tell Claude:

```
update resumebot
```

Claude backs up your data folder, replaces the app with the newest release, finishes any setup step that was never
done, and keeps all your data and settings. If the agents are not signed in yet, approve once in the browser.

**If you installed 2.4.0 or earlier,** first give Claude the new instructions. This repository's history was replaced
at 2.5.1, so an old copy cannot simply pull:

- **Route 1 (the plugin):** in Claude Code, run `/plugin marketplace update resumebot`, then say `update resumebot`.
- **Route 2 (a clone):** delete the clone folder, clone again as in route 2 above, start `claude` in it and say
  `update resumebot`. The clone holds none of your data.

## What you need

- Windows 10 or 11, or macOS, on an ordinary user account. No administrator rights are needed.
- Claude Code, installed and signed in, on a paid Claude subscription. The agents run on that
  subscription and never on an API key.
- `git`, `uv`, Node 22 or newer and Chrome. Claude installs the first three when they are missing: with `winget` on
  Windows, with Homebrew on a Mac (Homebrew itself you install first). Chrome is only for the optional browser jobs.

## Where things live

| What | Windows | macOS |
|---|---|---|
| The app's code | `%LOCALAPPDATA%\Programs\resumebot` | `~/.local/share/resumebot-app` |
| Your data: settings, secrets, the database, logs | `%LOCALAPPDATA%\resumebot` | `~/.local/share/resumebot` |
| Resumes and cover letters it builds | `Documents\resumebot`, one folder per role | `~/Documents/resumebot` |

The code is replaced when you update; your data never is. Nothing leaves your PC except what the agents send to Claude,
the public pages they read, and what you turn on yourself (a mailbox the app reads, read-only; a backup folder you
name). See [`docs/privacy-and-terms.md`](docs/privacy-and-terms.md).

## What it does

- Reads employers' public job feeds and career pages, and scores each role against your profile.
- Keeps every role it finds, with the reason it was kept or screened out.
- Builds a tailored resume and cover letter for the roles you choose, from your master resume and nothing else.
- Shows all of it in one page: Pipeline, Employers, My Resume, Search Settings, Notifications and Agent Settings.
- Lets your own Claude run the app's actions for you through a local connection, which is how the onboarding works.

## What it never does

- It never applies, signs in for you, fills in a form or solves a CAPTCHA. The Go button opens your Ready to Launch
  roles in tabs and stops there.
- It never rewords your master resume. Changes to it come only from you.
- It never drops a role silently: a screened-out role stays, with the gate that stopped it.
- It never sends your data anywhere except to Claude, as part of the agents' work.

## Starting it later

The install puts **resumebot** on your desktop (on a Mac the file is `resumebot.command`, and a Terminal window opens while it runs). Double-click it: it starts the app if it is not running and opens its page. Pin it wherever you like from there. More in [`docs/install.md`](docs/install.md).

## Honest limits

- **Windows first, macOS on this branch.** The app is built and released on Windows. This branch (`mac-compat`) also
  runs on macOS: the checks pass there and the app was started and smoke-tested on one Mac (Apple silicon). A real
  search with signed-in agents has not been run on a Mac yet. There is no Linux install.
- **United States searches.** Pay is dollars a year; numbers and dates are written the United States way.
- **Alpha.** Expect rough edges. There is no installer, no first-run wizard in the page, and no `update` command:
  updating is the same ask as installing, `set up resumebot`: Claude backs up your data folder, replaces the app's code
  with the newest release and keeps your data ([`docs/install.md`](docs/install.md)). It also finishes an install that
  stopped half way.
- **No automatic backup.** `scripts/backup_local.py` makes one when you run it.
- **Site terms are your risk.** Some job sites and job systems forbid automated reading in their
  terms. A read even though a site's `robots.txt` asks automated readers not to exists only in the owner's own copy and is off
  until the owner turns it on; the public copy ships no reader for a site whose terms forbid it, and career pages hosted on job
  systems whose terms allow it or are silent are read by default.
  Read [`docs/privacy-and-terms.md`](docs/privacy-and-terms.md).
- **Your Claude allowance does the work.** A search, an analysis and a packet each use it. Claude's peak hours and usage
  limits hold the agents; the app says so when they do.

## The documentation

| | |
|---|---|
| [`docs/install.md`](docs/install.md) | the install in order, where everything goes, `resumebot doctor` and `status`, updating, removing |
| [`docs/connect-claude.md`](docs/connect-claude.md) | the two connections to your Claude Code, and the agents' sign-in |
| [`docs/onboarding.md`](docs/onboarding.md) | what the guided first run covers, and how to do each step in the page instead |
| [`docs/agent-guide.md`](docs/agent-guide.md) | for your Claude, working as the app's assistant: the tools, the rules, the daily routine |
| [`docs/privacy-and-terms.md`](docs/privacy-and-terms.md) | what it reads, what it never does, where data stays, licence and warranty |
| [`docs/architecture.md`](docs/architecture.md) | how it is built, in a page |
| [`docs/browser-reader.md`](docs/browser-reader.md) | the optional browser jobs, for sites a server cannot read |
| [`GLOSSARY.md`](GLOSSARY.md) | the app's vocabulary |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | running the checks and the rules for changes |

## What is in this repository

```
README.md, AGENTS.md, CONTRIBUTING.md, GLOSSARY.md, LICENSE
.claude-plugin/marketplace.json     the marketplace that lists the setup plugin
plugins/resumebot-setup/            the plugin: the install skill and the onboarding skill
app/
  backend/                          the app: FastAPI and one SQLite file
  frontend/                         the page: React, built at install
  scout/                            the Node runner that starts the five agent sessions
  playbooks/                        the BPMN files that set the order of the work
  instructions/, templates/         what the agents are told and the documents they fill in
  scripts/                          stack.py (start, stop, smoke) and dev.py (the checks)
  packages/process-engine/          the process engine, vendored
scripts/                            backup and restore, and the checks that guard this repository
docs/                               the documentation above
```

## The setup plugin

What the marketplace lists, generated from the plugin's manifest.

<!-- catalog:start -->

1 plugin, 2 skills.

They split on one axis: who can invoke them. **User-invoked** skills are reachable only when you type them, and they do the orchestrating. **Model-invoked** skills can be typed *or* reached for automatically when the task fits; they hold the reusable discipline. A user-invoked skill may call a model-invoked one, never another user-invoked one.

*Chat* marks what works outside a coding harness. Skills get a filesystem and bash everywhere, and anything calling for parallel agents simply runs in sequence instead. Only hooks, slash commands, and MCP servers have no counterpart. On claude.ai network access varies by account, so a skill doing live web research may not work for every reader.

### Plugins

Installed through the marketplace. Each carries more than instructions: extra skills, agents, hooks, or scripts.

| Plugin | Chat | What it does |
|---|:--:|---|
| [`resumebot-setup`](plugins/resumebot-setup) | — | Sets up the resumebot app on your computer and connects it to your Claude Code, then walks you through onboarding: ask Claude to "set up resumebot" |

**resumebot-setup**

- *Automatic*: `install-resumebot`, `onboarding`

<!-- catalog:end -->

## License

MIT. See [`LICENSE`](LICENSE). The bundled DejaVu fonts carry their own licence, in the folder beside them.
