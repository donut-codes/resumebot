# Vocabulary

Shared terms for the app in `app/`. The point is that a human and an agent mean the same thing by the same word. Most of
the entries below exist because a specific misunderstanding cost real work.

## Session kind, agent kind, hand-out, claim and runner word

A **session kind** is one of the thirteen things a session of the runner can be started for (`parser`, `checker`,
`gaps`, `search`, `titles`, `analysis`, `researcher`, `site`, `seat`, `coach`, `mail`, `browser_reader`, `pathfinder`):
the name an ask for work is answered with. An **agent kind** is one of the seven the owner pauses, runs and sees listed
on the Agents page (`learner`, `pathfinder`, `researcher`, `writer`, `browser_reader`, `title_checker`, `search`); a
session kind belongs to one of them (a seat is the writer's, a mail session the researcher's), except the owner's asks
at the screen (a posting to split, a check of their roles, Find Gaps), which nothing pauses. The **runner word** is what
a session says it is in its heartbeat and starts its id with (`analyst` for the `analysis` kind, `writer` for `seat`,
`scout` for `search`): a different vocabulary from the session kind's name, which is why `kind(name)` and
`of_runner(word)` are two lookups. A **hand-out** is the app's answer to a free session's ask: the kind, and the exact
items claimed for it. A **claim** is that answer's row (`js_hand_outs`: the kind, the item ids, the runner, a two-minute
lease): until the session takes it, no other ask is handed those items. Both tables are in
`app/backend/app/jobsearch/work/session_kinds.py`, with the one order the kinds are handed out in. Spec:
`docs/architecture.md`.
The `titles` session kind (agent kind `title_checker`, "Title checker") no longer checks titles: it reads a role's header lines for the gate facts (place, pay, way of working) and tags a role with a target role, and the cost page counts it by the roles it read ("role read", shown as "Gate facts and role tags").

**Barred** (`work/availability.py`) is why Claude may not work now: the restart's drain, the global pause, Claude's peak
hours, Claude's usage limit, or no agent checked in. It is not "held", which is a role code (see below), and not a gate,
which is a hard gate on a role. `availability.barred(session, owner, question)` answers None when Claude may work;
`availability.standing` lists every reason that stands, for a caller with a rule of its own.

## Mission, and mission kind

A **mission** is a piece of the standing work the owner sees on Missions, one row each (`js_missions`): Read the employers'
boards, Read the mail, Research employers, Read in your browser, Scout the boards, Analyse the finds, Sweep up stuck runs and
Open my apply tabs. It has a schedule (every N hours, daily at a time, after another mission, only when run, or outside the
app's own for the browser), is on or paused, and keeps its **runs** (`js_mission_runs`) and its history of changes. A
**run** is one start of it: who started it (the schedule, you, Claude through the door, the browser's session, or the
import for the catch-up beside Analyse the finds' night), how it ended, and a JSON bag of what it kept (`started`). A
**night** is the stretch of work a Claude mission does; its **done conditions** (number of matches, most searches, roles or
employers to research, a finish time) say when it is over. A sentence names a mission by the work it does ("the scouting
mission"), never by its title.

A **mission kind** is one file in `app/backend/app/jobsearch/missions/kinds/` (one class, one instance, `KIND`): a
mission's **facts** (its name and the word a sentence uses, whether Claude's work and so held by peak hours, the robot
whose work it is, the hand-out the owner's Run now makes theirs, whether the app never starts it, its done conditions at
first) and its **procedures**: how it starts, what it does when a due start must wait or be skipped, how a running run
is brought up to date (**settle**), what the schedule pass does for a run that is still going (**tend**), what it gives
back when the finish time comes or Claude's usage runs out (**stop**), and what a restart cut short (**recover**).
`core.py` is the one interface: the schedule, pause, run now, the schedule pass and what the page reads; it reads only
the **common keys** of a run's bag (`record.COMMON_KEYS`) and every other key is its kind's own. A new mission is one
kind file, its key in `record.py`, one line in `core._wired()` and its wording. Spec: `docs/architecture.md`.

## Site to avoid, and a site read in your browser

In the app (`app/`), Where to look has two lists. A **site to avoid** is never read, and a posting whose link goes to
its host (or a subdomain of it) is stopped by the filter with the reason "a site you avoid". A site is never on both
lists. A site marked **read in your browser** (`read_by` = `browser`) is one a server cannot read (a page that needs a
sign-in, robots rules, a bot block): the night never fetches it and it gets no site project; the owner's own browser
reads it, in the app's one browser window, as items of the **browser queue**: small items (`source`, `read`, `scan`,
`map`) that a worker **leases** for 10 minutes, one at a time, with the app keeping each domain's pace and the tries
(the browser work spec, `docs/browser-reader.md`). Some careers pages list no jobs and hand off to the employer's own
listing on a job system: that listing is the site's **portal** (`js_sites.portal_url`).
The browser learns the site there, and what it reads there is the site's postings, filed under the site.

## A watched employer

An employer is **watched** when the switch on its own site is on ("Watch its job board" on its page); the night's read
reads only watched employers. The owner switches it, and with the setting "Watch the job board of every employer with a
role in play" (top of Employers, off until the owner switches it on) the app does too, for an employer with a role in
play (Found through Active Discussion). The app keeps who set a watch, so it unwatches only the ones it watched, once
none of the employer's roles is in play, and never switches back on one the owner switched off. A site is also switched
on the moment it holds a runbook (a reader for its job system, a learned skill or recipe), when nobody has decided about
it yet; no watch is kept for that.

## The log, and Not checked

The **log** is the posting index: every posting the app has read, from a board, a site or the owner's browser, and a
posting of every role (a tracker role's, whose system is `import`; a role the scout reported's, `scout`), each with the
role it became. A posting is **open** while its board still lists it and the app still reads that board, **closed** when
a whole read no longer finds it, and **Not checked** when the app no longer reads its board (the owner switched the
employer, the site or its job system off, the app turned the site off, or the employer is gated). A Not checked posting
is not closed, since nothing says the employer took it down: it keeps the day it was last seen, no stage works it and
the log counts it as open nowhere. Reading its board again settles it, open or closed.

## Gone Quiet, "No word came back", and "Still worth it?"

**Noise** (end reason `noise`, "Noise" on the Pipeline, `postings/noise.py`) is a role in Followup needed whose original
posting the app's code could not get: a role in Followup needed is sourced by code only, and after its tries the role is
closed (Gone Quiet), kept with the reason and a history line. The owner's own roles (starred, placed by hand, applied to,
with a packet, or from their tracker) are never closed as noise. Not the `noise` of `migrate/noise.py`, which is the raw
copy a migration drops.

Three rules end or question idle roles, in code, with the reason kept (`quiet.py`, `still_worth.py`). **The posting
went**: the night's read, after its filter, moves a role in play (Found, Needs Drafted, Ready to Apply or Applied, never
Active Discussion) to Gone Quiet when the employer's own board no longer lists its posting and nothing else says it is
still up. **No word came back** (end reason `no_reply`) moves an Applied role to Gone Quiet when it has had no word for
the owner's number of days ("Applied roles go quiet after" on Read the employers' boards), counted from the later of the
day it became Applied and its last matched mail (no mail is read yet, so for now from the Applied day). A rule never
moves a role the owner moved by hand since the evidence it acts on, and never moves one back out of Gone Quiet: the
owner does that. **Still worth it?** moves nothing by itself: a band on the Pipeline, above the rows of Ready to Apply,
asks about a role with nothing done on it for the owner's number of days ("Still worth it? after" on Forge the night's
packets). **Keep** leaves the role there and asks again after the same number of days; **Set aside** moves it to
Withdrew with the reason "Not known", which teaches the classifier nothing about the kind of job.

## Starter style rules, and the integrity checks

A **starter style rule** is one of five rules that ship with the app about how a packet reads: no em dash, no markup, no
year beside a degree, no phrase that concedes a gap first, no weak or inflated words (`style_rules.py`). Each starts on,
and the owner switches one off under Settings for QA of documentation (My Resume, Document Creation Rules); the packet
check holds the ones that are on against the words the writer prints. The **integrity checks** are three that are not a
setting: no certification or licence the master does not hold, no work that did not ship written as delivered, and no
line the master marks never in writing (`integrity.py`). A new install's Rules for ATS screening start with four plain
starter rules, saved once as version 1 of the writing rules; an install that already has writing rules is not given
them.

## Stop and re-gate

In the app (`app/`), a **stop** is a posting or a role a gate stopped, kept with its reason: the sentence the owner
reads, and as data the **step** that made it (a code gate, the title check, an agent's answer to a sentence, or the old
tracker's own screening), **which gate** and **which line** of Hard pass it was, and the **versions** of Hard pass and
What to look for it was judged under. A **re-gate** is the whole log and every role checked against the gates again, in
code, in the request that changed them: a new Hard pass, an employer gated or ungated, a site to avoid added or removed.
What a gate no longer stops goes back to where it would have been; what it now stops is stopped. A role at Applied or
later, a role the owner **moved by hand** (a move on the board; an ask for a packet is not one) and a role the old
tracker's own screening put in Gated out are never moved by one. Every such save says in one line what it changed
("Re-checked 2,500 postings and 100 roles: 30 now pass, 5 newly stopped."). The one gate check is `gating/check.py`;
the re-gate is `gating/regate.py`. (No sentence rule is saved or read since 2026-10-26; this stands for the answers on
record.) An agent's answer to a sentence rule is kept under the rule's **id and words**: a rule reworded since is a new
question, so the old answer stays on record, unused, and the rule passes until an agent answers it again, which only the
roles it reaches (read, in play, the rule applies, no answer in its words now) are sent back for, in the next night's
reading. Saving What to look for judges nothing again: it changes only what future searches look for. The app also runs
the re-gate and the re-score by itself, at every start and at the start of the night's read, when a release left stops
under an older Hard pass or scores under an older Prioritization (`keep_current.py`).

## Settled

A role is **settled** when its ask, then its score, then its gates have been worked out, in that one order, from what it
holds now (`research/settle.py`, `settle.roles`). The order is a data dependency, not a process order: the pay gate and
the score both read the worked **ask**, and the score and the gates never read each other. Whoever changes a role's
research, or a fact one of the three reads (a place or way of working filled, an employer's record, a corrected class),
settles the role with a cause and does nothing else. The ask is worked out again only when the research or the method
(`comp.METHOD_VERSION`) changed, never for a saved change to the ask's settings; the gates keep their own exclusions (a
role at Applied or later, one moved by hand, or one the old tracker or the employer limit stopped keeps its stage).
**`keep_gated_out`** is the one choice a caller makes: research taken on request and an employer split leave a role in
Gated out where it is, its ask and score worked out all the same. A save of Hard pass or Prioritization is not this: it
works the whole log out again, in a pass of its own (`regate.run`, `prioritize.rescore`).

## Placed, moved by hand, and held

A role the owner **moved by hand** is one they moved on the board (`board.move`, a `role_moved` line by the owner); an
ask for a packet is not one. That fact is never spent, and only the gates read it: no re-run of them moves such a role
from where the owner put it. A role is **placed** when it was moved by hand and is still where the owner put it, its
status unchanged since their last move. Once code has moved the role on (a migration, the stages since) the move is
spent: the stage rule and the queue treat the role like any other, though the gates still leave it alone. **Held** is
the wider word: a role code leaves where it is, whatever the reason. It is placed, or Go opened it, or the owner starred
it and it is read, or Go skipped it, or the owner's own data placed it, or a read of it is under way. One table says
which of those each question honours, and at which statuses (`pipeline/placement.py`, `HONOURED`); the questions are the
stage rule, the queue, an out-of-date read, the drop for a Must that waits, the employer limit and the gates.

## The Coach and interview prep

An **interview** is a record on a role in Active Discussion: when, the round (recruiter screen, hiring manager, panel,
executive or other, with the name the employer gives it), who is in it (a name and a title each) and the owner's notes.
There is no Interviewing status: Active Discussion stays the stage. Adding an interview whose time is ahead puts its
**prep** in front of the **Coach**, a session of the `coach` kind that researches the company and the interviewers on
public pages and writes a **report** in the owner's voice: the **Quick Sheet** (made by code), Open gaps, Core need,
What good looks like, a card for each interviewer (each with a "don't"), the Staples (opener, why here, why leaving,
pay), likely questions each with a story from the master resume or the story library, Problem to solve, Questions to ask
and Reference. **Code checks the report** (a claim about the owner cites the master or a story, no banned phrase, every
time-sensitive fact has a dated source, no job carries more than two answers) and sends it back until it passes; a
second session then reads it as the interviewer (the **red-team read**), and the report is filed. Each Prepare again is
a new **version**. The owner's **Rules for interviews** (My Resume, Document Creation Rules) are read only by the Coach.
There is no mock interview, download or Notion page.

## The employer researcher

The **employer researcher** is a small session that reads public pages for one employer at a time and reports its facts
(website, careers site, industry, size, ownership, headquarters, offices, founded, each with where it came from), its
**job board**, whether it is the same company as another record, and whether some of its roles belong to another
company. It is the analyst's role with an instruction file of its own, and the **Research employers** mission, nightly
after the boards are read, puts a batch of employers in front of it: those with a role in play first, then the most open
roles, then the never researched, none researched in the last so many days. **Code checks what it reports**, fact by
fact (a fact with no source, a careers link on a job board or a word for employees is refused and the rest stands), and
**decides what to do**: two records that share a website, or a job board beside names that look the same, merge by
themselves; any pair the app's checks do not show to be one employer stays two employers, and a split moves only the roles whose own postings show the other employer; nobody is asked. When an
employer's facts land, the gates and scores of its roles are worked out again. There is **no research bar**: every role
that passes the gates is researched.

## A role row

A **role row** is what the Pipeline list shows for one role: its title, company, stage, score, pay, age and a note only
when something is wrong or waits on the owner (`role_row.Row`; the page's `BoardCard`). It is made in one place
(`app/backend/app/jobsearch/pipeline/row.py`), so a column's card, a role's own page, the answer to a move and the
answer to a star are the same row for the same role, and the screen draws it without rewording it. It is not a **row**
of Prioritization (an ask with points: see the next entry), which the screens also call a row.

## The Ranked list, and Work in progress

The **Ranked list** is the Pipeline's one list of the roles First Impression, Second Impression and Ready to Launch
hold, sorted by each role's one number; every row says its stage. **Work in progress** is the roles still being got
ready (Followup needed, Landing Zone and Cleansed Data), which have no number yet. Each is a rail entry with its stages
under it, and the server gives each as one column (`GET /api/v1/board?column=ranked`, `?column=work`). A role's
**number** is the one the card prints: 0 until the role is ranked, Haiku's rank as written, then Sonnet's score once
read, with the first hours' bonus and less the stale penalty.

## The role's text, held and taken

A role's **text** is its posting's own words, whole and verbatim, from a source: a job system's own answer, the app's
own read of the role's source, or the owner's browser's capture of it. A listing's snippet is not one (under 300
characters from a job system, under 500 from a page or a capture), and neither is a page read whole (its markup and
menus), a scout's or a reader's report, the tracker's, or the analyst's summary of a page. What a role **holds**
(`posting_text.held`) is its own description when that is a text, else the fullest text a posting of the role holds,
else its own snippet, which the role page shows and nothing counts as a text (`whole.thin`). A text reaches a role that
exists in one way only, **taken** (`posting_text.take`): from the role's source, from what a posting of the role already
holds, or from a capture; taking it also keeps the raw copy, the posted day and what follows (research queued again, the
gate facts), so no caller says them. A role is made holding a posting's own whole text, and the readers write a
posting's text with its raw copy: those are not taking, nor are the three bridges that stay until nothing needs them
(`postings/text.py` says which they are). What counts as a text is one rule (`postings/whole.py`: `whole.why_not`,
`whole.of_posting`, `whole.thin` and, for a text handed in whole, `whole.refusal`).

## A row, a first match, an any of, graded requirements, decided

On Prioritization every ask is a **row**, and all rows add up (that sum is the "and"). Two kinds of row hold several
**statements**: a **first match** (an ordered list, each statement with its own points; the first one true of the
posting from the top decides, and the row's most is its largest positive statement) and an **any of** (one set of points
earned once when any one statement is true). The screens say "first match", "any of", "statement" and "row", and never
"group", "all of" or "ranked list" (on Prioritization; the Pipeline's Ranked list is another thing, defined above; in
the code a first match is kind `best`, an any of kind `any`). **Graded requirements** is the checkbox "Let the app grade
the requirements": the app gives each requirement the analyst rated Proven, Transferable or Unproven the points the
owner set (five numbers), decides it itself and never asks an agent. A row is **decided** when it can be settled from an
answer, the employer's record or the classes; **coverage** is the share of the points a role could earn that are
decided, and a fit is the points earned as a percent of the points a role could earn. There is **no partial credit**: a
met row earns its full points, and one that is not met, partly met, unknown or unanswered earns nothing, so a role whose
asks have not been checked scores low. A **suggestion** is a note left for the owner (put these asks in one row, or
remove asks the grading counts again); it changes nothing until the owner uses it and saves.

## The app's pages

The top bar of the app (`app/`) has two groups. At the left: **My Resume** (was Me; five tabs: Master resume, Find Gaps,
Resume Rules, Cover Letter Rules and Document Creation Rules), **Search Settings** (Where to look, What to look for,
Hard pass, Prioritization, About; was Profile), **Pipeline** and **Employers**. At the right end: **Notifications**
(what needs the owner, what the app has to tell them, what the agents have open and finished, past decisions; was Inbox,
and now holds what was the Tasks page) and **Agent Settings** (Your agents, How they work and Missions; was Agents, with
Missions moved in). The addresses keep the old names (`#/me`, `#/profile`, `#/inbox`, `#/agents` and `#/missions`). "The
profile" with a small p is still the match profile, the stored documents; Search Settings is the page that edits them.

## Catch-up

In the app (`app/`), the **catch-up** is what follows a tracker import that leaves roles to research: every role the
tracker has brought in that is still in play (Found, Needs Drafted, Ready to Apply, Applied, Active Discussion; not
Rejected, Gone Quiet, Withdrew or Gated out) and was never researched is researched by the analyst one after another,
straight away: applications first (Applied and Active Discussion, the most recent first), then Ready to Apply, then the
rest, the likeliest first. It is not the nightly Analyse the finds: it has no number for a night and no research bar
(there is none now, and an imported role has no score yet), and it shows on Missions as its own run, "the catch-up after
an import", in Analyse the finds' history. It is held by Claude's peak hours (when the owner avoids them) and by
Claude's **usage limit**, and picks up in the next window, not the next night. A role it researches keeps the stage it
is in; only a Found role moves. It also looks for the job system of each employer of those roles that has none
(`resolve.py`).

## Find Gaps, a theme, a sample

**Find Gaps** (the second tab of My Resume) reads a **sample** of postings for one role and says what they ask that the
master resume does not yet show. The sample is up to 10 stored postings, at most one from each employer (at least 5),
or, for a title that is not a target role, about 10 an agent finds on the public web. A **theme** is one ask as several
postings put it in different words; it has a kind (required, preferred or responsibility), how many of the sample ask it
("7 of 10") and a grade, **Proven, Transferable or Unproven**, in the analyst's own rules, against the whole master
resume. A target role keeps its latest run; the run goes stale when the resume changes (Check again grades the same
postings again; New sample draws others). **Not relevant** hides a theme for that role and touches nothing else; the
owner alone adds what is true, with **Add to resume**.

## Fresh read, and the Reviewer

A **fresh read** is a read of a document by a session that did not write it and was not given the writer's notes or
citations. The **Reviewer** is the runner role that makes one: of a packet, of the master resume and of an interview
prep report. The writer checking its own draft, or a linter passing, is not a fresh read. "Lint passed" means the
mechanical rules held; it says nothing about whether a claim is true, or the right claim for this posting.

## Proposal

An agent's suggested change to one of the owner's documents (the master resume, the profile, the writing rules), shown
as before and after for the owner to accept or reject. It is not an edit: a document changes only when the owner
accepts, and the version history says who suggested it. The coach, the reviewer and the resume passes all speak in
proposals.

## Standing variant

A saved cut of the master resume for one target role (a What to look for tab): which jobs lead, which bullets stay,
which summary opens. A packet is that variant tailored to one posting. It is not a second master: it only subtracts from
the master, so it can never hold a claim the master does not.

## Crib sheet, and the apply kit

The **crib sheet** is a one-page summary kept for each packet: the problem the employer is hiring to
solve, the pay line, the posting's hard gates and the form notes. In the app it is not a file. It is shown on the role
page as the **apply kit**, together with everything else the owner needs beside the form: the resume and letter to
upload, the references to attach, and the answers ready to paste. Neither the crib sheet nor the estimated ask is made
as a separate document: the ask is the role's Starting expectation tab. The interview prep report reads the same part
and does not copy it. No browser extension is part of this.

## Conversation

A turn-by-turn talk between the owner and the Coach: the build interview, the mock interview and the career coach. Each
of the owner's turns starts a short session and the transcript is kept. It is not a live chat session: nothing stays
open between turns.

## A to-do

A **to-do** is a small row the code writes in the same save as a change, saying "look at this" (never "do this"):
a topic (`role.settle`, `deck.dirty`) and a key (a role's id, empty for the whole owner, `*` for anything of the topic).
It lives in `js_todos` and belongs to the change-driven work queue (the work queue spec). It is internal and
never shown: it is not an **Inbox task** (a thing for the owner to do or decide, `Task`) and not a **plan task**
(`PlanTask`, a step of a site project). Since phase 2 a dispatcher takes some of them (the cheap filter, the making of
roles, the read of descriptions and two **ticks**: to-dos with a due time that post themselves again, a minute tick and
an hour tick); the hour's janitor deletes the old ones nothing takes.

## Page program, engine program, notice and bell

The app runs as two programs on one SQLite file. The **page program** (`app/backend/app/jobsearch/page.py`) is the one at the owner's address (port 8430): it serves the built page, every read, every click of the owner, the live stream and the owner's action door, and it runs no schedule and no work. The **engine program** (`engine.py`, the page's port plus 100) runs the schedule, the work queue, the fetchers and the doors the lanes use (`/mcp`, `/mcp-browser` and the runner's routes under `/api/v1/agent/`). Say "the engine program" for this process: "the engine" alone in older text can also mean the **process engine** (the vendored BPMN runner, `app/packages/process-engine`), which runs inside the engine program. The **notice** (`app/notice.py`) is the one small datagram the engine program sends the page program after it commits something the owner can see (ids and tags, never row data); the page reads the rows and pushes them to the open screens. A page that missed one, or an engine that restarted, is caught up by one **resync**, which makes the screens read again. The **bell** is the same kind of datagram the other way: a hint to the engine that there is work, never the only record of it (the work is a row first).
