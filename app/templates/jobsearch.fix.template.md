<!-- Used by: the job-search app's composer seat at a fix step (the named lines only), as the shape of its one reply.
     Loaded: by the seat hand-out (backend/app/jobsearch/seats.py) at the fix steps of playbooks/jobsearch/build_packet.bpmn.
     Placeholders: none. Context keys: none. -->
resume:                                   # the whole resume, in the shape it was given, with only the named lines changed
  headline: "..."
  summary: "..."
  summary_from: ["master line id"]
  competencies:
    - area: "Area name"
      items:
        - {term: "Term", backed_by: ["master line id"]}
  achievements:
    - {text: "...", from: ["master line id"]}
  roles:
    - role: "master role id"
      intro: "..."
      intro_from: ["master line id"]
      bullets:
        - {text: "...", from: ["master line id"], lib: "the library id, kept as given"}
letter:
  paragraphs:
    - {text: "...", from: ["master line id"]}
