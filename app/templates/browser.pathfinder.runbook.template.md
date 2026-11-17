<!-- Used by: the Pathfinder, a session of the app's pool that maps job sites in the app's own browser (docs/PRD.md 37.3), as the exact shape of the site runbook it writes; the browser worker follows that runbook for each scan of the site, and its section 6 to find a listing's original posting.
     Loaded: by browser.next_item (backend/app/jobsearch/browser_work.py) with each map item, as `runbook_template`, beside the Pathfinder's instructions and the `browser.skill` response template. The runbook goes in `browser.skill`'s `recipe.runbook`.
     Placeholders: none (the <angle-bracket> parts and {phrase} / {place} are for the Pathfinder to fill in, not the app's placeholders). Context keys: none. -->
# <Site name> runbook
Mapped <date> · Job system: <the job system's name / custom …> · Reuses: <runbook it started from, or "none">

## 1. Start
- Start URL: <exact address of the job search page>
- How to get there: <direct | main site → Careers → … | redirected to the job system at …>
- Search state lives in the URL: <yes / partly / no>

## 2. Filters
One line per filter the site offers, in this form:
<Filter name> | <how it's set: URL parameter or control> | <every option> | → maps to: <which search setting, and which options to use> | <use / don't use, and why>

Always check for: title or keyword search · date posted · location · remote / hybrid / on-site · level · department or function · sort by newest.
If a filter doesn't exist, write: "<Filter>: not offered."

## 3. Bookmarks
- Template: <the URL with {phrase} and {place} slots and the chosen filters in place>
- One line per search needed: <phrase> · <place or remote> · <full URL>
- Tested: <which URL> → <result count>, <what was checked>

## 4. New since the last run
- Method: <newest-first sort + date filter | posting ID watermark | page until older than the last run | none: full scan>
- Stop when: <the rule for when to stop paging>

## 5. Reading results
- Result row: <where to find title · employer · location · work mode · pay · posted date>
- Next page: <how to page>
- Full posting: <where the whole text appears, and how to expand it>
- Posting address: <the stable address for one posting>
- Apply link: <where it is and where it leads; read it, never click Apply>

## 6. Finding the original posting
- From a listing to the employer's own posting: <follow the Apply link to the employer's site | the employer site link on the posting | search the employer's career site for the title | none: Easy Apply only, so this listing is the source>
- How to recognise the original: <the job system's address pattern, the posting ID, what the page shows>
- If the link lands on a general careers page: <how to search there for the matching posting>
- Tested: <one listing → its original posting address>

## 7. Fallbacks and pitfalls
- If the bookmark returns nothing: <what to try next>
- Known traps: <loading delays, fuzzy matching, sign-in walls, pages that only render in a browser, a cookie banner and the button that declines it …>
- Brute force (last resort): <how to walk every posting, if needed>

## 8. Unconfirmed
- <anything not tested, so the next run checks it>
