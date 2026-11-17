<!-- Used by: the scout's Do step in a site project, as the shape of the site's skill (a recipe): what an item of kind site.read builds, tries with skill.try and keeps with task.complete.
     Loaded: fetch with doc.read("site.skill"); the Do response template points to it. Written for this app (app/PROVENANCE.md, "Site skills"); the rules are those of sites/skills.py.
     Placeholders: none. Context keys: none. -->
# Response: the site's skill

A skill is a recipe: a JSON object the app runs each night, and its check runs first. It is data, never code. `kind` is one of three that you write (`browser`, a fourth, is not yours). Try one with `skill.try(task_id, recipe)`; when one real read looks right, send the same object as `recipe` on `task.complete`, and it is kept as the site's next version.

## `adapter`: a job system the app already reads

`{"kind": "adapter", "ats": "<a job-system key>", "board": {"slug": "<the board's name in its address>"}}`

- `ats` is the key of one job system from the `job_systems` list the app sends beside this text (each system's `key`, `name` and `parts`).
- `board` holds the parts that system's address has, as that list gives them as `parts` for the system's key (most take just a `slug`).

## `request`: the data request the page makes, replayed

`{"kind": "request", "method": "GET", "url": "https://jobs.example.com/api/jobs", "items": "data.results", "fields": {"title": "name", "url": "path", "location": "loc.name"}, "url_prefix": "https://jobs.example.com", "page": {"param": "offset", "size": 20}}`

- `items` is the dotted path to the list in the JSON answer (empty when the answer is the list). `fields` maps a posting field to a dotted path inside one item. `title` and `url` are required; the others are `location`, `posted`, `summary`, `ext_id`, `department`, `comp`, and, for a job board or association that lists other employers' roles, `company` (an employer's own site needs none).
- `url_prefix` is put before a `url` that is only a path. `body` (a JSON object) is only for `"method": "POST"`.
- `page` is optional. `param` is `offset` (counts postings already read) or `page` (counts from 1); `size` is the page size (pages are asked for until the site's last: a short or an empty one); `in` is `query` (the default) or `body`; `name` and `size_name` give the site's own parameter names.

## `list_page`: plain pages of links

`{"kind": "list_page", "url": "https://example.com/careers", "link_pattern": "/careers/jobs/\\d+", "title_from": "link_text", "next_pattern": "^next$"}`

- Each link whose address matches `link_pattern` is a posting; its text is the title (`title_from` `title_attr` uses the link's title attribute). `next_pattern` matches the text of the link to the next page; pages are followed until there is none.

## `browser`: written only by the owner's browser session

A fourth kind exists, `browser`: the words the owner's own browser session follows to read a site it must open in a browser. Only that session writes one. Never produce one and never send one to `skill.try` or `task.complete`. A page that needs a browser, or a site that refuses to be read, is not yours to read: call `task.escalate` with the reason `needs_browser` or `cannot_be_read` (`doc.read("agents.scout.role")` says how).

## Rules

- Public `http` and `https` addresses only. No sign-in, no key or token, no address that needs a cookie.
- A pattern is at most 300 characters and may not nest repeats, put a choice inside a repeat, use a back-reference or look around; a run reads a site to its last page, however many that is, and stops by itself if a page brings nothing it has not read already. The app reads `robots.txt`, paces its requests, and stops at a refusal. Never look for a way round one.
- Every posting needs a title and a link. A recipe that reads none, or reads titles without links, fails its check.
