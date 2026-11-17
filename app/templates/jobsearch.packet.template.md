<!-- Used by: the job-search app's composer seat (the compose and once-over steps), as the shape of its one reply.
     Loaded: by the seat hand-out (backend/app/jobsearch/seats.py) at the steps of playbooks/jobsearch/build_packet.bpmn that name it.
     Placeholders: none. Context keys: none. -->
thread: "One sentence: the line from what this employer has to get done to what the candidate has already done."
resume:                                   # the app prints the name, contact line, each role's employer, title, place, dates and descriptor, and Education
  headline: "By the owner's headline rules."
  summary: "By the owner's summary rules: positions the candidate for this job; the lines prove, so it repeats none of them."
  summary_from: ["library or master line id"]
  competencies:                           # 3 to 4 labelled rows, 12 to 16 terms in all
    - area: "Area name"
      items:
        - {term: "a skill the library lists for a line you print, as written", backed_by: "that library line's id"}
  achievements: ["library line id"]       # only where the owner's rules call for them
  roles:                                  # the master's order, newest first; within a role, by the posting's qualifications
    - role: "master role id"
      intro: "the role's scope line id, or empty"
      lines: ["library line id", "library line id"]   # one line of a claim
blocks: ["library block id", "library block id"]   # two or three, at most one per story, in the letter's order
letter:
  paragraphs:                             # the greeting first ("Dear ..."); code adds the sign-off
    - {text: "Dear Hiring Manager,", from: []}
    - {text: "The opening, written for this job.", from: ["the ids of the blocks and lines whose facts it uses"]}
    - {text: "A block as approved, led in by a link written for this letter.", from: ["block id", "line id"]}
    - {text: "The close.", from: ["ids"]}
once_over:
  would_send_as_is: false
  changes: ["one short line each"]
