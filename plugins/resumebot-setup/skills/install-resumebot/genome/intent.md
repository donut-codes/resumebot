# Intent Spec: install-resumebot

Spec version: 1.1
Current phenotype: SKILL.md (as published)
Owner: the skill's user
Replayable: mostly -- the work is `resumebot <verb>` commands whose results are checked by `resumebot doctor`; the
judgment is in asking first, finding the repository address from the marketplace, and picking the machine's time zone
when the app cannot.

## Purpose [INVARIANT]

End at a running resumebot app on the user's own computer, connected to the user's Claude Code. The agent runs every
command; the user is never given a command to type or paste.

## Inputs [INVARIANT]

- The user's one answer up front: their name, and whether the app starts at sign-in; plus a yes to the plan.
- The machine: which prerequisites exist (git, uv, Python 3.12, Node 22 or newer, Claude Code).
- A release tag of the app, never a branch, in the repository the user's clone came from, else the one the plugin's own marketplace came from.

## Success criteria [INVARIANT]

1. `resumebot doctor` prints no MISSING line and `resumebot status --for-claude` says the app is running.
2. No token or password is printed in the chat, and none passes through the agent.
3. The user's only actions are: answer the question, approve the sign-in in the browser (the window the app opens
   closes itself; a paste only if it asks), restart Claude Code.
4. Nothing is installed or changed that the user was not told about first; PATH, environment and the logon task are
   the user's own, never the machine's.
5. Code and data live in separate folders; the user's data is never deleted, overwritten or renamed unless they ask. One
   flow installs, finishes a half-made install and updates (the code folder is replaced, a copy of the data folder is made
   first), and every command in it is safe to run twice.

## Out of scope [INVARIANT]

Onboarding. Writing the helper agents into the user's project is the
`getting-started` agent's job, not this skill's.
