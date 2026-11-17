"""The process engine ("the process is the program"): runs BPMN files on SQLite or Postgres, with no model in it.

- `loader.load_process` / `ProcessLibrary`: a BPMN file -> an executable model, or a plain refusal naming the
  element (anything outside the supported subset, an unknown action/role/file/card, a condition over a field the
  data model doesn't declare, a decision with no way to decide).
- `registry`: the action registry (`register_action`, `list_actions`) and the catalog of names a file may use.
- `feel`: the safe FEEL subset conditions are written in; `datamodel`: the published data model.
- `engine.Engine`: start, complete, deliver a message, tick timers, cancel; run-to-wait, all recorded in
  `models` (instances, tokens, waits, history).
- `live`: register a process to run live; `attempt` runs its work in a savepoint, so a failure is undone and held.
- `versions`: an owner's own versions of a file.
- `config`: the extension points an app fills in (`configure(...)`); the README lists them.
- `portable`: the few places SQL differs between SQLite and Postgres (the upsert, the row locks, the counting key);
  the only module that names a dialect.

The package imports nothing from an app. Import from the submodules (`process_engine.engine`, `.loader`, ...): this
module imports nothing itself.
"""
