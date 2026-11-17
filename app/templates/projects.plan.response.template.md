<!-- Used by: the scout's plan step in a site project, as the shape of its plan (step PR-4 of playbooks/sites/project.round.bpmn); carried whole in `context.plan_template`.
     Loaded: by the harness at hand-out. Also doc.read("projects.plan.response").
     Placeholders: none. Context keys: unresolved_items. The Limits block is checked against the constants in backend/app/sites/base.py by backend/tests/test_plan_limits.py: change both together. -->
template_version: 4

# Plan response template

Give each part to `plan.propose` (first plan) or `plan.revise` as its field.

- `problem` (required): what the site is and how its listings load, in 2-3 plain sentences with real numbers (how many postings the page shows). On a revision, what the last round found.
- `proposal` (required): what the plan will do, as short bullets.
- `dropped`: `[{key, reason}]` for an unresolved item left out on purpose, `reason` one plain sentence. Every key in `unresolved_items` is an item or here. Never drop an item that still needs doing.
- `summary`: what the plan achieves.
- `changes` (`plan.revise` only, required): what changed from the last version, and why.
- `method`: which route the skill takes (a known job system, a data request, list pages) and how one read of the site is checked, written fresh for this version.
- `items`: `{key, title, group, action, kind, data}`, one small step each. `action` is `do`, `skip` or `investigate`. A page that needs a browser, or a site that asks not to be read: keep `kind` `site.read`, set `data.needs_browser: true` or `data.asks_not_to_be_read: true`, and say why in `title`. `needs_browser`: the app hands the site to the owner's browser and ends the project before any item runs. `asks_not_to_be_read`: nothing can read it, so the app switches it off, tells the owner and ends the project. Nobody is asked.
- `validation` (required, read-only):
  - `done_means`: when an item is done.
  - `how_to_check`: what the check will test on one read: postings came back, their number matches what the page says, titles and links are there, sample links open.
  - `off_plan_means`: which differences to report, and how to word them.
  - `tools_needed`: `[{title, purpose}]`, abilities the scout lacks; `[]` if none. Kept with the plan; it never asks the owner (the flag on an item's `data` does).
- `act_guidance`: what to learn after the check, and what the next version of the skill should change if it fails.

## Limits

<!-- limits:start -->
- `problem`: at most 20,000 characters.
- `proposal`: 1 to 3 bullets, each at most 2,000 characters.
- `method`: at most 20,000 characters.
- `validation`: each of `done_means`, `how_to_check`, `off_plan_means` at most 6,000 characters; `tools_needed` at most 10, each field at most 200.
- `dropped`: at most 2,000, each `reason` at most 500 characters.
- `act_guidance`: at most 6,000 characters.
- An item's `data`: at most 8,000 bytes.
<!-- limits:end -->
