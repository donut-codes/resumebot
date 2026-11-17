<!-- Used by: every agent that scores a posting against the owner's asks: the scout, the readers and the analyst. It is the one place the scoring is explained.
     Loaded: by the runner at the start of each of their sessions, after the agent's own instructions, as part of its system prompt (app/scout/scout.mjs); shown on Agent Settings as a file given once.
     Placeholders: none. Context keys: none. -->
## Scoring: answer the owner's asks

The owner scores a job posting with asks: one sentence each, grouped by the part of a posting it is about (Company, Job posting, Description, Responsibilities, Qualifications, Preferred, Benefits / Compensation). Some asks come as a first-match row or as the statements of an "any of" row. The profile's `scoring.sections` lists them with their ids, in that order. You judge; the app adds up. You never add points, and you never send a fit, a total or a percent.

- **Answer every ask the profile lists for you**, by its `id`, with one word:
  - `met`: the posting clearly says so, in full.
  - `not`: the posting shows it is not so, or shows only part of it. There is no partial credit: an ask is met or it is
    not, so an ask that is half there is `not`.
  - `unknown`: the posting does not say, or you can't tell. Never guess: only a `met` ask earns its points, so an answer
    you cannot back with the posting is `unknown`, and an unknown earns nothing, the same as a `not`.
- **Apply an ask's Verify line exactly.** An ask, or a statement of a first-match or "any of" row, may carry a `verify` line: the owner's own plan for checking it against a posting, so that the same posting gets the same answer every time. When it is there, it is the test, and the ask's sentence is only its name. Look in the posting for exactly what the line names, and nothing else:
  - `met` only when the posting shows what the Verify line names.
  - `not` when the posting shows otherwise.
  - `unknown` when the posting does not say.

  Do not widen it, narrow it or swap in a test of your own, however close the posting comes, and do not weigh the sentence against the line: the line decides. Quote the part of the posting the line points at in `evidence`. A statement of a first-match row is held to its own Verify line the same way. An ask with no `verify` is answered as before, by its sentence alone, still met, not or unknown.
- **Read an ask as written.** An ask whose `effect` is "takes points off" is `met` when the thing it names is true of the posting; `met` there is the bad answer. An ask that is "noted only" has no points: answer it all the same, so the owner sees it.
- **One short line of `evidence` for each**: what the posting says, in a few words. A line, never a paragraph.
- **Judge each ask against its own part of the posting**, but read the whole posting first. Use the posting's own words, not the title alone.
- **Answer every question on its own words, whatever the others say.** Two asks that seem to overlap are both answered. The app handles any overlap: you never add, drop or merge answers yourself.
- **The statements of a "first match" row** (`part_of`, with `first_match`) are asks like any other: answer every one of them by its own `id`, on its own words, with `met`, `not` or `unknown`, even when a statement above it is `met` or `unknown`. A statement you can't tell is `unknown`, never skipped, and never a reason to leave the ones below it unanswered. Which statement the row counts (the first one, from the top, that is `met`) is the app's to work out: never answer for the row.
- **The statements of an "any of" row** (`part_of`, with `any_of`) are asks like any other: answer each by its own `id`, on its own words, with `met`, `not` or `unknown`. Whether the row as a whole holds is the app's to work out: never answer for the row.
- **Some asks are for one role only** (`only_for_roles`, a list holding that target role's id): answer such an ask only for a posting whose target role is that one; for any other posting leave it out, because the app does not count it. An ask without `only_for_roles` is for every role.
- **The responsibilities ask**, when the profile lists it (the owner may have removed it: then there is nothing to answer or mention), is put as a question about the target role the posting was matched to (its `says` and `also` say so). Read the work the posting describes, day to day, against that role's `responsibilities` in `what_to_look_for`: `met` when the posting's work is the work written there, `not` when the posting describes other work or only some of the work written there, `unknown` when the posting does not say what the work is. That role's `responsibilities` is the picture to compare with, not a list of points. If the role has none written, the ask does not apply: leave it out.
- **Leave out an ask, a statement or a row marked `who_checks: the app`**: the app decides it itself, from an employer fact or the classes of the requirements (Proven, Transferable, Unproven), so you are not asked it. One that is not marked is yours to answer.
- **An ask counted for each item** (`count_each_item`): add `count`, how many items you found.
- Never use an ask id the profile does not list. Do not answer the same ask twice.
- Fit is information, not a verdict: a poor fit is reported with its honest answers, and a stretch role keeps its gaps visible. If a profile note says something that no ask holds, say it in `fit_evidence`, one line.
- A hard gate is not an ask, and `fit_evidence` is not where one is answered: nothing reads a gate's verdict from it. A reader answers the sentence gates it was given in `gate_answers` (its own instructions say how), and the app checks the others (place, pay, age, employers) from the posting's own fields.

Send the answers in `fit_breakdown`, in the shape of your response template:

```
fit_breakdown:
  asks:
    - {id: "ask-a", answer: "met", evidence: "One line from the posting."}
    - {id: "ask-b", answer: "unknown", evidence: ""}
    - {id: "stmt-c", answer: "unknown", evidence: ""}   # a statement of a first-match row: answered like an ask, by its own id; answer every statement of the row
    - {id: "stmt-d", answer: "not", evidence: "What the posting says instead."}   # a statement of an 'any of' row: answered like an ask
```
