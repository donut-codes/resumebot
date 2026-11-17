<!-- Used by: the browser worker, a session of the app's pool that works the queue of small browser items (the browser work spec), as the shape of one `browser.postings` call and one `browser.hand_back` call (handed over with the session's first `browser.next_item`).
     Loaded: by browser.next_item (backend/app/jobsearch/browser_work.py).
     Placeholders: none. Context keys: none. -->
postings:   # a browser.postings call, for the scan item you hold: at most the item's batch; each is one listing you read
  - site: "the item's site, exactly as it gave it"
    url: "https://... the posting's own address (on the portal, for a site that has one)"
    title: "The title as the listing shows it"
    company: "The employer as the listing shows it"
    location: "Place as shown"            # leave out what the page doesn't show
    work_mode: remote                     # remote | hybrid | onsite, only if the listing says
    pay: "$00,000 - $00,000 a year"       # as written
    posted: "2 days ago"                  # as written
    snippet: "The text the listing shows"
    # description: "The whole posting text, copied exactly" # only when it is the whole posting; a preview or a cut-off text is left out
    apply_url: "https://... the address behind Apply, read but never clicked"

# A browser.hand_back call: what came of the item you hold. Only the fields its result needs.
item_id: "as browser.next_item gave it"
result: found          # source: found | easy_apply | not_found | gone | blocked
                       # read:   text | gone | unreadable | blocked
                       # scan:   done | blocked | failed
url: "https://... found: the employer's own posting or its job system's (never a job board's); easy_apply: the board listing"
source_kind: employer  # found only: employer, or recruiter for a recruiter's page where Apply lands
text: "The whole posting text, copied exactly as the page shows it with see more expanded: every section, never shortened or summarised"
# title: "The title as the page shows it"       # read, for an item marked added_by_link
# company: "The employer as the page shows it"  # read, for an item marked added_by_link
# note: "the careers page shows no posting with this title"   # not_found, unreadable, blocked, failed: why; gone: the words the page showed
