---
vocab: porter.core
terms:
  - {id: site, term: site, means: "An address the owner wants searched, with its kind (an employer's careers site, a job board or an association's board). Each site is a project.", avoid: ["source", "board"], group: object}
  - {id: plan, term: plan, means: "The list of items for a project round; the owner approves it only when the site needs them.", avoid: ["proposal", "draft"], group: object}
  - {id: plan_version, term: plan version, means: "One numbered write of the plan (v1, v2...); a revision makes a new version.", avoid: ["revision number"], group: object}
  - {id: item, term: item, means: "One unit of work in a plan, with a kind and the data that kind requires.", avoid: ["ticket", "step"], group: object}
  - {id: kind, term: kind, means: "The named type of an item, defined by a kind file (data, do, done means, check).", avoid: ["type", "action"], group: object}
  - {id: task, term: task, means: "An item once started and handed to the scout to carry out.", avoid: ["job"], group: object}
  - {id: round, term: round, means: "One pass through plan, do, check, act.", avoid: ["lap", "iteration", "cycle"], group: object}
  - {id: skill, term: skill, means: "The saved recipe the app runs to read a site: a known job system's adapter, a data request, or list pages. Each round that builds one saves a new version.", avoid: ["plugin", "prompt", "script"], group: object}

  - {id: phase_plan, term: plan (phase), means: "The scout writes or revises the plan; the owner approves it only when the site needs them.", group: phase}
  - {id: phase_do, term: do, means: "The scout builds the site's skill and tries one real read.", group: phase}
  - {id: phase_check, term: check, means: "Code reads the site through the skill and tests what came back against the item's check.", group: phase}
  - {id: phase_act, term: act, means: "The scout turns the round's findings into lessons and keeps the skill.", group: phase}

  - {id: scout, term: scout, means: "The agent that writes the plan, builds the skill and does Act.", group: role}
  - {id: resumebot, term: resumebot, means: "The deterministic core: keeps state, checks plans, runs the skills and the clock. The check is its own.", avoid: ["the system", "the backend"], group: role}
  - {id: owner, term: owner, means: "The person who approves, answers and decides.", avoid: ["user", "customer"], group: role}

  - {id: done, term: done, means: "Do outcome: the item was attempted and its done-means holds.", avoid: ["complete", "succeeded"], group: outcome}
  - {id: off_plan, term: off plan, means: "The item was attempted (Do) or tested (Check) and the result is not what the plan said.", avoid: ["failed", "error", "broken"], group: outcome}
  - {id: not_attempted, term: not attempted, means: "Do outcome: nothing was tried, for example the scout can't run the kind.", avoid: ["failed", "skipped"], group: outcome}
  - {id: on_plan, term: on plan, means: "Check outcome: the item's check test passes.", avoid: ["verified", "passed"], group: outcome}
  - {id: not_checked, term: not checked, means: "Check outcome: the test could not run, or the item had no attempt to test.", avoid: ["unknown"], group: outcome}

  - {id: awaiting_approval, term: awaiting approval, means: "Plan status: written and waiting in the owner's Inbox, because the site needs them.", avoid: ["pending"], group: status}
  - {id: approved, term: approved, means: "Plan status: the owner said yes, or the plan needed no asking; items are running or ready to run.", avoid: ["running"], group: status}
  - {id: sent_back, term: sent back, means: "Plan status: the owner asked for changes; the scout writes the next version.", avoid: ["rejected"], group: status}
  - {id: declined, term: declined, means: "Plan status: the owner said no and no further version is written.", avoid: ["rejected", "cancelled"], group: status}
  - {id: replaced, term: replaced, means: "Plan status: a newer version superseded this one.", avoid: ["obsolete"], group: status}

  - {id: decision, term: decision, means: "A question only the owner can answer, asked once.", avoid: ["prompt"], group: object}
  - {id: option, term: option, means: "One possible answer to a decision; each maps to a kind or an instruction.", avoid: ["choice"], group: object}
  - {id: default, term: default, means: "The option resumebot proposes, shown with every decision.", avoid: ["recommended"], group: object}

  - {id: lesson, term: lesson, means: "What to do differently, with when, do, evidence and what it changes.", avoid: ["note", "insight"], group: object}
---

Use these words and no others for the same thing. "Failed" is never an outcome here: say off plan, not attempted or not checked.
