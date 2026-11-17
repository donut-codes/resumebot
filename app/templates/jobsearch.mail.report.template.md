<!-- Used by: the job-search app's scout agent in its analyst role, as the shape of one `jobs.mail_report` call (handed over in the `jobs.mail` reply).
     Loaded: by jobs.mail (backend/app/jobsearch/mail_haiku.py); named by playbooks/jobsearch/read_mail.bpmn.
     Placeholders: none. Context keys: none. -->
answers:
  - id: "the message's id from jobs.mail"
    kind: "confirmation"          # confirmation, rejection, interview, offer, outreach, other or ambiguous
    employer: "the company the message is about, as roles_in_play says it; leave out when it does not say"
    title: "the job title it is about; leave out when it does not say"
    why: "The sentence of the message you decided from, copied word for word from its subject or text (a paraphrase is refused). No addresses, numbers or links."
  - id: "another message's id"
    kind: "interview"
    employer: "Company"
    title: "Job title"
    why: "Can you share two times next week for a call with the hiring manager?"
    when: "2026-11-05T14:00"       # only when the text names a date and time clearly
    round: "hiring_manager"        # recruiter_screen, hiring_manager, panel, executive or other
  - id: "a third id"
    kind: "outreach"
    employer: "Company"
    title: "Job title"
    why: "I came across your profile and have a Product Lead role I think fits."
    link: "https://... the posting's address, copied exactly from the text; leave out when there is none"
