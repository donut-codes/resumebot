# Onboarding

Onboarding is the first run: getting your resume, your targets and a first search into the app. Your Claude walks you
through it, one step at a time, using the `onboarding` skill of the setup plugin
([`plugins/resumebot-setup/skills/onboarding/SKILL.md`](../plugins/resumebot-setup/skills/onboarding/SKILL.md)). Nothing in
it is required to be done by Claude: every step can be done in the page instead.

## Start it

After the install (Claude runs every command) and the restart of Claude Code, the plugin prints where you stand at the start of every session. Say
`continue onboarding`, or `what's next with resumebot`, any time. To see the same thing yourself:

```
resumebot status
```

Its `next:` line names the first step still to do. Claude works on that step and re-reads the status after each one. The
same lines are available to Claude through the owner door as `act.read onboarding.state`.

## The steps, in order

Claude says what each step is for, does it with you, tells you what changed and offers the next. The choices are yours:
every target, gate, weight, place and pay figure is what you say, saved as you say it. Claude does not suggest numbers or
places of its own.

1. **Import your master resume.** You give Claude the whole path of your resume file (`.docx`, `.pdf` or `.txt`), or paste
   the text and Claude saves it as a `.txt` file. The app starts an agent reading it and gives Claude a page address. You
   open it and look at what would be added to your master resume. Repeats are skipped and near repeats are kept beside the
   line they resemble. Nothing is deleted or reworded. **Nothing is saved until you confirm it on the page and save.**
   Claude does not confirm it for you. If you have no resume, you can write one by hand on My Resume.
2. **Bring in what you already have (only if you have it).** A tracker spreadsheet from an earlier resumebot (`.xlsx`), and
   an employer list (`.csv` with a name column and a website column). Claude asks once and does a dry run first, telling
   you the counts. A real tracker import starts researching the roles in play, which uses your Claude allowance. Running
   either again changes nothing the app has worked on. The employer list only gives the app a first clue about where an
   employer's job board is. Skip both if you have nothing.
3. **Set your target roles and search settings.** Claude reads what is saved, then asks which roles you want and what titles
   they go by, what kind of employer, where and how you will work, and the lowest pay you need a year in dollars. It saves
   each answer as you give it, as a new saved version, and reads the result back in plain words for you to correct. It
   does not offer to read a board whose terms deny automated reading.
4. **Run the first search.** The search reads public job pages and takes a while. Claude asks for a dry run first, which
   says whether it would be refused (the search paused, Claude's peak hours, a search already running), then runs it and
   follows it. When it finishes Claude shows what it found: how many postings were seen, how many are new, how many were
   kept, and how many each gate screened out. A screened-out role is kept with the gate that stopped it.
5. **Mail (optional).** If you want replies read, the app can read your mailbox, read-only. Claude gives you the steps for
   your provider to make an app password and the address of the page where you type your mail address and that password
   into **Your email**. The password is typed on the page and never into the chat, and Claude never types it for you.

When the status says `next: onboarding is done`, Claude says so and stops.

## Helper agents in your project

`resumebot project` writes three agents into your project folder's `.claude/agents/`. Each reaches the app through the
same owner door, applies to nothing and submits nothing, and asks you before it saves anything: a dry run first, read
back in plain words, then a save only after your yes. Nothing in the app's page needs them.

- **career-coach** works out what roles to target with you and saves your profile (target roles, where you will work, pay,
  scoring) after your yes.
- **master-resume-help** reads your master resume and asks about the work behind each job (it reads the master with `resume.read`, looks at real postings with `postings.sample` and suggests with `resume.propose`). What it adds is your own words,
  quoted, as suggestions that wait on **My Resume** for you to accept, edit or reject. It never rewords or saves the master.
- **profile-draft** writes `profile-draft.md` in your project folder for you to paste into your network profile by
  hand. It never touches your live profile.

## Doing a step in the page

| Step | Where in the page |
|---|---|
| Import your resume | My Resume: the **Import your resume** button |
| Target roles and search settings | Search Settings |
| First search | Agent Settings, Missions: run **Scout the boards** with Run now |
| Mail | Agent Settings, **Your email** |
| Tracker and employer list | no screen; ask Claude, who does it through the owner door |

The agents must be signed in (`resumebot signin`, which the install runs) for any step that starts an agent (the resume
reading, the search): see [`connect-claude.md`](connect-claude.md). If a step is refused, the answer says why in words, for example Claude's peak
hours; try again when it has passed.

## After onboarding

The app searches on its schedule, and Claude, when you ask, can read what it found. What to do each day is in
[`agent-guide.md`](agent-guide.md).
