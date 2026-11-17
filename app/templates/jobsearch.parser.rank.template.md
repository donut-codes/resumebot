<!-- Used by: the job-search app's parser, as the shape of one `jobs.parse_report` call for a batch of ranked postings.
     Loaded: by the app's hand-out of a batch of postings to rank (`jobs.parse_next`, backend/app/jobsearch/role_parse.py); named by instructions/jobsearch.parser.rank.instructions.md.
     Placeholders: none. Context keys: none. -->
id: "The id, exactly as jobs.parse_next gave it"
fields:
  ranks:
    - n: 1
      fit: 73
      why:
        - "What the posting is, in under 20 words"
        - "What decides the score, in under 20 words"
    - n: 2
      fit: 0
      why:
        - "Gate: the gate it clearly hits"
        - "What the posting is, in under 20 words"
