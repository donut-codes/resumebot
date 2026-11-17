<!-- Used by: the Pathfinder, a session of the app's pool that maps job sites in the app's own browser, one map item at a time (docs/PRD.md 37.3 and 37.12, the browser work spec), as the shape of one `browser.skill` call and of a portal's declaration (handed over with the session's first `browser.next_item`).
     Loaded: by browser.next_item (backend/app/jobsearch/browser_work.py) with the Pathfinder's instructions.
     Placeholders: none. Context keys: none. -->
# Response: one site's runbook recipe

The recipe is a JSON object, sent as `recipe`; this is it in YAML. The runbook in it is what a smaller session follows every night, so write what you did, in plain words it can act on, not what you guess. The runbook is markdown in exactly the shape of `runbook_template` (`browser.pathfinder.runbook.template.md`, with each map item), at most 6,000 characters: a longer one is refused. The fields beside it are the few the app and the reader use; each text is at most 300 characters. Addresses are public `http` or `https` addresses, never one that carries a token or a key. The site below is made up; every value is an example of the kind of thing to write.

A site whose openings are on the employer's own listing on a job system (a portal) is declared before its postings, in a `browser.postings` call (`postings` may be empty). The recipe then describes the portal's pages, and its `start_url` stays the site's own careers page: a portal's address can carry an id that a recipe may not.

```yaml
portals:             # in browser.postings, only for a site with a portal
  - site: "the item's site, exactly as it gave it"
    url: "https://... the employer's own listing on the job system, as the site's careers page links to it"
```

```yaml
site: "the item's site, exactly as it gave it"
page_says: 23        # the number of results the page states, if it states one; leave the line out when it does not
lessons:             # up to three plain sentences about this site or its kind of site; leave out what you did not learn
  - "The location box wants a city, not a state."
recipe:
  kind: browser
  start_url: "https://www.example.com/jobs"   # the listings or search page (for a site with a portal: its own careers page)
  runbook: |                                  # the whole runbook, in the template's shape, at most 6,000 characters
    # Example Jobs runbook
    Mapped 2026-10-15 · Job system: <name> · Reuses: none
    ## 1. Start
    ...                                       # sections 1 to 7 as the template has them
  bookmarks:                                  # one for each search the item needs (its phrases and places); test at least one
    - phrase: "the item's search phrase"
      place: "the item's place, or remote"
      url: "https://www.example.com/jobs?q=...&l=...&sort=newest"
  ats: "<name>"                               # the job system's name, or leave the line out when the site is its own
  new_since: "Sort by newest and stop at the first posting older than the last run"   # one line: how to read only what is new
  # An older recipe (search, results, posting, recommended_url, notes) is still read, but write a runbook.
```
