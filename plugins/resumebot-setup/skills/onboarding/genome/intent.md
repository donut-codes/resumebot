# Intent Spec: onboarding

Spec version: 0.1 (draft)
Current phenotype: SKILL.md (as published)
Owner: the skill's user
Replayable: partly -- `resumebot status --for-claude` says the next step mechanically; how each step is carried out is a
conversation.

## Purpose [INVARIANT]

Take a user with the app installed, connected and running from nothing to a first search: master resume, target roles
and search settings, first search, and optionally mail.

## Inputs [INVARIANT]

- The output of `resumebot status --for-claude` (resume, targets, first search, mail, next).
- What the user says: their resume, their targets, their choices. Nothing else is a source for a saved value.

## Success criteria [INVARIANT]

1. `status` says `next: onboarding is done`.
2. Every saved target, gate, weight, place and pay figure is what the user said; none is suggested by Claude.
3. The master resume is never reworded; an import is saved only after the user confirms the app's review.
4. No password is asked for in the chat; mail goes through the app's own page.
5. Each step is done through the owner door's action that `status` names (`resume.import`, `profile.read` and
   `profile.save`, `mission.run` with `scout_boards`, `mail.state`); the files the user brings go in through
   `tracker.import` and `employers.import`. A step with no action is never done by another route: the user is sent to
   that page of the app.
6. The door never saves the resume and never changes a site's read-anyway switch; the skill never asks it to, and never
   offers or suggests reading any site that is not already on their list.
