# Provenance: what was copied from Porter, and from where

The app was built inside Porter and moved here on 2026-10-07. Everything under `app/` that is not new was copied from
Porter's `origin/main` at **`4482318a`** (Porter 0.167.2). Nothing imports Porter:
the copy stands alone and Porter keeps its own. A later fix in Porter reaches this repository only when someone carries it
over. To see what changed in a copied module since this copy, in a Porter checkout:

```
git log --oneline 4482318a..origin/main -- backend/app/harness        # or any path in the tables below
git diff 4482318a origin/main -- backend/app/harness/loader.py
```

How to read the tables: "Porter commit" is the last commit that touched that file before the copy. Python imports kept their
`app.` prefix, so a Porter file drops in with no renaming. Where a file was edited here, "Changed here" says what, and those
edits have to be re-applied when the file is replaced. Paths are the ones in this tree; if a module has moved inside the app,
find it by its name. Most of the app was written after the copy and is not listed: only what came from Porter is.

## Vendored packages (Porter `packages/` to `app/packages/`)

The process engine and the process view were taken whole from Porter at **`8cdd383e`** (Porter v0.182.3; first vendored at
`fb68150c`, Porter 0.169.1): `git archive` of `packages/process-engine` and `packages/process-view`, with their tests,
nothing edited. resumebot is a fork and owns these packages now (owner, 2026-10-14). They are changed here, and no change
waits for Porter. Each change made here is a divergence from the Porter commit it started from, and is recorded in this
section: what changed, in which file, and why. Divergence 1 (`fix/stranded-instances`): `process_engine/engine.py` and `errors.py` move a run whose own file can no longer load (`Stranded`, an `EngineError`) onto the current file instead of failing it: `Engine._live_def`, `_move_to_current`, `move_stranded` (run at start-up, `jobsearch/loops/startup.py`); a release that removed an instruction file had left every older run unable to submit. The dialect-neutral engine on
branch `feat/engine-portable` is recorded here when it merges. Divergence 2 (`fix/engine-races`, 2026-10-21, tests only, no engine code): `packages/process-engine/tests/test_races.py` opens the connections of the sessions it waits on before the request starts (`connected`), and `wait_until_blocked` takes the request's task, fails at once if it ends, and allows thirty seconds instead of five; a request that was still making its connection (slow on Windows) was not yet waiting for a lock, so two tests failed intermittently with "nothing is waiting for a lock". The engine's locking was checked and is correct. Divergence 3 (`fix/app-log-errors`, 2026-10-27): `Engine.move_stranded` sweeps only runs of the processes its own library holds (`ProcInstance.process_key in library.keys()`); the job-search engine's start-up sweep had taken the site projects' runs (a library of their own) for stranded and logged an error for each at every start. `jobsearch/loops/startup.py` now sweeps the site projects' runs with the site engine. Divergence 4 (`fix/process-view-framing`, 2026-10-28, `packages/process-view`): the Process flow board was an empty dotted canvas in a hidden or throttled tab, and its fit was poor. New `src/framing.ts` (`frameWhenMeasured`, `fitWhole`, `nodesMeasured`): the first fit reads each node's measured size from React Flow's internal node (`getInternalNode(id).measured`; `getNodes()[i].measured` is never set on a controlled node, so the old wait always ran its 41 frames), and a 400 ms timer shows the board whatever the tab does (the reveal had been only inside a `requestAnimationFrame` loop, which a hidden tab never runs, so the viewport stayed at opacity 0); a fit made on unmeasured nodes is made again when the tab becomes visible. The fit is computed from the board's own size and the nodes' bounds (`getViewportForBounds`, zoom 0.05 to 1.5, padding 0.12, instead of `fitView` at 0.15 with no ceiling, which zoomed small graphs to 1.6x and clipped them) and each node is now given its drawn `width` and `height`, so the bounds exist before any measurement. `LivingFlow.tsx`: a live graph is fitted again when its first positions arrive (until then the circles stand where dagre put them, so the first fit was of the wrong shape), and entering or leaving full screen fits the whole graph in the same commit instead of re-anchoring on the first or selected step (`homeId` is gone from `LivingFlow` and from `ProcessView`'s call). `livingGraphKit.tsx`: a step's number breaks after each dot and is sized to its node (`numberFontPx`, `--pw-num-fit` and `--pw-num-w`, used by `processView.css` and `styles/focus/process-view.css`); "4b.2a.2b.4c.1" had overflowed its square. `FLOW_MIN_ZOOM` moved to `framing.ts` and is still exported from `LivingFlow`. Tests: `packages/process-view/src/__tests__/framing.test.ts` and three in `frontend/src/components/agents/ProcessView.test.tsx`; `styles/focus/agents.test.ts` allows the two new local properties. Divergence 5 (`chore/w1b-process-engine-trim`, 2026-10-31, `packages/process-engine`, the codebase cleanup): the engine's shadow mode and proof gate are removed, because this app runs every process live and never created their tables. Gone: `gate.py`, `report.py` and `shadow.py`; the tables `proc_shadow_checks`, `proc_modes` and `proc_gate_runs` (their models `ProcShadowCheck`, `ProcMode` and `ProcGateRun`), which no migration here ever made; the `SHADOW` constant and `models.owner_id`; the config fields `scenario_runner`, `session_factory` and `live_needed` (and the `ScenarioRunner` protocol); in `engine.py` the `mode=` argument of `start`, `start_by_message` and `_start`, the `shadow_resolver`, `shadow_call_resolver` and `live_call_resolver` arguments of `Engine`, `Engine.compare_decision`, `_service_outcome`, `_call_followed_elsewhere` and the `shadow_action` waits (`proc_instances.mode` stays, always `live`; `complete` takes an error only for a `deferred` step, now worded "only a deferred service step"). `drive.py` is trimmed and renamed `live.py`: it keeps `register_drivable` (key, label, library, handouts, registry, deferred, retry, plain), `attempt`, `restore`, `inside`, `ensure_ok`, `finished`, `dispatch`, `live_engine`, `open_live`, `last_end`, `Drivable`, `Attempt`, `DriveFailed` and `DriveError`; `mode_row`, `mode_of`, `drives`, `set_mode`, `fall_back`, `current_hash`, `calls_of`, `callers_of`, `drivable_keys`, `last_live`, `open_by_data` and the `replay`, `followed`, `code_runs` and `live` fields of `Drivable` are gone. `attempt` with no `on_drive_failure` set no longer falls back to shadow: it undoes the work, logs and returns `Attempt(ok=False)`. `process_engine/testing/` (the sample processes, the fake clock) moved to `tests/support/` with its two fixtures, since only the package's own tests used it. Tests removed with the behaviour (360 collected before, 311 after): `test_shadow.py` and `test_gate_legs.py`, the set-mode and fall-back cases of `test_drive.py`, the mode asserts of `test_drive_failure.py` (the two files are now `test_live.py`), three set-mode cases of `test_races.py`, the shadow-run case of `test_runtime.py` and the scenario-runner and drive-owner cases of `test_extension_points.py`. Divergence 6 (`fix/stranded-site-round`, 2026-10-31, `packages/process-engine`): an action can end its own run. `errors.RunOver` is new, and `Engine._service` ends a run whose action raised it as cancelled, with the message as the reason, running nothing after the step (an `ActionError` with no boundary fails the run, which is wrong for a run that has simply nothing left to do). It is for a run moved onto a new file by `move_stranded` whose work was settled before it arrived: the project round's step PR-6S (`round.start_plan`) raised `PlanEngineError` for a plan the owner had declined under the removed plan question, the run stayed stranded and the error came back at every start. Test: `test_an_action_that_says_the_run_is_over_ends_it_cancelled_and_nothing_after_the_step_runs` in `packages/process-engine/tests/test_engine.py`. The same divergence: `Engine.move_stranded` logs any other exception a moved run raises, with the instance id, and goes on to the next run (it caught only `EngineError`, so on the live update to 1.166.0 the first run's `PlanEngineError` stopped the sweep for the two behind it; the `Stranded` and `EngineError` handling is as it was). Test: `test_one_stranded_run_that_raises_does_not_stop_the_sweep_for_the_others` in `packages/process-engine/tests/test_files.py`. Divergence 7 (`port/sqlite`, 2026-11-07, `packages/process-engine`, SQLite port tasks B1 and B5): the engine's tables use dialect-neutral types. `process_engine/types.py` is new (`UtcDateTime`, a `TypeDecorator` that stores UTC and returns aware times; `utc_now`); in `base.py` and `models.py` `UUID(as_uuid=True)` is `Uuid`, `JSONB` is `JSON`, `DateTime(timezone=True)` is `UtcDateTime`, and each `server_default=func.now()` is `default=utc_now` (`onupdate=utc_now` for `updated_at`). Tests: `backend/tests/core/test_sqlite_schema.py`. Divergence 8 (`port/sqlite`, 2026-11-07, `packages/process-engine`, SQLite port task D4): the engine runs on SQLite, and no module but the new `process_engine/portable.py` names a dialect. `portable.py` holds `insert_ignore` (`INSERT ... ON CONFLICT (columns) DO NOTHING`, built with the `insert` of the session's dialect, SQLite or Postgres), `for_update` and `get_locked` (the row locks: `FOR UPDATE [SKIP LOCKED]` on Postgres; on SQLite SQLAlchemy leaves the clause out, and that is right because there is one writer, whose transaction begins `BEGIN IMMEDIATE`, so no request holds a row another has to wait for or skip) and `AutoKey` (`proc_history.id`: `BIGINT` on Postgres, `INTEGER` on SQLite, the only column SQLite counts for). In `engine.py` the two `pg_insert` calls (`keep_files`, `_keep_file`) are `portable.insert_ignore`, and the five row locks (`_lock`, `_try_lock`, `_on_child_end`, `_cancel_children`, `_end`) go through `portable`; `models.py` takes `AutoKey` for `ProcHistory.id`. What a host owes the engine on SQLite is in `portable.py` and the README: begin every write transaction `BEGIN IMMEDIATE` and set `isolation_level = None` on connect (otherwise a savepoint opened after reads only is the outermost one, and its release commits the caller's whole transaction). The package's tests run on a SQLite file (`tests/conftest.py` sets it that way; the Postgres set-up is gone, and `asyncpg` with it from the package's dev dependencies). Tests removed with the behaviour they pin, which SQLite does not have: `test_races.py::test_tick_does_not_wait_for_an_instance_another_request_holds` (a row another request holds, and `tick` leaving it for the next tick) and `::test_an_answer_to_a_sub_process_locks_the_parent_before_the_sub_process` (the order of row locks, read from `pg_stat_activity`); the two tests of a locked read being fresh now end the request that read the copy first (a read that stays open holds the database), and two twins put one request behind another. New: `tests/test_portable.py` (11 tests). Count: 313 before, 324 after. Not done here: the app's own Postgres code (the port's other tasks). Divergence 9 (`perf4/deck-stages`, 2026-11-09, `packages/process-engine`, the writer-turn survey): `versions.refresh` reads the saved versions as plain rows (`entity_id`, `process_key`, `number`, `xml`) and, when the session it was given held nothing (no transaction, nothing added, changed or deleted), ends that transaction before the files are parsed and checked (`_build`, 0.26 s of 0.33 s on a copy of the real data), so the writer's one turn is held for the read alone; a session holding work of its own keeps its transaction. `_build` takes any rows with those fields. Tests: `packages/process-engine/tests/test_versions.py` (`test_refresh_parses_the_files_after_it_has_ended_the_transaction_it_began` and `test_refresh_leaves_a_transaction_that_holds_work_as_it_was`). Divergence 10 (`split/int1`, 2026-11-11, `packages/process-engine`, the split into a page program and an engine program): `versions.current_for(session, entity_id)` reads the process, number and file hash of every version an owner has saved (no file text) and calls `refresh` for that owner only when they are not what the library was last filled from; it keeps a note of what it read for each owner (`_seen`) beside the library table the note was true of (`_seen_in`), so any swap of the tables drops the notes. The two programs share one database and no memory, so each reads the versions before it uses them and neither tells the other. It never ends the caller's transaction. `loader.py` changes a comment only. Tests: `packages/process-engine/tests/test_versions_current_for.py` (5 tests). Divergence 11 (`split/join-13-26-d`, 2026-11-11, tests only, no engine code): the two autouse fixtures of `packages/process-engine/tests/conftest.py` (`default_config`, `restore_drivables`) act on the package's own tests only, and `default_config` puts back the settings and owners' versions it found instead of leaving the defaults. pytest gives a conftest outside its root folder to every test of the run, so in one process with the app's tests (root folder `backend`) the fixture reset the engine's config around every app test, and the app's process files no longer loaded.

A Porter fix is still carried over on purpose, when someone decides it, and recorded the same way; nothing is carried over
by assumption. To see what Porter has changed since the copy, in a Porter checkout:

```
git log --oneline 8cdd383e..origin/main -- packages/process-engine packages/process-view
git diff 8cdd383e origin/main -- packages/process-engine packages/process-view
```

To re-vendor a Porter commit, take the files from it with `git archive` as above, re-apply each divergence recorded here
(or drop the ones Porter now has), and record the new commit here. The packages import nothing from an app, so this
repository's edits to the app's own code live outside them, in the table below:

| Package | Used how | This app's own part |
|---|---|---|
| `packages/process-engine` (distribution `process-engine`, import `process_engine`) | `app/scripts/local/stack.py setup` puts its `src` on the app's environment (a `.pth` file), as `dev.py` does for the checks; its own tests run with `dev.py check packages/process-engine` | `app/jobsearch/process.py` configures it once with `process_engine.config.configure(...)`: the roles, the instruction source (`app.harness.instructions`) and the owner key. The job-search actions are in the engine's default registry (`app/jobsearch/actions.py`); the site projects' have a registry of their own. The engine keeps each run's file text (`proc_files`), its versions (`proc_versions`) and its tables on its own `process_engine.base.Base`; `migrations/env.py` lists both metadatas and keeps to `OWNED_TABLES` |
| `packages/process-view` (React, `"process-view": "file:../packages/process-view"`) | `app/frontend/package.json` takes it by path; `vite.config.ts` dedupes react and `@xyflow/react` and runs the package's tests with the app's, leaving out five that read Porter's playbooks; `tsconfig.json` includes its source | `src/components/agents/harnessProcess.ts` (How they work's steps become the view's step and edge shapes) and `src/styles/process-view.css` (square corners, a 1px rule round the decision criteria box, circles kept for the notation) |

## Copied modules (Porter `backend/app/` to `app/backend/app/`)

#### `backend/app/`

| File | Porter commit | Changed here |
|---|---|---|
| `__init__.py` | `1208adcc` | unchanged |
| `config.py` | `d2f72487` | trimmed to the paths, the database URL and the MCP host settings; Porter's household settings removed; no default database URL (the app stops at start without one) |
| `database.py` | `84b8e118` | the Clock's second engine removed; only `Base` is used; wave 5 (BA-19): `one_of`, which builds the CHECK a closed list of words becomes |
| `errors.py` | `2f6d7118` | unchanged |
| `textutil.py` | `b4f9dc61` | unchanged |

#### `backend/app/agents/`

| File | Porter commit | Changed here |
|---|---|---|
| `__init__.py` | `f188014d` | unchanged |
| `security.py` | `5d33fbe2` | `SecretRejected` is also a `DomainError`; `cap_length`'s refusal is a `DomainError` too; `find_secret` judges a long token by its alphabet's entropy, not a flat 4.0 bits; the Porter token and wake-secret makers and `validate_credential_ref` are gone: only `hash_token`, the never-list and `cap_length` are used |

#### `backend/app/harness/`

| File | Porter commit | Changed here |
|---|---|---|
| `__init__.py` | `a545d8a2` | unchanged |
| `instructions.py` | `0f7fe2cb` | `UnknownName` and `MissingPlaceholder` are also `DomainError`s; `read_kind` takes no household packs |
| `kinds.py` | `ae0ad1c4` | `planner_vocab` and `PORTER_MADE_KINDS` are gone |
| `loader.py` | `df52e6e3` | `HarnessError` is also a `DomainError`; skills, goal plugins, household packs, `rate_limit` and the unused constants are gone |
| `where.py` | `harness/where.py` in `8cdd383e` | the connection's skills are gone; the step ids are the site loop's; the step's instruction is read through `instruction_files.read` (the owner's saved version, else the file on disk) |

Porter's `harness/limits.py` and `harness/words.py` were not kept. The check that the plan template's limits block matches
the code is a test, `tests/test_plan_limits.py`.

#### `backend/app/jobsearch/`

This directory was the job-search app inside Porter (last touched at `bd64cb20`: `__init__`, `actions`, `cli`, `db`,
`gates`, `main`, `mcp_server`, `models`, `process`, `profile_import`, `queue`, `run`, `settings`, `tools`, `tracker`, `wake`).
It has been rewritten and extended so far that the per-file record would mislead, so none is kept: treat the directory as
this app's own code. `polite.py` and everything else in it that is not in that list was written here.

#### `backend/app/mailmine/`

Porter's household mail miner, cut to the reader. What stays is what the email path reads the owner's mailbox with
(the product requirements 14.10): the read-only IMAP connection, the mbox stream and the message parsing. Go's mail check reads
through it, unchanged (`jobsearch/mail/reader.py`: application confirmations before the apply tabs open).

| File | Porter commit | Changed here |
|---|---|---|
| `__init__.py` | `09afd9b4` | docstring |
| `imap.py` | `176b64f2` | the docstrings and the refusal text no longer name Porter, the household, the miner or the worker; `read_headers` searches from a day before `since` (the server's `SINCE` is the day it filed a message, and the owner's day for it can be the next; the callers keep only what is from `since` on), clock audit 2026-11-07 |
| `mbox.py` | `31155ee2` | unchanged |
| `message.py` | `d03829d6` | cut to the generic reading: `Seen` is the sender, the subject, the day, the start of the text and whether it is bulk mail (and, for the mail sync of path 14.10, the message's own id, the sender's address and the text of an attached calendar invite, the one attachment that is read); the household judgement (signal words, amount, renewal, the dates a message states) is gone, and the sender and bulk-header lists are constants in the module; `scrub_quote` and the hostile-input bounds are as they were; the message's day is the owner's calendar day (`owner_zone()`, a `-0000` header read as UTC), not the day on the sender's clock, clock audit 2026-11-07 |
| `providers.yaml` | `7be6f48b` | wording only: the owner's inbox, an app password called `resumebot`; Outlook added (Porter's file had none) |

Settings > Access, cut to the one login (the owner, 2026-10-16: connect your email in the app, the product requirements 14.10). From
Porter at **`12355fb3`**: the idea and the sealing of `household_secrets/crypto.py` (AES-256-GCM, a key worked out with HKDF-SHA256
from a secret that lives outside the database, the row's owner bound in as associated data, `v1.` stored form) became
`jobsearch/mail/seal.py`, with the key in a file in the install's secrets (`settings.mail_key_file`, made on the first save)
instead of Porter's `PORTER_SECRETS_KEY` setting; the write-only rule and `providers.yaml` (host, port, `strip_spaces`, the
how-to steps) came with it as `mailmine/providers.py` and `jobsearch/mail/login.py`. Left in Porter: row signing, release
to a tool run, the passcode proof, login detection by name and every other kind of secret. Everything else is written here.

`tests/test_mail_reader.py` is this app's own: the message reading, the hostile-input bounds, the mbox stream and the
check that keeps the IMAP connection read-only.

#### `backend/app/models/`

| File | Porter commit | Changed here |
|---|---|---|
| `__init__.py` | `09afd9b4` | replaced: Porter's imports every household model; here a docstring only |
| `agent.py` | `a71157ea` | the `Role` enum kept; `RoleDef` (roles as data) and `AgentConnection` (one roster row) are this app's own, far smaller than Porter's; `AgentRunner` (one row per runner of the scout, 2026-10-13) is this app's own too |
| `base.py` | `4a9c0595` | `EntityScoped` removed (a household foreign key) |
| `handout.py` | `2691cb6b` | `task_id` is a search run; `plan_task_id` links a site project's task; no entity or project |

#### `backend/app/planengine/`

| File | Porter commit | Changed here |
|---|---|---|
| `__init__.py` | `16d22d35` | unchanged |
| `budget.py` | `ae0ad1c4` | `AGENT_PAYLOAD_BUDGET_CHARS` is 30,000, not 60,000, and `payload_budget_for` never goes over it: Claude Code shows a reply only up to `ALWAYS_SHOWN_CHARS` (50,000, new) and saves a longer one it counts over 25,000 tokens to a file the scout's sessions cannot read; `result_ceiling_for` is gone; `one_line` is no longer re-exported, callers import it from `app/textutil.py` (SI-16); `REPLY_CEILING_CHARS` (200,000) and `RESULT_SIZE_META` are new (2026-10-14): the owner's own text goes to a Claude agent whole, `AGENT_PAYLOAD_BUDGET_CHARS` is only the size of a batch of items, and a reply over the ceiling is refused by `jobsearch/work/door.py`, never cut |
| `handouts.py` | `69159cd4` | one hand-out path (`jobs.profile`), the task is the search run, files come from the process file's `porter:instructions` and `porter:template`; `files_for` and `changed_since` name the version of each file |

The rest of Porter's `planengine/` was not kept apart from the site loop's modules (below).

#### `backend/app/process/`

| File | Porter commit | Changed here |
|---|---|---|
| `__init__.py` | `38c2cd77` | docstring |
| `bpmn.py` | `38c2cd77` | `BpmnParseError` is also a `DomainError`; only the reader How they work's page needs; the element tables and `lanes_of` come from `process_engine.bpmn` |
| `models.py` | `8dd0fb13` | only `StepType` |
| `schemas.py` | `8dd0fb13` | only the graph shapes the reader returns |

The BPMN extension namespace the engine reads (`http://porter.local/bpmn/ext`, prefix `porter:`) is part of the engine's file
format and was left as it is, so process files move between the two without edits.

#### Agents, trail and API (adapted)

Phase 2 was a scope change: the process view, How they work, the agents, the Inbox with its notifications and the task list
came over; goals and the household did not. Porter's addresses and JSON shapes are kept where a screen was copied
(`/api/v1/entities/{entity}/agents`, `.../harness`, `.../trail`), with one fixed entity and one fixed owner (`api_deps`;
`GET /api/v1/me` hands them to the page; sign-in stays off), so a copied screen needs little editing and a later Porter fix
can be carried over. The Inbox and Tasks are the exception: Porter's hang off goals.

| Here | Porter file (commit) | Changed here |
|---|---|---|
| `agents/how_they_work.py` | `agents/how_they_work.py` (`dff15af3`) | the goal processes and the role files are gone; processes are every file in `playbooks/jobsearch/`; who does a step comes from the `agent_roles` table; what the scout is sent is the fields `jobs.profile` returns; a step lists the instruction files shared by several agents as files given once; each process carries its `version` and the page draws the version in force |
| `agents/how_routes.py` | `agents/how_routes.py` (`dff15af3`) | no household or member checks; the example and the "what was sent" route take a search run; here since (2026-10-14, the sweep of character limits): no bound on a file's text or its why (`SaveFile` has none), and the save, restore and use-shipped routes refuse a file that, with the file an agent is sent beside it, would pass the room one reply leaves for the two (`owner_text`, 413 with both sizes; `process_steps.paired_names` finds the pair), so a later vendoring from Porter must not bring a bound back |
| `agents/live_view.py` | `agents/live_view.py` (`1b5cef78`) | a run is found by its subject `jobsearch:<run id>`; hand-outs are matched by search run; no shadow runs; each run carries the version it ran on |
| `agents/routes.py`, `agents/roster.py` | `agents/routes.py` (`a71157ea`), `agents/service.py` (`ac4fd3c2`), `agents/capacity.py` (`68dd3427`) | rewritten small: the roster, one agent, Pause and Resume, status and capacity. Connecting, tokens, wake secrets, skills and boundaries are not here; here since (2026-10-13, feat/find-gaps): the `gaps` session kind (Find Gaps, path 38) is one more kind the roster knows, busy-words and activity line included; here since (2026-10-13, fix/roster-lanes): an install with lanes runs several runners, so each heartbeat is noted against the runner that sent it (`agent_runners`, `note_runner`) and the roster lists them (`runners`: which, what it is at, since when, or not seen since) with the slots as the runners that are there and busy as those with a session at work; one runner is the agent as before; `session_kinds.doing` (was `doing_words`, until 2026-10-31) is the account of a session of a kind, shared by the connection's and the runners' |
| `trail/service.py`, `trail/routes.py` (routes are in `agents/routes.py`) | `trail/service.py` (`248e8126`), `trail/routes.py` (`e1ad64de`) | one table, `trail_events`, written as things happen; same entry shape and filters |
| `api_deps.py` | `api_deps.py` (`e1ad64de`) | one fixed entity and member |
| `timeutil.py` | `timeutil.py` (`eafd9ac6`) | unchanged |
| `migrations/`, `alembic.ini` | `alembic/env.py` (`9e1d4bfe`), `alembic.ini` (`3485d79c`) | the directory is `migrations/` (a folder named `alembic` is taken for a first-party package by the linter); an async env that manages only this app's tables (`app.jobsearch.db.OWNED_TABLES`); one baseline migration, `0001_baseline`, written out table by table and creating a table only if it is missing: on the running install's database (made by `create_all`) it adds the six new tables and leaves the seven it has, on an empty one it makes everything (tested both ways in `tests/test_migrations.py`, and by hand on a `pg_dump` of the running database) |

Written for this app, with no Porter file behind it: `models/work.py` (`tasks`, `notifications`, `trail_events`), `inbox/`
(the task and notification model, what a search raises, the API), and the frontend's pages. The frontend is a trimmed copy
of Porter's React app, edited since; no per-file record of it is kept. Its Dockerfile and `nginx.conf`
(Porter's pattern) were removed in 1.166: the app serves the built page itself. Restyled for this app's own brand
(1.6.x, 2026-10-07; carry a Porter style fix over by hand): `styles/focus/tokens.css` holds this app's palette (the package's `--porter*`, `--ink` and
other names are answered in Focus tokens in one block of `styles/focus/process-view.css`, loaded after the package's own
stylesheet; it draws the steps as soft squares and puts the decision criteria on a wash in place of Porter's 2px accent;
2026-10-28, with Agent Settings' move to the Focus Inbox, replacing the earlier listings-desk look); `components/agents/AgentsScreen.tsx` draws each
agent as its crew of disguised bots (`AgentCrew`) in place of Porter's initial avatar and pulse.

## Copied content and tooling

| Here | From Porter | Porter commit | Changed here |
|---|---|---|---|
| `app/playbooks/jobsearch/`, `app/instructions/jobsearch.scout.instructions.md`, `app/templates/jobsearch.scout.report.template.md` | `playbooks/jobsearch/`, `instructions/`, `templates/` | `bd64cb20` | the first process, instruction and template files; every file in these folders is this app's own now |
| `app/scout/scout.mjs`, `app/scout/scout.test.mjs` | `deploy/planner/planner.mjs`, `planner.test.mjs` | `bd64cb20` | trimmed to the scout: the planner and Teacher sessions, their soul and tool list are gone; the settings are `SCOUT_*` and `RESUMEBOT_*_URL`, the token `agent-token`; a model, effort and web tools per kind of session; the tests use fake `claude`s for the stop, timeout, kill, failure and chain paths; here since (2026-10-12, fix/scout-runner): one `finish()` for every way a run ends, SIGKILL ten seconds after a SIGTERM, a check-in with the app out of reach starts nothing, a wake's timestamp must be a number and its body under 64 KiB, settings that are not numbers fall back to their defaults, SIGTERM/SIGINT and unhandled rejections handled, port default 8651; the `reader` kind (a small Haiku session, path 29.7) with its own model setting; a `runnerFor` helper for every runner in the tests; `install.test.mjs` tests `install.sh` with stand-ins for docker and openssl; here since (2026-10-13, feat/find-gaps): the `gaps` kind (Find Gaps, path 38: the main model, three tools, the public web read-only, started by `gaps_asked`); here since (2026-10-13, feat/search-everything and feat/import-catch-up): a session that ends on Claude's usage limit tells the app once, in its "ended" heartbeat (`usage_limit`: Claude's own words, and `limit`: the time it lifts, read from the CLI's own words by `usageLimit` in `scout.mjs`), and `SCOUT_CHAIN_MAX` is a pace, not a stop (after that many sessions in a row it waits `SCOUT_CHAIN_PAUSE_SEC` and asks the app again); here since (2026-10-13, feat/parallel-research): `SCOUT_LANE` (comma-separated: `search`, `analysis`, `owner`, `writer`) makes a runner serve the kinds of work of those lanes only (another container of the same image, each its own home folder: it asks `GET /agent/work?only=<lanes>`, takes a reply outside its kinds as nothing waiting, has a check-in default per lane, chains with no pause unless it serves the search, declares only if it serves the search, says it is up between sessions only if it serves the owner's work, sends `lane` on its heartbeats unless it serves the owner's work, and holds itself at Claude's usage limit); a runner with no lane is unchanged: it serves everything, one session at a time; here since (2026-10-13, fix/roster-lanes): every runner says it is up between sessions (the `idle` heartbeat, which a runner with lanes that does not serve the owner's work sends with its `lane`), and a runner with lanes names itself in every heartbeat (`runner`: `SCOUT_RUNNER`, else its lanes joined with `+`; the two researchers need one each), so the Agents page can list every runner; a runner with no lane sends neither and is unchanged; here since (2026-10-13, fix/lanes-stalls): every session's MCP calls carry `X-Session-Id` (its heartbeat id) and, for a runner named by `SCOUT_RUNNER`, `X-Runner: <name>/<start>`, and that runner reports its start once (`announceStart`: a `boot` heartbeat), so the app frees the role a killed session held at once (`analysis.claim`, `release_runner`) instead of after the lease; here since (2026-10-13, fix/scout-sonnet-5-5): the main model defaults to the id `claude-sonnet-5-5`, not the alias `sonnet` (the pinned Claude Code 2.1.197 resolves the alias to Sonnet 5, a step behind; `SCOUT_MODEL` still overrides; carry the same into Porter's planner when it is next touched); here since (2026-10-14, the sweep of character limits): a session's instructions reach `claude` as `--append-system-prompt-file <file>`, not as the `--append-system-prompt` argument. The text is written to a file beside the MCP config in `/tmp` (mode 600, a leftover from a killed runner removed first) and deleted when the session ends, however it ends, because the kernel refuses one argument over 131,072 bytes and an instruction file the owner edits has no limit on its size; here since (2026-10-14, chore/lanes-local, interim until the app's own lane supervisor, lightweight-local D1): what a lane on the owner's own Windows PC needs, none of which changes a container (it is all unset there, or only on Windows): `SCOUT_BIND` (the listen address; 127.0.0.1 by default on Windows), `SCOUT_CLAUDE_CONFIG_DIR` (a Claude folder for each lane, with no CLAUDE.md or memory read), `SCOUT_WORKDIR` as an empty folder a session will not start without, the user's own `claude.exe` found each time a session starts when `SCOUT_CLAUDE_BIN` is unset (`findClaude`: PATH, the native install, WinGet, then the Claude desktop app's copy, newest whole version, looked for in both of the app's folders: where the app sees it under `%APPDATA%`, and its package's real folder under `%LOCALAPPDATA%\Packages`, which is all that a program started outside the app sees, 2026-10-14, feat/local-instance; and, since the app moved its copies, `%LOCALAPPDATA%\Claude-Data\claude_<id>\claude-code`, 2026-10-26, fix/claude-data-folder), a .js stand-in for `claude` under the same Node, a session ended with its whole process tree on Windows (`taskkill /T`, `killChild`), no console window and no stdin pipe for a session on Windows, and the Claude token file, which is how every install signs its agents in (`resumebot signin`), `ANTHROPIC_AUTH_TOKEN` removed from a session as the API key is; the `DEBT(lightweight-local D<n>)` tags (D1, D4, D6, D7) mark the shortcuts; here since (2026-10-31, fix/browser-stall-roster): a check-in that finds nothing says the runner is idle itself (`presence(true)`), because the pool's check-in and its idle beat are both two minutes and the beat landed while the check-in was asking, `running` was true and the beat was skipped every time; here since (2026-10-31, chore/last-leftovers): the lanes are gone from the runner: no `SCOUT_LANE`, `LANE_KINDS` or per-lane check-in, and no `SCOUT_CHAIN_MAX` or `SCOUT_CHAIN_PAUSE_SEC` (the pool never paused). Every runner is a session of the app's pool: it asks `GET /agent/work?only=any`, checks in every 2 minutes unless `SCOUT_CHECK_IN_MIN` says otherwise, declares itself, says `lane: any` and its name (`SCOUT_RUNNER`, else `any`) in every heartbeat, and is held at Claude's usage limit; here since (2026-11-14, feat/bridge-key): a browser session carries the bridge's key (`secrets/bridge-key`, made by the launcher) in its `--wsEndpoint` address and in its `ensureBrowser` and `ownBrowserStatus` calls (path 52.11) |
| `app/scout/lanes-local.mjs`, `app/scout/lanes-local.test.mjs` | none | | this app's own (2026-10-14, chore/lanes-local; interim, lightweight-local D1): the launcher that starts the five lanes (search, analysis twice, owner, writer) as `scout.mjs` processes on a PC, each on its own port, folders and Claude folder, pointed at the app on this machine from the data folder's `.env`; its tests run on Windows and in the Linux container, which `scout.test.mjs` (POSIX only) cannot. Both are deleted with `scout.mjs` when the lane supervisor (WP-7) replaces them |
| `app/backend/pyproject.toml` | `backend/pyproject.toml` | `4482318a` | Alembic kept, no cryptography; the tests live in `app/backend/tests`; the pyproject's ruff is pinned exactly, hypothesis is dropped and pydantic-core declared; `requirements.lock` and `requirements-dev.lock` are made by `dev.py lock` and installed with `--require-hashes`. Porter's `backend/Dockerfile` (`d2f72487`) was removed in 1.166 |
| `app/scripts/shots.mjs` | `scripts/shots.mjs` (`8869e603`) | | minus the member picker. Here since (2026-10-13): each picture is compared with the last release's (`pixelmatch` and `pngjs` come with Playwright's install), it notes content cut off at the edge, and `--out` takes a Windows path. Porter has none of these. Porter's `shots.sh` (`b5ced8be`) was removed in 1.166 |

Removed in 1.166 with the retired deploy host (their Porter origins are not carried): `app/deploy/` (`install.sh`, `import.sh`,
`docker-compose.yml`, `backup.sh`; `deploy/jobsearch/`, `8ec95bff`, `bd64cb20`), `app/scout/Dockerfile`
(`deploy/planner/Dockerfile`, `c8abc630`) and the scripts `release.sh`, `lock-deps.sh`
and `maintainer-env.sh`. Do not carry a script that names a host back from Porter.

Tests: the engine's tests (`test_procengine_*.py`, `procengine_support.py`, `fixtures/procengine/`, and the `session` fixture
in `conftest.py`) were copies of Porter's and are gone, because the package ships the same coverage
(`app/packages/process-engine/tests`, run by `dev.py check`). Porter's `test_isolated.py` and the MCP isolation check in
`test_mcp.py` only checked Porter's isolation from this app, so they were dropped. The app's own check that a process file's
names are held to the real instruction and template files is `test_the_app_configures_the_engine_with_its_roles_its_instruction_files_and_its_owner_key`
in `test_process_files.py`. Porter's mail-miner tests went with the miner; the reader keeps `tests/test_mail_reader.py`.
This app's own backend tests for the adapted screens are `test_harness_screens.py` and the Pipeline tests in `test_app.py`.

## Site projects (Porter `8cdd383e`)

Porter's plan-do-check-act loop, for sites as projects (the product requirements path 29). Tables in `app/backend/app/sites/models.py`
(migration `0015_site_projects`; the site loop reads and writes them).
Class: GEN is a copy with its imports changed; TRIM is a copy with the Porter seams cut; OWN is written here with Porter's file
as the reference at most. The household is the `owner` string; the role is `scout`; a goal or incident is a site
(`Project.site_id`).

The process files, from Porter `8cdd383e` as TRIM, are in `app/playbooks/sites/`, loaded into a library of their own by
`sites/process.py`. Roles are `scout` (not `chief_of_staff` or `specialist`); `household` is `owner`; Porter's `porter:engine`
attributes and the tagged step documentation are dropped.

| Here | From Porter (`8cdd383e`) | Class | What was cut or changed |
|---|---|---|---|
| `playbooks/sites/project.round.bpmn` | `playbooks/project.round.bpmn` | TRIM | project creation is code at the entry; the equip, wiki, escalation and hourly-rewake steps are cut; PR-7N is one gateway over `start.needs_owner`; the subject is a site |
| `playbooks/sites/do_one_item.bpmn` | `playbooks/do_one_item.bpmn` | TRIM | the OK-to-run, login, direct-run and Teacher rewrite steps are cut |
| `playbooks/sites/check_one_item.bpmn` | `playbooks/check_one_item.bpmn` | TRIM | the checker steps are replaced by one code step, `site.check` |
| `playbooks/project.pdca.process.yaml` | `playbooks/project.pdca.process.yaml` | TRIM | read by `harness/loader.py`; `precheck` and `plan.equip` are cut; roles are `scout`, `owner` and `code`; `act.learn` updates no wiki |
| `sites/models.py`: `projects`, `plans`, `plan_items`, `plan_tasks`, `review_reports` | `models/plan.py` | TRIM | `EntityScoped` is an `owner` column; goal, incident, approver and worker columns are dropped, as are `plan_activity` (it is `trail_events`), `project_notes` and `plan_task_steps`; enums are text columns; `Project.site_id` is added |
| `sites/models.py`: `lessons` | `models/lesson.py` | TRIM | `owner` for the entity; `applies_to` is a kind of site, `all` or `project` |
| `sites/models.py`: `js_sites`, `js_site_skills` | none | OWN | a site (`url`, `kind`, `on`) and one row per version of its saved recipe |
| `sites/act_summary.py`, `compact.py`, `earlier.py` | the same names in `planengine/` | GEN | imports only (`compact.py`: the owner's note comes first) |
| `sites/base.py` | `base.py` | GEN | imports; the household's escalation constants, project-note kinds and wiki refusal are gone; `ACT_ACTIONS` is `(act,)` |
| `sites/round_process.py` | `round_process.py` | TRIM | about half the length: the equip, run-direct, watch and permission drivers are cut; a plan that comes in starts a run at PRT-6 with `start.needs_owner`; `site.check` wraps `site_check.site_check`; the registry is `round_process.registry`, not Porter's default one; wave 5 (SI-13): the ending every `drive_*` entry shared (run the work in a savepoint and, when it fails, read back what the entry had changed) is `_attempt_or_restore`; `do_items` is `autostart.do_items`, not a second copy (SI-16) |
| `sites/plans.py`, `loop.py`, `completion.py`, `checkout.py`, `agent_calls.py`, `validation.py`, `plan_check.py`, `check_flow.py`, `task_context.py`, `lessons.py`, `resolution.py`, `projects.py`, `records.py`, `coverage.py`, `agent_handout.py` | the same names in `planengine/` (`agent_handout.py` from `process_engine_adapter/`) | TRIM | Equip, standing notes, the wiki, the household's OK card, second looks, goal and incident cases, and the second-agent checks are cut; a check is made `checked_by: code`; `_record_activity` writes `trail_events`; `projects.create_project` is the one way to open a project, for a site, one open project at a time (a partial unique index); wave 5 (SI-13): `plans._decide_plan` is a dispatcher and one function for each decision; `completion._stop` and `resume_project` take `who` (and `resume_project` the words it records), so the app's own switch of a site for a role in play (`where.switch`, `auto_watch.py`, the product requirements 29.9) writes the Activity line as the app, not as the owner; here since (2026-10-14, the sweep of character limits): `loop.py` no longer cuts the owner's "Send it back" words at 1,000 characters or folds their line breaks, so the Act task's `notes[].said` and the next plan task's `owner_said` carry them as typed, and a later vendoring from Porter must not bring the cut back; here since (2026-10-14, a "Send it back" note of 26,000 characters was accepted and the plan never asked for): `agent_handout.hand_out` holds a step only past `REPLY_CEILING_CHARS` (200,000) and not at `AGENT_PAYLOAD_BUDGET_CHARS` (30,000, a batch size), so the owner's words reach the planner, the scout and Act whole, and `task_context.task_view` leaves `owner_said` out of what the plan template's room counts; a later vendoring from Porter must not bring the batch-size hold back |
| `sites/escalation.py`, `connections.py`, `autostart.py`, `project_summary.py`, `process.py` | `escalation.py`, `connections.py`, `autostart.py`, `project_summary.py` | OWN | a few small functions the loop reads, with Porter's file as the reference: ending a project, the scout's connection, the facts the gateway needs, the plan card's content |
| `sites/asks.py`, `inbox_asks.py`, `failures.py`, `wake_ops.py` | the household cards, `process_engine_adapter/failures.py`, `planengine/wake*.py` | OWN | each owner ask is an Inbox task; a site failure is one task for the project; a wake the scout did not take is retried after 30 seconds, 2 minutes and 10 minutes, in memory |
| `sites/skills.py`, `site_check.py`, `tools.py` (`work.next`, `skill.try`), `read_probe.py`, `unreadable.py`, `where.py`, `jobsearch/fetch/polite.py`, `jobsearch/routes/site.py` | none | OWN | the site's recipes and their runner through the one guarded fetcher; the code check; the scout's two own site tools; the server's test of a site; switching off a site nothing can read; the list of sites and the owner's switches |
| `sites/tools.py`: `project.create`, `plan.propose`, `plan.revise`, `task.next`, `task.complete`, `task.escalate`, `item.lookup`, `review.read`, `act.complete`, `lesson.*`, `doc.read` | `mcp/tools/plan.py`, `lessons.py`, `docs.py` | TRIM | `project.create` takes a site instead of a goal; no Agent/Planner split, notes or second-agent checks; `task.complete` keeps the `recipe` an item built; `task.escalate` goes to the owner only; `doc.read` serves the site documents and kinds, read through the saved version |
| `jobsearch/work/session_kinds.py` (was `jobsearch/work.py`, with `handout_hold.py`, until 2026-10-31), `scout/scout.mjs` | `deploy/planner/planner.mjs` | TRIM | a `site` work kind and session, with the scout's role text as its system prompt and the site tools but `project.create` |
| `app/kinds/site/site.read.kind.md`, `app/templates/site.skill.template.md` | none | OWN | the kind a site project's items use (`risk: write`, since it writes the site's skill) and the recipe formats read with `doc.read("site.skill")` |

Not carried: the whole of Porter's mail-miner tests, goals, homes, setup, the Clock's planning, household secrets, packs,
plugins, know-how and the wiki; Porter's MCP server and role tool lists, `agents/auth.py` and agent token management (this
app's door compares one token file, `jobsearch/doors/scout.py`); the proof gate and shadow mode of the process engine (removed
from the package here, divergence 5); the mailmine's household-bound modules; and Porter tests that
need its factories, database or household models.

## Changing how a process works (the product requirements path 16)

How they work edits a process's BPMN file as text with the process view package's editor; the engine's own loader checks it,
and a save is the next version. Porter's rule-change loop (`rulechange/`) is not carried: it asks for the owner's OK in a card,
and here a save is the change. The versions are the engine's own (`process_engine.versions`, the table `proc_versions`).

| Here | Porter file | Class | Cut or changed here |
|---|---|---|---|
| `agents/edit_process.py` | `rulechange/rules.py` (`check_edit`, `summarize_edit`, `propose_edit`) | TRIM | no `RuleChange` row, no card and no Chief of Staff: a save calls `versions.add_version` at once (`kind` `edit`); restore is `versions.rollback`; `own_key` checks a file is for this process; `reconcile` keeps each shipped file as version 1 and follows a release; here since (2026-10-14, the sweep of character limits): no `WHY_MAX`, so an edit's reason and a rollback's reason are kept whole (spaces folded, nothing cut) |
| `agents/edit_routes.py` | `rulechange/routes.py` | TRIM | the file, `validate`, `edits`, the versions, one earlier version and `rollback`; no member lookup; here since (2026-10-14, the sweep of character limits): `why` has no bound on an edit or a rollback (the file's own `xml` bound stays) |
| `agents/shipped_rule.py` | none | OWN | Porter has no rule for a release that ships a changed process file. Here: a file the owner has not changed becomes the next version; one they have keeps their version in force and raises one Inbox decision ("Use the shipped version" or "Keep my version") |
| `frontend/src/components/agents/ProcessEditor.tsx`, `ProcessVersions.tsx`, `VersionList.tsx` | `HowTheyWork.tsx` (edit mode), `RuleChange.tsx` (`VersionsBlock`) | TRIM | Edit opens `BpmnProcessView` on the file in use; Save needs a why and a file the engine can run; no "waiting for your OK"; here since (2026-10-14, the sweep of character limits): the reason box has no `maxLength` |
| `jobsearch/work/instruction_files.py`, table `js_instruction_files` | none | OWN | Porter keeps no versions of its instruction files. The harness, the site loop's templates, `doc.read`, the hand-outs and `scout.mjs` read the owner's newest saved version, else the file on disk |

## Porter's names: what was renamed and what stays

**Renamed** (the scout's settings, its token, and the words an owner or an agent reads): `PLANNER_*` is `SCOUT_*`
(`PLANNER_INTERVAL_MIN` is `SCOUT_CHECK_IN_MIN`); `PORTER_MCP_URL` and `PORTER_API_URL` are `RESUMEBOT_MCP_URL` and
`RESUMEBOT_API_URL`; `PLANNER_HOST` and `PLANNER_ENGINE` are `SCOUT_HOST` and `SCOUT_ENGINE`; `PLANNER_SECRETS` is
`SCOUT_SECRETS_FROM`; the scout's `secrets/porter-token` is `secrets/agent-token`; the session's MCP config is
`/tmp/scout.mcp.json`; the trail line's actor word `porter` is `app` (no data changed: the stored values were only ever
`app`, `agent` or `person`); `restates_porter_rule` is `restates_app_rule`; the words in the mail reader's provider steps and
refusals, the plan check and the earlier-items note say resumebot or the app. `scout.mjs` still reads every
old name when the new one is missing, and keep `porter-token` beside `agent-token`, for one release; a later release deletes
the fallbacks.

**Stays, on purpose** (stored or format ids; renaming one needs a migration of kind files, plan items, vocab ids or process
files, for no gain to anyone who reads them):

- the kind `porter.explore` (`app/kinds/porter/porter.explore.kind.md`), the kind domain `porter` and `porter.lookup`; plan
  items store the kind id, and three instruction files name it to the agent;
- the vocab ids `porter.core` and `porter` (`app/vocab/`), and the `do.runner: porter` and `check.by: porter` values in kind
  files;
- the BPMN extension namespace `http://porter.local/bpmn/ext` and the `porter:` attributes in `app/playbooks/`
  (`porter:role`, `porter:instructions`, `porter:template`, `porter:key`): the format the vendored process engine and view
  read, so a change to them is a change to `app/packages/`, made here and recorded in the vendored section above;
- the trail event name `planner_retry` (stored in `trail_events.event`), and the `planner_*` handler names the round
  process's BPMN files call (`planner_run_failed`, `retry_planner_runs`).
