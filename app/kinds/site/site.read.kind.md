---
kind: site.read
domain: site
title: Read a site's job listings
risk: write  # it writes the site's skill (a recipe in the app), never anything on the site; not a look-up feeding the next plan
enabled: true
data:
  url: {type: string, required: true, means: "The page the listings are on, or the site's careers section"}
do:
  runner: model
done_means: "The site's skill (its recipe) was built or tried and one real read came back; the evidence says how many postings and the first titles."
check:
  by: porter  # the app itself: site.check is code
  test: "site.check runs the site's newest skill: postings came back, each with a title and a link, the count matches the page, sample links open."
  causes: [not_attempted]
result:
  count: {type: int, required: false, means: "How many postings the read found"}
  titles: {type: list, required: false, means: "The first few titles, as read"}
  expected_count: {type: int, required: false, means: "How many postings the page itself says it lists; 0 when it says there are none"}
---
Build or fix the site's skill for this page (a recipe of kind `adapter`, `request` or `list_page`; `doc.read("site.skill")` shows each), try one real read with `skill.try`, and complete the task with the count and the first titles as evidence and the recipe as `recipe`, which the app keeps as the site's next version. Don't judge the read yourself: the check is code. When the page states how many postings it lists (or that it has none), send that number as `result.expected_count`, so the check can compare. A page that needs a browser, a sign-in or a way around a block, or one whose robots.txt asks not to be read, is not yours to read: call `task.escalate` with the reason `needs_browser` or `cannot_be_read`.
