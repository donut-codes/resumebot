<!-- Used by: the scout's Act step in a site project, as the shape of its summary (step PR-13 of playbooks/sites/project.round.bpmn).
     Loaded: named in the task as `template`; fetch with doc.read("projects.act.response").
     Placeholders: none. Context keys: none. -->
# Response: what a round taught

Every call takes this task's `project_id` and `task_id`.

- `lesson.record`, one per lesson: `text` (one plain sentence), `applies_to` (`project` or `owner`), `source`, and optionally `subject` (a few words; a newer lesson on the same subject retires the older).
- `lesson.retire(lesson_id, source)`.
- `act.complete(summary, resolution)`.

`summary` has five parts, each a short plain sentence or two (300 characters at most), with facts from `report` and `items`:

- `planned`: what the round set out to do.
- `done`: what was actually done.
- `checks`: what the checks found.
- `learned`: what was learned, or that nothing new was.
- `next`: what happens next, and what the next version of the site's skill changes or what to look at first if something went off plan.

`resolution`, only when the task has `closing`, has three plain parts for the owner:

- `problem`: what was wrong or suspected.
- `done`: what was built or found.
- `outcome`: how it ended and how it was confirmed.
