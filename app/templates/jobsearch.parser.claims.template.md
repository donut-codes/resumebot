<!-- Used by: the job-search app's parser, as the shape of one `jobs.parse_report` call for the master resume's claims.
     Loaded: by the app's hand-out of a master resume for its claims (`jobs.parse_next`, backend/app/jobsearch/role_parse.py); named by instructions/jobsearch.parser.claims.instructions.md.
     Placeholders: none. Context keys: none. -->
id: "The id, exactly as jobs.parse_next gave it"
fields:
  claims:
    - claim: "The category that follows 'Experience with', a few words, no year"
      examples: ["a brief example in the master's own words", "another"]
      lines: ["the id of a line that shows it", "another id"]
