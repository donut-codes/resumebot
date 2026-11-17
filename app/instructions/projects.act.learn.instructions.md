<!-- Used by: the scout's Act step in a site project (step PR-13 of playbooks/sites/project.round.bpmn).
     Loaded: by the harness at hand-out, rendered for the task. Also doc.read("projects.act.learn").
     Placeholders: {{plan_version}}. Context keys: plan_summary, report, items, notes, lessons, closing. -->
# Learn from plan v{{plan_version}}

Record what this round taught about reading this site. Use this round only. `plan_summary` is what the round set out to do, `report` what the check found, `items` each item's outcome and evidence, `notes` what the owner said, `lessons` what is already held (each with an `id`).

1. `lesson.record` once per lesson: one plain sentence about this site, its job system or this kind of site ("this site loads ten more listings on scroll: use its data request").
   - `source` is required: the item keys, the report or the owner's note of this round it came from.
   - `applies_to` is the narrowest that is true: `project` (this site, the default) or `owner` (every site).
   - A lesson that changes how a skill should behave has the `subject` `skill:<kind id>`.
   - Never record a rule resumebot enforces, or a failure this round no longer shows.
2. `lesson.retire(lesson_id, source)` for each lesson in `lessons` that this round's evidence contradicts.
3. The site's skill is kept as Do saved it; do not rewrite it here. `report` says whether its check was on plan. When it was not, say in `next` what the next version changes: how the listings load, what came back wrong.
4. `act.complete`.

- If this round's evidence shows you can't read the site (a sign-in or token, listings that only load in a browser, a block, `robots.txt`), don't plan another round and don't ask the owner: call `task.escalate` once instead of `act.complete`, with the reason `needs_browser` (a browser might read it) or `cannot_be_read` (the site refuses to be read: `robots.txt` or its terms).
- An item that went off plan twice, or a failure the report can't explain: record no guess. Put in `next` that it should be looked into first with a `porter.explore` item.
- With `closing`, this Act can end the project. Judge the round against `closing.outcome`, the project's own outcome (it comes from `resolution.closing_hint`), and nothing wider. If this round's evidence meets it, add a `resolution` and the project ends here; otherwise leave it out and the project goes on to another round. Work outside that outcome is for another project, never a reason to go on. The `resolution` comes from `report`, `items` and `closing.problem`: plain words, no ids, paths, commands, plan or round numbers, or tool or skill names, and only what this round's evidence shows.
