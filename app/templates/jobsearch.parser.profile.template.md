<!-- Used by: the job-search app's parser, as the shape of one `jobs.parse_report` call for the master resume's structured record.
     Loaded: by the app's hand-out of a master resume (`jobs.parse_next`, backend/app/jobsearch/role_parse.py); named by instructions/jobsearch.parser.profile.instructions.md.
     Placeholders: none. Context keys: none. -->
id: "The id, exactly as jobs.parse_next gave it"
fields:
  entries:
    - category: "areas, skills, tools, methods or domains"
      name: "One term, as the resume words it"
      lines: ["the id of a line that shows it", "another id"]
  certifications:
    - name: "A certification or licence the resume states"
      lines: ["the id of the line that states it"]
  highlights:
    - line: "The id of a line worth carrying into a comparison with a posting"
      terms: ["a name this line shows"]
