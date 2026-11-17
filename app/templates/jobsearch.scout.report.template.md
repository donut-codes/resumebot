<!-- Used by: the job-search app's scout, as the shape of its one `jobs.report` call (handed over in the `jobs.profile` reply).
     Loaded: by jobs.profile (backend/app/jobsearch/tools.py) from the open search's step; named by playbooks/jobsearch/search_round.bpmn.
     Placeholders: none. Context keys: none. -->
roles: []   # the scout judges no posting: the readers' reports are already staged by the app
done: true  # the scout's one call, when it has found the boards it can
