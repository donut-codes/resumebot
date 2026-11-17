<!-- Used by: the scout's do step in a site project, as the shape of one item's result (step Specialist_does of playbooks/sites/do_one_item.bpmn).
     Loaded: named in the task as `template`; fetch with doc.read("projects.do.item.response").
     Placeholders: none. Context keys: kind_spec. -->
# Response: one item done

`task.complete`:

- `task_id`: this task's id.
- `outcome`: `done` or `off_plan`.
- `summary`: one line: what you did, with the item's key.
- `evidence`: what the real read returned (how many postings, the first few titles), or a short quote. When the page states a total, add it as `expected count N`, so the check can compare.
- `recipe`: only for an item of kind `site.read`, and only with `done`. The skill you built and tried, as `doc.read("site.skill")` shapes it; the app keeps it as the site's next version. Try it first with `skill.try`, which reads the real site once and gives the count and the first titles.
- `result`: only when `kind_spec` has a `result`; required for `done`. An object with exactly those fields, typed as it says (a whole number is a number, not text).
- `off_plan_reason`: only for `off_plan`. What differed from the plan, with the real values.
- `tried`: only for `off_plan` on a step that only looks (`kind_spec.risk` is `read_only`); required there. A short list of what you tried to meet the intent: another way, a document, an earlier result, a retry.
