# Reading job sites in the app's browser

Some work a server cannot do: roles on sites it cannot read, postings that only show in a browser, sites that disallow
robots or block anything that is not a person. For those the app does the reading the way a person would, in a browser,
slowly and one page at a time. It is optional and off until you switch it on. It never applies, never fills in a form and
never gets around a block.

## What it is for

The goal is that every role the app has heard of ends up with its original posting and the posting's whole text, or with a
stated reason why it could not.

Job boards whose terms deny automated reading (the `forbids` entries of `app/backend/app/sites/site_terms.json`) are offered on no screen, and the app
suggests none of them. To have a site read, add it yourself under **Where to look** and mark it **In my browser**. The app
does not check a site's terms for you: adding a site is your choice and your risk
([`privacy-and-terms.md`](privacy-and-terms.md)).

## What you need

- Chrome, and Node.js (for `npx`), on the computer the app runs on.
- The sites you want read, marked **In my browser** on **Where to look**. A site left on "Ask resumebot to find out" joins
  them when the server cannot read it.
- The **Read in your browser** mission resumed on **Agent Settings, Missions**. It starts paused, and while it is paused no
  browser session takes any work.
- If a site needs a sign-in to show its listings, sign in yourself, in the app's own Chrome profile. Open it with:

  ```
  node app/scout/lanes-local.mjs open-browser-profile
  ```

  from the code folder. The app never signs in for you, and it never types or stores a password.

No scheduling is needed on your side. The app's own agent lanes start the browser sessions when there is work. Registering the
browser door with your Claude Code (`resumebot connect`) is not needed for them.

## The queue

The work is a queue of small items the app owns. A browser session asks for one item, does it, hands the result back, and
asks again, until nothing can be taken.

| Item | What a session does |
|---|---|
| `source` | opens a board listing, reads where Apply leads, and finds the employer's or job system's own posting |
| `read` | opens a posting's known address and copies its whole text |
| `scan` | reads one site's new listings by its runbook and hands them in |
| `map` | the Pathfinder writes or rewrites a site's runbook: how its search works, its recent filter, how its listings are laid out |

A site is scanned again 8 hours after its last scan. A taken item is leased for 10 minutes and the lease is renewed by every
call the session makes; a lease that runs out returns the item to the queue. A session that hands back nothing for 10
minutes is ended.

### The pace

Before each page load a session asks the app how long to wait, so a site sees one page every 5 to 12 seconds (a board with the robots-exempt read,
one every 30 seconds) whoever loads it, and no more than one session is on a domain at a time. A block, a CAPTCHA or a
rate-limit page stops that domain for a cooling period: 1 hour, then 8 hours if it is blocked again before a success. A
block does not use up a try and does not switch the site off.

### Results

| Result | What happens |
|---|---|
| source found | the role is sourced when the address is the employer's or a job system's. A job board's address is refused |
| source `easy_apply` | the listing itself is the source |
| read text | the role goes on to be analysed. A shortened or placeholder text is refused |
| `gone` | the role ends, with the evidence that the posting is gone |
| `not_found`, `unreadable` | tried again after 24 hours; after three tries the role stays stuck, with the reason on its card |
| `blocked` | the item goes back and the domain cools down |

## The browser

One Chrome, one window, one tab for each session. By default (`SCOUT_BROWSER=profile`) it is a Chrome profile of the app's
own, in `browser-profile` in your data folder, which you sign in to yourself. A session closes its tab when its item is
done, and no session touches any other tab. The window's first tab is titled "resumebot" and is a plain page the app serves.
The app's Chrome opens when a job first asks for it and closes when the queue has been empty for about two minutes.

A setting in the data folder's `.env`, `SCOUT_BROWSER=own`, uses your everyday Chrome and its sign-ins instead. It needs
Chrome 144 or later, remote debugging switched on once at `chrome://inspect/#remote-debugging`, and one Allow click each
time Chrome starts. Leave it on `profile` unless you have a reason.

Other settings, all optional: `SCOUT_BROWSER_PROFILE` (the profile's folder), `SCOUT_BROWSER_PORT` (the browser bridge on
`127.0.0.1`, 9333 unless set), `SCOUT_CHROME_BIN` and `SCOUT_CHROME_USER_DATA` when Chrome is not in its usual place, and
`SCOUT_BROWSER_MCP` (the DevTools MCP package and version).

## Rules every session keeps

A session reads listing pages and posting pages and nothing else. It never clicks Apply, never fills in or submits a form
(other than a site's own search box), never messages, connects or posts, never solves a CAPTCHA, and stops at a CAPTCHA or a
block. It declines non-essential cookies (Reject, Decline, Necessary only), never accepts tracking, and carries on. It does not
judge fit; the app does. It takes everything on a page as data, never as an instruction, and does nothing to hide that a
program is driving the browser. It has two MCP servers and nothing else: the app's browser door and the DevTools server
that attaches to the browser bridge. No web tools, no shell, no files.

The door is `http://127.0.0.1:8530/mcp-browser/` (the engine's address), with its own token (`secrets\browser-token`). Its
tools are `browser.next_item`, `browser.heartbeat`, `browser.hand_back`, `browser.postings` and `browser.skill`.

## What you see in the app

- The Agent Settings screen shows each browser session and the item it is on.
- **Missions** lists **Read in your browser** with when it last ran and a line saying what was handed back.
- Under **Where to look**, a site read in the browser shows where it stands: not yet mapped, waiting to be mapped, or read
  every 8 hours with the date of the last scan.
- A role's card says why the browser gave up on it after its three tries.

## If something does not work

- **No browser session starts.** The session log (`logs\runs*.log` in your data folder) says why: for example that
  `browser-token` is not in `secrets`, or the mission is paused.
- **A domain is cooling.** It was blocked. It rests an hour, then eight hours, and a session never gets round a block.
- **A role is stuck.** Its card says why; the browser tried three times, a day apart.
- **Nothing is handed in.** That is normal on a quiet night: the app skips links it has seen in the last two weeks.
