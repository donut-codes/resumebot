<!-- Used by: the job-search app's library writer seat (the resume lines), as the shape of its one reply.
     Loaded: by the seat hand-out (backend/app/jobsearch/seats.py) at the lines step of playbooks/jobsearch/build_library_part.bpmn.
     Placeholders: none. Context keys: none. -->
rules:                                    # what the owner's own rules say, for code to enforce on every line
  protected: ["each name or word the owner's rules keep out of writing, in every form the master writes it"]
  approved: ["each wording the owner's rules or line notes say to use exactly as written"]
  roles:                                  # only what the owner's rules say; null where they say nothing
    - {role: "master role id", min: 3, max: 5, scope: true, firm_first: false}   # scope: its scope line always prints; firm_first: work owned inside the employer before client work
scopes:                                   # only for the roles asked for
  - {role: "master role id", text: "One plain sentence a reader wants under the title.", from: ["master line id"]}
entries:                                  # every entry you were given, decided
  - entry: "entry id"
    status: bullet                        # bullet | folded | skip
    into: "entry id (folded only)"
    reason: "skip only: one short reason"
    bullets:
      - role: "master role id, or null for an achievement"
        claim: "short-slug-of-the-accomplishment"   # lines of one accomplishment share it; a resume prints one of them
        side: "firm"                      # firm | client | empty
        for: "the kind of role it serves"
        skills: ["Skill a recruiter searches for"]  # one to three, each shown by this line
        text: "Past-tense verb first: action, scope, result."
        from: ["master line id"]
