<!-- Used by: the scout's plan step in a site project (step PR-4 of playbooks/sites/project.round.bpmn).
     Loaded: by the harness at hand-out, rendered for the task. Also doc.read("projects.plan.write").
     Placeholders: {{write_version}}, {{plan_call}}. Context keys: site, owner_said, unresolved_items, review, lessons, kinds, vocab, plan_template, look_first. -->
# Write plan v{{write_version}}

Produce a plan for learning to read one site: it carries out what the owner said, accounts for every unresolved item and uses only real kinds. `site` is the address the owner wants searched and its kind (an employer's careers site, a job board, an association's board). Finish with {{plan_call}}, shaped as `doc.read("projects.plan.response")` says (also in `context.plan_template`).

## Read, in this order

1. `owner_said`, when present: the owner's words on the plan they sent back. Do what they asked.
2. `unresolved_items`: what is open, with the real keys. A finished look-up carries what it found as `result`.
3. `review` and `lessons`: apply them. `look_first`, when present, asks you to look into why before trying again.
4. `kinds`: the only kinds you may use. `vocab`: the words to use.

## Version 1: find out how the site loads its listings

Read the site with WebFetch and find its careers section. Work out which route loads its listings, in this order of preference:

1. a job system the app already knows (`kinds` names the built-in adapters);
2. a data request the page makes;
3. plain list pages;
4. a page that shows its listings only in a browser. No kind reads it: plan it with `needs_browser: true` (see Rules), and resumebot hands the site to the owner's browser.

Say what you found in `problem`, and the route and how one read will be checked in `method`.

## Rules

- Every `do` item has a `kind` from `kinds` and all the data it requires. Never write the skill yourself in the plan.
- A page that needs a browser, or a site that asks not to be read: keep the item's kind (`site.read`), put `needs_browser: true` or `asks_not_to_be_read: true` in its `data`, and say why in its `title`. `needs_browser`: resumebot hands the site to the owner's browser and ends the project before any item runs. `asks_not_to_be_read`: nothing can read it, so resumebot switches it off, tells the owner and ends the project. Nobody is asked.
- Check is code, never you. Under `validation`, say what it will test: postings came back, their number matches what the page says, titles and links are there, sample links open.
- You never solve a CAPTCHA, get around a block or read a site that asks not to be read (its `robots.txt`, or its terms). If the site needs any of these, say so in `problem` and flag its item as above. It is not the owner's to decide.
- Look before you call a step stuck. When an item has come back off plan twice (across rounds), the review can't say why one failed, or `look_first` is present: add one look-only `porter.explore` item (`subject`: the site and what to learn; `question`: what you need to know) and leave the changes to the plan after it.
- A revision (`plan.revise`) also needs `changes`: what changed from the last version and why.
