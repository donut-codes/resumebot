<!-- Used by: the job-search app's parser, as the shape of one `jobs.parse_report` call for a posting's structured record.
     Loaded: by the app's hand-out of a posting's text for its record (`jobs.parse_next`, backend/app/jobsearch/role_parse.py); named by instructions/jobsearch.parser.structure.instructions.md.
     Placeholders: none. Context keys: none. -->
id: "The id, exactly as jobs.parse_next gave it"
fields:
  requirements:
    - text: "One thing the candidate must have, in a few words"
      quote: "The line or phrase of the posting it rests on, copied word for word"
  duties:
    - text: "One thing the job does"
      quote: "The line or phrase it rests on, word for word"
  preferred:
    - text: "One nice-to-have"
      quote: "The line or phrase it rests on, word for word"
  keywords:
    - term: "A term the posting asks for"
      importance: "required or preferred"
      quote: "The line or phrase it rests on, word for word"
  certifications:
    - name: "A certification or licence the posting names"
      required: true
      quote: "The line or phrase it rests on, word for word"
  areas:
    - area: "An area of experience that states a number of years"
      years: 5
      quote: "The line or phrase that states the years, word for word"
  clearance:
    value: "The clearance the posting states"
    quote: "The line it rests on, word for word"
  degree:
    value: "phd, master, bachelor, associate or degree"
    quote: "The line it rests on, word for word"
  level:
    value: "The seniority the posting states"
    quote: "The line it rests on, word for word"
