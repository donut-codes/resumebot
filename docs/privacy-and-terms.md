# Privacy and terms

resumebot runs on your PC and keeps your data there. This page says what it reads, what it never does, where your data
goes, and what is your risk. It is not legal advice.

## What it reads

- **Public job feeds and career pages.** The app's own code reads employers' public job feeds and career pages, and job
  systems' public listings. It honours each site's `robots.txt` (its rules and its crawl delay), keeps to about one request a
  second for each host, announces itself with an honest user agent (`resumebot/<version>`, "a personal job-search
  assistant; reads public job listings only"), and treats a refusal of any kind as a stop: a host that refuses is left alone
  for a while. The search agents also read public pages with Claude's own web search and fetch tools.
- **A read even though `robots.txt` asks automated readers not to, only if your copy carries one and you turn it on.** It
  is a runbook field that exists only in the owner's own copy (it lives in the restricted folder, which never ships), so a
  public copy has nothing to switch. The setting (`read_robots_exempt`) is off by default. When it is on, the app reads
  the public guest job feed of a board whose roles must be followed to the employer's own site, never signed in, at a slow
  pace of one request every 30 seconds, and a refusal of any kind stops it for the night. That board's `robots.txt` asks
  automated readers not to, and its terms forbid automated access. This is the one place the app does not follow
  `robots.txt`. Leave the setting off unless you accept that risk. The app never suggests it.
- **The browser jobs, only if you use them.** For sites a server cannot read, the app can use one Chrome window on its own
  profile, which you sign in to yourself. Every session works in tabs of its own in that window and touches none of yours.
  The app never signs in, types or stores credentials, and a signed-in page is only read, as you would see it. The
  mission starts paused. See [`browser-reader.md`](browser-reader.md).
- **Your mailbox, only if you connect it.** The app can read your mail to see replies and confirmations. It sends only the
  read-only commands of the mail protocol and changes nothing in the mailbox. You give it an app password on the page, never
  in a chat. You can leave this off.

## What it never does

- It never applies to a job, submits anything, signs in for you, fills in a form or solves a CAPTCHA. The Go button opens
  your roles in tabs and stops.
- It never gets around a block. A CAPTCHA, a refusal or a rate-limit page stops reading that site for a while.
- It never rewords your master resume. Only you change it.
- It never drops a role silently.
- It never sends your data to anyone but Claude, below.

## Where your data stays

On your PC. The database, your settings, your secrets, your logs and the resumes and cover letters it builds are in your own
folders ([`install.md`](install.md)). The page and the engine listen on `127.0.0.1`, so nothing else on your network can
reach them, and the app checks the Host and Origin of every request. The owner door needs its token, and the
secrets folder is limited to your user.

The one place it goes out is Claude. The agents are Claude Code sessions on your own subscription, and what they work on
is sent to Claude as any Claude Code session's is: your resume's lines, your targets, the postings, a role's text. Claude's
own terms and privacy policy govern that. The app has no server of its own, no account, no telemetry and no hosted
service. Backups stay in your data folder unless you name a second folder yourself (`RESUMEBOT_BACKUP_DIR`).

## Your own risk with site terms

Some job sites and job systems forbid automated reading in their terms, and the app cannot judge a site's terms for you.
Know where that touches you:

- **The board with the robots-exempt read** is off by default and stays off until you switch it on (above). Its terms
  forbid automated access.
- **Job systems** host many employers' career pages. The app reads their public job listings for
  the employers you watch, the way their own career pages do, with the pace and stops above. Which job systems the app
  ships a reader for, and what each one's terms say, is data (`app/backend/app/sites/site_terms.json`); whether a
  reader of this kind is allowed by the terms of one you add is not something the app checks, so this is a risk you carry by default.
- **Anything you add** under Where to look, turn on, or point the browser jobs at is on your own responsibility. The app
  adds no job board for you and does not check the terms of one you add.

Where to look shows one line, "Warning: automated tools on this site may violate its terms of service", with a link to
the site's terms page, on a site or job system the app knows forbids them in its terms. The list ships with the app,
was read on 2026-11-14 and is not updated while the app runs, so a site that is not on it, or whose terms changed since,
shows nothing. The warning only informs: it never blocks, switches off or changes anything.

Using the app against a site's terms can get your accounts or your address blocked, and the cost of that is yours. If
that matters to you, read those sites' terms first and switch off what you do not accept.

## Licence and warranty

resumebot is released under the MIT licence ([`../LICENSE`](../LICENSE)). It comes as is, with no warranty of any kind, and
the authors are not liable for anything that comes of using it, including a missed job, a wrong score, a lost file or a
blocked account. The bundled DejaVu fonts carry their own licence in the folder beside them. It is alpha software: keep a
copy of anything you cannot lose, such as your resume.
