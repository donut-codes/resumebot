<!-- Used by: the job-search app's scout agent in its analyst role, as the shape of one `jobs.employer_report` call (handed over in the `jobs.employer` reply).
     Loaded: by jobs.employer (backend/app/jobsearch/employer_research.py); named by playbooks/jobsearch/research_employer.bpmn.
     Placeholders: none. Context keys: none. -->
employer: "the employer's id from jobs.employer"
facts:                                    # only what you found; leave out what you couldn't
  website: "https://... the employer's own site, never another organisation's"
  careers_url: "https://... the page listing all its jobs, on its own site or its job system; never a job board"
  reviews_url: "https://... its page on an employer review site, only as a web search result showed it; never opened"
  industry: "Plain words"
  sectors: ["Sector", "Sector"]
  employees: 1200                         # a number
  ownership: "private"                    # public, private, pe, vc, nonprofit or government; leave out when unsure
  headquarters: "City, ST"
  offices: ["City, ST", "City, ST"]
  founded: 1998
  ai_native: false                        # only when you are sure
  events: []                              # only when you searched for layoffs: [] means searched and found none (give sources.events); leave out when you did not search
sources:                                  # where each fact came from, by field: a web address or words; a fact with none is refused
  website: "https://... the page that showed it"
  employees: "https://..."
  industry: "its About page"
board_url: "https://... the employer's job system page, only when you saw it; the app checks it"
same_as:                                  # a verdict for each employer in may_be_the_same_as; same only on evidence they are one employer, not a shared website
  - {employer: "the other record's id", verdict: "same", reasons: ["The evidence they are one employer, and where: its About page says the two names are one company"]}
  - {employer: "another record's id", verdict: "different", reasons: ["Why: a dental practice, not the same company"]}
split:                                    # roles that belong to another company; the owner decides
  - {roles: ["role id", "role id"], new_name: "The other company's name", reasons: ["What shows it, and where"]}
notes: "One or two lines for the owner, or leave out."
