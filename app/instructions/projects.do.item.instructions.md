<!-- Used by: the scout's do step in a site project (step Specialist_does of playbooks/sites/do_one_item.bpmn).
     Loaded: by the harness at hand-out, rendered for the task. Also doc.read("projects.do.item").
     Placeholders: {{item_key}}, {{kind}}. Context keys: kind_spec, earlier. -->
# Do item {{item_key}} (kind {{kind}})

Do this one item as its kind says and report it with `task.complete`. It is done when `kind_spec.done_means` holds.

`kind_spec` is the kind's summary: `data` (what the item carries), `done_means`, `risk`, and sometimes `result` (the fields `task.complete` takes). How the kind is done is in its file: `doc.read(kind_spec.id)` returns it with the prose in `notes`.

1. Fetch the kind's file with `doc.read(kind_spec.id)` and do exactly what its `notes` say, with this task's `data` and nothing else.
2. For a site's skill (an item of kind `site.read`, whose recipe is one of the recipe kinds `adapter`, `request` or `list_page`) the item builds the recipe as that recipe kind describes it, then tries it once on the real site and reports how many postings came back and the first few titles as `evidence`. You do not judge your own read: a check, run by code, does that.
3. When `done_means` holds, finish `done`.
4. Looking is free inside this step. If something stops it (a page isn't where you expected, a request is refused, a result is cut off), look another way: `doc.read`, `item.lookup`, WebFetch, a retry, or `context.earlier` (what earlier items found). Touch nothing the kind does not name.
5. If the intent still can't be met, finish `off_plan` with the real values in `off_plan_reason`. A look-only step (`kind_spec.risk` is `read_only`) also needs `tried`.
6. If you can't read the site (it asks you to sign in, solve a CAPTCHA or get around a block, disallows you in its `robots.txt`, or shows its listings only in a browser), stop and call `task.escalate` once, with the reason the role file gives (`needs_browser` or `cannot_be_read`). If you cannot run the kind at all for another reason, call `task.escalate` once and stop.
