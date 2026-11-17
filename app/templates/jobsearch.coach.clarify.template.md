<!-- Used by: the job-search app's scout agent in a coach session, as the shape of one `jobs.coach_submit` call for a Find Gaps job (handed over in the `jobs.coach` reply).
     Loaded: by jobs.coach (backend/app/jobsearch/gap_coach.py); named by playbooks/jobsearch/clarify_gaps.bpmn.
     Placeholders: none. Context keys: none. -->
id: "the job's id from jobs.coach"
questions:                                # mode clarify_ask: one or two for every theme in the task, none for any other
  - theme: "governance-and-risk"          # the theme's id, exactly as the task gave it
    question: "Your resume says you led the risk working group. Did that include reviewing a program's risks for a board or a client, and what changed because of it?"
  - {theme: "budget-ownership", question: "Have you owned a budget anywhere, in any job or project? How large, and who signed it off?"}
proposals:                                # mode clarify_propose: master lines from the owner's answers; an empty list when none is worth adding
  - card: "the answer's card id from the task"
    text: "ran the quarterly risk review for the board and cut its prep time in half"   # the owner's answer cut at its ends, nothing else
    place: {kind: "new_line", job: "the job's id from the master"}                       # a new line under that job...
    why: "One short line: what it shows for the theme."
  - card: "another card id"
    text: "cut its prep time in half"
    place: {kind: "evidence", job: "the job's id", line: "the id of one of that job's bullets it backs"}   # ...or evidence on one of its lines
    why: ""
# Send questions in mode clarify_ask and proposals in mode clarify_propose, never both.
