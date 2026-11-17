<!-- Used by: the job-search app's scout agent in a coach session, as the shape of one `jobs.coach_submit` call that sends the red-team read of a report (handed over in the `jobs.coach` reply, mode review).
     Loaded: by jobs.coach (backend/app/jobsearch/coach.py); named by the "Reviewer reads" step of playbooks/jobsearch/prepare_interview.bpmn.
     Placeholders: none. Context keys: none. -->
id: "the prep's id from jobs.coach"
review:
  summary: "One or two sentences: whether the report holds up as the interviewer would read it, and where it is weakest"
  findings:                              # leave empty when you found nothing
    - where: "The section and the item, as 'questions: Tell me about a launch'"
      problem: "What would not hold up, and why"
      fix: "What would fix it"
      action: "Only when the owner can still do something before the call: one plain sentence"
