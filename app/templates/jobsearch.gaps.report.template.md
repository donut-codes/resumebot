<!-- Used by: the job-search app's scout agent in its analyst role, as the shape of one `jobs.gaps_report` call (handed over in the `jobs.gaps` reply).
     Loaded: by jobs.gaps (backend/app/jobsearch/gap_tools.py); named by playbooks/jobsearch/find_gaps.bpmn.
     Placeholders: none. Context keys: none. -->
id: "the run's id from jobs.gaps"
postings:                                 # only for a title searched on the web (mode search): about 10, one from each employer, 5 at least
  - {employer: "Employer name", title: "The posting's title", url: "https://... the page you read"}
themes:                                   # the asks that repeat across the postings, one theme for one ask
  - name: "The ask in plain words"        # short; the same name for the same ask, and a theme the owner answered keeps its name exactly
    kind: "required"                      # required, preferred or responsibility
    asked_by: ["p1", "p3", "p4"]          # the refs of every posting that asks it
    grade: "P"                            # P Proven, T Transferable, U Unproven
    from: ["master-line-id"]              # the master lines that show it (a P) or bridge to it (a T); none for a U
    why: "One short line: how the line shows it, or what the bridge is."
  - {name: "Another ask", kind: "preferred", asked_by: ["p2", "p5"], grade: "U", from: [], why: ""}
# A drafted line (mode draft) is sent as: {id: "the request's id", draft: "One plain sentence, from the owner's words alone."}
