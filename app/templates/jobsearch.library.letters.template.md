<!-- Used by: the job-search app's library writer seat (the letter blocks), as the shape of its one reply.
     Loaded: by the seat hand-out (backend/app/jobsearch/seats.py) at the letters step of playbooks/jobsearch/build_library_part.bpmn.
     Placeholders: none. Context keys: none. -->
stories:                                  # only the stories you were asked to write, and new ones
  - id: "short-slug"
    title: "A few words"
    index: "One line: what the story shows a reader"
    blocks:
      - for: "the kind of role this angle serves"
        sentences:
          - {text: "...", from: ["master line id"]}
