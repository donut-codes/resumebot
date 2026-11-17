# process-engine

A deterministic BPMN process engine. It loads process files and runs them on SQLite (or Postgres) with every move recorded. No
model is anywhere in it. It imports nothing from an app: everything an app adds goes through the extension points
below.

Distribution `process-engine`, import `process_engine`. Python 3.12, SQLAlchemy 2 (async, SQLite or Postgres), defusedxml.

## What is in it

| Module | What it does |
|---|---|
| `loader` | A BPMN file becomes an executable `ProcessDef`, or is refused in plain words naming the element. |
| `bpmn` | The reader's helpers (element names, lanes, event definitions, the upstream project's step tags). See `PROVENANCE.md`. |
| `registry` | The action registry (`register_action`), card kinds, and the catalog of names a file may use. |
| `feel`, `datamodel`, `model` | The safe FEEL subset conditions are written in, the declared data model, the loaded model. |
| `engine` | `Engine`: start, complete, deliver a message, tick timers, cancel. Runs to the next wait. |
| `live` | Register a process to run live (`register_drivable`); `attempt` runs its work in a savepoint, and a failure is undone, then held for the app (`on_drive_failure`). |
| `versions` | An owner's own versions of a process file, and rolling back. |
| `models`, `base` | The tables, on the package's own `Base`. |

Steps carry the upstream project's tags without acting on them: tagged `<documentation>` (`_instructions`, `_criteria`,
`_commands`, `_prompts`) and the extension elements (owner, role, person, software, document, control) are
read into `Element.tags` (`StepTags`), kept in `ProcessDef.xml`, and left out of `behaviour_hash`.

## Extension points

An app fills these once at start-up with `process_engine.config.configure(...)`. Each has a default that works with
nothing configured. The live values are `process_engine.config.config`.

| Setting | The engine asks | Default |
|---|---|---|
| `roles` | which roles an agent step (`porter:role`) may name (a set, or a function returning one) | none: every agent step is refused |
| `card_kinds` | which card kinds a user step (`porter:card`) may name, besides `registry.register_card_kind` | none |
| `instructions` | an `InstructionSource`: `names(kind)` and `short_name(kind, ref)` for the instruction and template files a step may name | none exist |
| `owner_key` | the key of an instance's data that holds the id of the owner it runs for | `"owner"` |
| `owner_candidates` | `(data, subject) -> strings` that may be the owner's id, for choosing which versions an instance starts on | `data[owner_key]` |
| `shipped_dir` | the folder of the shipped `*.bpmn` files (versions, rollback) | none: nothing is shipped |
| `register_processes` | imports whatever registers the actions the shipped files name, before a version is checked | nothing |
| `member_names` | `async (session, owner_id) -> {id: name}`: who made a version | no names |
| `shipped_label` | the history line for version 0 | `"The file as it ships."` |
| `known_attributes` | the `porter:` names the app reads itself (besides `engine`, `harness`, `calls`); given, every other name is refused | `None`: only a misspelling of a name the engine reads is refused |
| `on_drive_failure` | `async (session, entity_id, key, what, exc, subject=None)`, when `live.attempt`'s work fails. The savepoint is rolled back first. It is the only thing that happens: no live run is cancelled, no other code acts, and the app tells the person (the run is held at that step) | `None`: the failure is only logged |
| `on_drive_success` | `async (session, entity_id, key, what, subject=None)`, after a live `attempt` finished its work, so what `on_drive_failure` raised can close by itself | `None` |

Beyond the config, an app plugs in through the registries the modules already have:

- **Actions:** `registry.register_action(name, async fn(session, instance, data), inputs=, outputs=, description=, source=)`
  (or its own `ActionRegistry`). A service step names one; safeguards live in the action, never in the process.
- **Processes that run live:** `live.register_drivable(key, label, library, handouts=, registry=, deferred=, retry=, plain=)`.
  `handouts` maps a waiting step's kind (`user`, `agent`) to the function that hands it out (the app's cards and tasks).
  `deferred` names the actions the engine can't run inside its own run: a step naming one waits, and the code that does
  the work feeds its outcome with `Engine.complete`. `retry=` is `async (session, entity_id, subject)`: it runs the
  process's entry again for a subject (or, with `None`, for everything the owner's process holds) from today's facts, for
  the app's "Try again" after a held failure. It works through `live.attempt`, so a second failure is held and reported
  the same way. `live.live_engine(key)` is the engine that calls the actions and hands the waiting steps out.
- **A failure nobody expected:** `live.attempt(session, entity_id, key, what, work, savepoint=, subject=)` rolls the
  work back and returns `Attempt(ok=False)`. The run is held where it was (nothing is cancelled) and, with
  `on_drive_failure` set, the app's hook says so. `what` is the work in plain words ("start a look"), because an app
  shows it to a person; `subject` (optional) names what the work was about, so a retry can be exact.
- **Errors:** `versions.VersionError` carries a `status` (409) so an app can map it to a response; the package raises
  no HTTP exception and knows no web framework. Routes and who may sign off stay in the app.
- **File format:** the extension attributes use the namespace `http://porter.local/bpmn/ext` (prefix `porter`), its
  historical name, kept so existing files load (`loader.PORTER_NS`).

## Tables

The package keeps its tables on its own `process_engine.base.Base` (its own `MetaData`), so it never needs the app's
`Base`. They are `proc_instances`, `proc_files`, `proc_tokens`, `proc_waits`, `proc_history` and `proc_versions`.
`proc_files` keeps the text of every file an instance starts on, by its hash, so a run finishes on its own file after
the loaded one has changed. No table has a foreign key to a table the app owns: the owner (`entity_id`) and the people
(`changed_by`, `signed_off_by`) are plain UUID columns, and `rule_change_id` is a plain UUID.

The app puts the tables in its migrations either way it likes:

- list `process_engine.base.Base.metadata` beside its own as Alembic's `target_metadata`; or
- copy them into its own metadata with `Table.to_metadata(app_metadata)` and add the foreign keys it wants (a host app can do
  this, so its metadata matches the schema its migrations built: `app/process_engine_adapter/tables.py`).

The package ships no migration. A new app creates the tables with `Base.metadata.create_all` or its own migration.

## The database

The tables use dialect-neutral types, and `process_engine/portable.py` is the one module that knows which database is
behind a session: the upsert (`insert_ignore`), the row locks (`for_update`, `get_locked`) and the counting key of
`proc_history` (`AutoKey`). On SQLite there is one writer, so there is no row lock to take or to wait for. The host owes
the engine two settings there: begin every transaction that may write with `BEGIN IMMEDIATE`, and turn the driver's own
transaction handling off (`isolation_level = None` on connect), which also makes savepoints (`live.attempt`, `tick`)
undo with the caller's transaction. `tests/conftest.py` does both; the module's docstring says why.

## Tests

`pytest` in this folder, on a SQLite file in a temporary folder (`PROCESS_ENGINE_TEST_DATABASE_URL` names another
`sqlite+aiosqlite` file). Nothing has to be running. `tests/test_boundary.py` fails if any file here imports an app.
`tests/support` holds the sample processes, the registry of sample actions and the fake clock the tests share.
