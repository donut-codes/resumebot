# Architecture

How the app in `app/` is built, in a page. It names the parts and what each is responsible for, not every file: the code
moves, and these responsibilities do not. The app's words (role, gate, mission, packet and the rest) are in
[`../GLOSSARY.md`](../GLOSSARY.md).

## The parts

```
 your browser
      |
 page program (FastAPI, :8430)      the page, every read and click, the live stream, the owner door
      ^      |
      |      |  one small datagram "look" up, a bell down: hints, never the record
      |      v
 engine program (FastAPI, :8530)    the schedule, the work queue, the fetchers, the process engine,
      |        \                    the doors the agents use (scout and browser)
 SQLite file    five lanes (Node)   each starts headless Claude Code sessions on your own subscription
 (WAL, local)                       and talks to the app only through the engine's doors
```

Everything runs on your PC, bound to `127.0.0.1`, started and stopped by one launcher, `app/scripts/local/stack.py`. No
database server and no container is involved.

- **The page.** React, Vite and TypeScript (`app/frontend/`), built once at install and served by the page program. It
  holds no rules: it shows what the API sends and sends back what you changed. One screen each for My Resume, Search
  Settings, Pipeline, Employers, Notifications and Agent Settings.
- **The page program** (`app/backend/app/jobsearch/page.py`). Serves the page, every read, every click of yours, the live
  stream and the owner door. It runs no schedule and no work, so the page keeps answering while the engine is busy,
  stopped or restarting.
- **The engine program** (`engine.py`, the page's port plus 100). Runs the schedule, the work queue, the fetchers that
  read public job pages, and the doors the agents use. When it commits something you can see, it sends the page one small
  datagram (ids, never row data), and the page reads the rows and pushes them to the open screens. A hint can be lost, so
  nothing that matters travels only by hint: the record is a row first, and one `resync` makes the screens read again.
- **The database.** One SQLite file in your data folder, in WAL mode. Each program writes one transaction at a time, and a
  transaction is held only for its database work, never across a network read. The schema is Alembic's
  (`app/backend/migrations/`), applied at start; migrations only go forward. One engine runs on a file at a time: a second
  start on the same data folder is refused. Backups are made with SQLite's online backup (`scripts/backup_local.py`).
- **The five lanes** (`app/scout/`). Node runners that each wait for work and start a headless Claude Code session when the
  app has some: one for searching, two for analysis, one for the owner's own asks and one for writing. A session gets only
  the app's door and its instructions (`app/instructions/`), no files and no shell.
- **The doors.** MCP servers on the app. The scout door and the browser door are the agents'. The owner door is yours: four
  tools, `actions.list`, `act.read`, `act.do` and `act.job`, through which your own Claude runs the app's actions. See
  [`connect-claude.md`](connect-claude.md) and [`agent-guide.md`](agent-guide.md).
- **The browser bridge** (`app/scout/own-chrome.mjs`, started by the lanes' launcher on 127.0.0.1). Every browser session and
  the app's own browser code reach the app's Chrome only through it. It serves only resumebot's own clients: a request
  must carry the app's secret key (`secrets/bridge-key` in the data folder, made once when the lanes first start, read
  by the sessions and the app from the same file), in the `X-Bridge-Key` header or the address's `key` query; any other
  request, including another program on this PC, is refused with 403 and starts nothing.
- **The process engine** (`app/packages/process-engine`, configured in `app/backend/app/jobsearch/process.py`). Runs the
  BPMN files in `app/playbooks/`: finding roles, analysing one, building a packet, reading mail and the rest. The order of
  the work lives in those files. Agents do the judgement (finding, scoring, writing); everything else (de-duplicating,
  gating, ordering, exporting) is plain code.

## How a role moves

1. **Found.** The search lane's agent, and the fetchers reading employers' public feeds and career pages, report postings.
   The app de-duplicates them against what it already holds (the log of every posting it has read).
2. **Gated.** One check applies your hard gates (where you will work, the pay you need, employers and sites you avoid) to
   what is stored for the posting. A role a gate stops is kept as Closed, with the gate that stopped it.
3. **Ranked.** A role that passes is read and scored against your profile in stages: a first impression, then a closer
   second one for the best, as the Pipeline's columns show (Landing Zone, Cleansed Data, First Impression, Second
   Impression).
4. **Ready to Launch.** The best few, in the number you set, wait here. The Go button builds a packet for each, a tailored
   resume and cover letter written from your master resume and checked, and opens each role's posting in a tab. You apply.
5. **After.** You, or your mail (read-only, if you connect it), move a role to Applied and on to Active Discussion; a role
   ends as Rejected, Gone Quiet or Withdrew, each with its reason.

Standing work, such as scouting the boards, reading the employers' boards, reading mail and researching employers, is a
**mission** on the Agent Settings screen, with a schedule you can pause, change or run now.

## The layers of the backend

`app/backend/app/` holds the code. Under `jobsearch/`:

| Folder | What it does |
|---|---|
| `routes/`, `page.py`, `engine.py` | the API, split between the two programs |
| `missions/` | the schedule and each kind of mission, one file each |
| `gating/`, `fit/`, `pipeline/` | the gates, the scoring and the board's columns and moves |
| `postings/`, `employers/`, `adapters/`, `fetch/`, `sites/` | what is read, from where, and how politely |
| `browser/` | the browser queue and the app's own Chrome window |
| `resume/`, `writing/`, `profile/` | the master resume, the packets and your settings as saved versions |
| `owner_actions/` | the owner door's actions |
| `work/`, `pool/` | handing work out to the lanes' sessions |
| `research/line.py` | where a role stands in the analyst's line, and the one way it leaves it (a named cause) |
| `mail/`, `imports/` | the mailbox reader and the tracker and employer-list imports |

Rules the structure follows:

- A route owns its request and commits; services flush and never commit.
- The profile (criteria, gates, scoring, settings) lives in the database as saved versions, so any change can be restored.
- Deterministic work is code and judgement is the agents'. Do not move one into the other.
- Nothing in the app signs in to a site, applies, fills a form or solves a CAPTCHA.

## Sites are data, and the problem has the name

No site is named in code, comments, settings, API fields, tests, agent text or documents (the owner, 2026-11-15); each
says what the problem is: "a board whose roles must be followed to the employer's own site, its copy standing in until
then" (`follow_to_employer`), "a read even though robots.txt asks automated readers not to" (`robots`).

- **A runbook is one JSON per site** (`app/backend/app/sites/runbooks/`): its hosts, address rules, how to read it, its
  pace and stops, its mail senders. `app/backend/app/sites/registry.py` loads the folder once at start into an immutable
  registry and answers every question about a site (pace, stop, address, mail kind, verdict). Loading writes no row, and a
  runbook that fails to load never leaves a host at the generic pace: reads of it refuse with a plain reason.
- **A site is named in one place for its terms** (`app/backend/app/sites/site_terms.json`: name, hosts, terms address, date
  read, verdict `allows`, `silent` or `forbids`) **and in its own runbook**; nowhere else.
- **The one restricted folder** (`app/backend/restricted_runbooks/`) holds every runbook whose site forbids automated
  reading (`runbooks/`), the runbooks of hosts with no terms entry yet (`pending/`), the reader hooks and the board
  package (`code/`) and their tests (`tests/`). It is the only place the fields `robots`, `reader`, `signed_in` and `extras`
  are accepted, it never ships, and nothing in `app/backend/tests/` imports from it.
- **The guard** is `scripts/check_no_one.py` (rule `site-name`, inside `dev.py check`): it builds its names at run time
  from `site_terms.json` and the runbooks, scans file contents and paths, and fails only on a hit that is not in the
  shrinking baseline `scripts/site-names-baseline.txt` (a small permanent list, `scripts/site-names-allow.txt`, holds
  proven false positives). `--exported` mode allows nothing but `site_terms.json` and a shipped runbook for its own site.
- **The public copy** (`scripts/export_public.py`) drops the restricted folder, keeps only runbooks whose verdict is
  `allows` or `silent`, and installs from `db.create_schema` on SQLite: its migration chain starts at a no-op root revision
  (the newest migration whose code names a site) followed by every later migration unchanged.
- **One release.** The change that made all this was released once, as 2.3.0, with no intermediate cut points. The owner's
  install behaves as before; what changed is the names.

## Where things are

```
app/backend/    the app: routes, services, models, migrations, tests
app/frontend/   the page and its tests
app/scout/      the lanes' Node runner and its tests
app/playbooks/  the BPMN files
app/instructions/, app/templates/   what the agents are told, and the documents they fill in
app/packages/   the vendored process engine and process view
app/scripts/    stack.py (start, stop, smoke), dev.py (the checks), the helpers
plugins/resumebot-setup/   the install and onboarding skills
scripts/        backup, restore, and the repository's own checks
```

[`../CONTRIBUTING.md`](../CONTRIBUTING.md) says how to run the checks.
