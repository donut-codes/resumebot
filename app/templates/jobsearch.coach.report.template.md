<!-- Used by: the job-search app's Coach session (kind `coach`), as the shape of one `jobs.coach_submit` call that sends a report (handed over in the `jobs.coach` reply, mode write).
     Loaded: by jobs.coach (backend/app/jobsearch/coach.py); named by the "Coach writes" step of playbooks/jobsearch/prepare_interview.bpmn.
     Placeholders: none. Context keys: none. -->
id: "the prep's id from jobs.coach"
report:
  core_need:
    text: "One sentence: the problem this hire is paid to solve, from the posting's duties, not its title"
    from_title_only: false               # true only when the posting text is missing and you worked from the title
  good_looks_like:                       # what a person must know to be effective on day one, researched for this role and industry
    - {text: "A point, in a sentence", source: {url: "https://...", title: "The page", checked_on: "YYYY-MM-DD"}}   # source: leave out for plain judgement
  stage_read:
    screens_for: "What this round screens for"
    interviewers:                        # one card for every named person, in the order given
      - name: "As the task gave it"
        found: true                      # false when public pages show nothing; then leave background empty
        background: "Their role and background, from public pages"
        screens_for: "What their part of the round screens for"
        connection: {text: "One link to the owner's material", from: ["master line id"]}
        dont: "One thing not to do with this person"
        questions: ["Two or three questions this person is likely to ask"]
  staples:
    opener:                              # 120 to 240 words in all, about 60 to 90 seconds spoken: present, relevant past, why this role, ending on the core need
      - {text: "A sentence in the owner's voice", from: ["master line id"]}
    why_here:                            # why this company and role, one level deeper than its About page
      - {text: "An employer fact", source: {url: "https://...", title: "The page", checked_on: "YYYY-MM-DD"}}
      - {text: "The owner's relevant past", from: ["master line id"]}
    why_leaving:                         # in the owner's voice; or, when the master doesn't say why, {ask_owner: "What the owner must write"}
      text: "One sentence"
      from: ["master line id"]
    market:                              # the pay range for this role; never the owner's own numbers
      low: 0
      high: 0
      basis: "base"                      # base, total or hourly
      source: {url: "https://...", title: "The page", checked_on: "YYYY-MM-DD"}
  questions:                             # the questions likely to be asked, each with the one answer the owner would give
    - question: "The question"
      asked_by: "A name from the interview, or leave out"
      method: "What the interviewer is really asking for"
      answer:                            # a story or a judgement as the owner's Rules for interviews say; stories when there is no rule
        kind: "story"                    # story, or judgement (the answer and a one-clause receipt)
        title: "A short name"
        text: "What happened, what the owner did, in their voice"
        result: "A result someone could check"
        from: ["master line id or library story id"]
      gap: "When there is no real story: what the owner can still do before the interview. Leave answer out then. Never invent a story."
  problem:
    kind: "approach"                     # approach (a role that builds or runs something) or first_engagement (an individual contributor)
    approach: "How the owner would approach it, as an approach and not a prescription"
    discovery_questions: ["What they would need answered in the first 30 days"]
  ask:                                   # questions for the owner to ask: diligence, not performance
    - question: "The question"
      round: "hiring_manager"            # recruiter_screen, hiring_manager, panel or executive
      priority: 1                        # 1 first choice, 2 second
      diagnoses: "What it tells the owner about the place"
      good_answer: "What a good answer sounds like"
      bad_answer: "What a bad answer sounds like"
      follow_up: "The one follow-up that stops a rehearsed answer"
  reference:                             # facts about the employer and the market; each needs its source
    - {text: "A fact", source: {url: "https://...", title: "The page", checked_on: "YYYY-MM-DD"}}
  open_gaps:                             # only what the owner can still do before the call
    - {text: "What is missing", action: "What to do about it"}
