# Contributing

resumebot is a product other job seekers install, so a change is judged by whether it is right for anyone, not for one
setup. Open an issue before a large change.

## Run the checks

From the repository root (it needs Python, and Node for the page; nothing is installed system-wide):

```
py -3 app/scripts/dev.py check
```

That is the one check command. It runs Ruff, basedpyright, the backend tests, the process engine's tests, the scout's
Node tests, the page's type check, ESLint, the page's tests and ShellCheck. The backend tests need no database server and
no `DATABASE_URL`: each test works on its own SQLite file. To run part of it, add `--backend`, `--frontend` or
`--runner`, or name test paths.

Three of the gates compare against a committed baseline of old findings (`app/backend/.basedpyright/baseline.json`,
`app/frontend/lint/eslint-suppressions.json`, `app/scripts/shellcheck-baseline.txt`). A new finding fails; fix it. Never
add to a baseline. After you fix old findings, shrink the baseline.

Dependencies are hash-locked: after changing `app/backend/pyproject.toml`, run `py -3 app/scripts/dev.py lock` and commit
the lock files.

Two checks guard this repository itself, with the standard library only:

```
py -3 scripts/check_no_one.py        no one's machine, address, employer or search in the repository
py -3 scripts/check_migrations.py    a released migration is never edited, deleted or renamed
```

## Tests

- **Add.** A behaviour change or a bug fix ships with a test that fails without it. For a bug, write the failing test
  first. One behaviour per test, named for it. Use the smallest layer that can fail: a pure function, a service with the
  database, an API route, a UI component.
- **Make them safe.** Tests set up their own data and run in parallel workers. No network, no model calls, no sleeps; the
  clock is injected. Fixtures hold no real person's name, employer or place.
- **Remove** a test only when the behaviour it pins is removed or moved, or when it duplicates another. Say which in the
  commit message. Never delete, skip or loosen a test to get past a gate, and never retry a flaky one: fix it or delete
  it.
- **Edit** a test only when the behaviour changed on purpose, in the same commit as the code, and say the old and the new
  behaviour. A refactor changes no test.
- **Keep it fast.** The whole check should stay near ten minutes. A new test over a second needs its reason in the commit
  message.
- Migrations only go forward. Nothing runs a downgrade.

## Rules for the product

- **Built to share.** No one's preferences go in code, defaults, tests or fixtures. Every target, gate, weight, place and
  pay figure is the user's data, set in the app. A rule that exists for one user is a setting, never a special case.
- **The database is the source of truth.** Files are outputs only: exports and documents.
- **Nothing signs in, applies, fills a form, solves a CAPTCHA or gets around a block.** Code that reads a site honours its
  robots.txt, keeps to about one request a second per host, and stops when it is refused.
- **The app never rewords the master resume.** Only the owner changes it.
- **Every role is kept, with a reason.** A screened-out role is stored and shown with the gate that stopped it.
- **The order of the work lives in the BPMN files** in `app/playbooks/`; the process engine decides what comes next.
  Agents do the judgement (finding, scoring); the rest (de-duplicating, gating, exporting) is code. Do not move ordering
  into prompts or judgement into code.
- **Build what is asked.** No extra features, controls, options or switches. If something extra seems worth having, ask
  first. A bug fix is not a feature.

## Branches and pull requests

- Branch from `main` as `feat/<name>`, `fix/<name>`, `chore/<name>` or `docs/<name>`, and open a pull request.
- Commit subjects read `<area>: <what changed, lowercase, imperative>`, under about 72 characters (`app:`, `docs:`,
  `chore:`, `scripts:`). The body says why, what was wrong, and what you ran, with counts.
- A branch never changes the version number; a release does.
- Never force-push `main`.

## Where things are

[`docs/architecture.md`](docs/architecture.md) says how the app is built and where the code lives. [`GLOSSARY.md`](GLOSSARY.md)
is its vocabulary: a human and an agent should mean the same thing by the same word.
