<!-- Used by: the job-search app's resume parser, as the shape of one `jobs.parse_report` call for a resume.
     Loaded: by the app's hand-out of a resume (`jobs.parse_next`, backend/app/jobsearch/role_parse.py); named by instructions/jobsearch.parser.resume.instructions.md.
     Placeholders: none. Context keys: none. -->
id: "The id, exactly as jobs.parse_next gave it"
fields:
  summary: "The summary paragraph, as written"
  achievements:
    - "One selected achievement, as written"
  competencies:
    - area: "The group's name, as the resume gives it"
      items: ["one term", "another term"]
  roles:
    - employer: "The employer"
      title: "The title held"
      location: "The place given with this job"
      start: "As written, such as Jan 2019"
      end: "As written, such as Present"
      intro: "A sentence under the title that sets the scope of the job, or empty"
      descriptor: "A few words on what the employer is, only if the resume gives them, or empty"
      bullets:
        - "One bullet, as written"
  education:
    - degree: "The degree as written, with its field and year when they are on that line"
      school: "The school"
  philosophy: "The statement of leadership philosophy, or empty"
  awards:
    - "One award, as written"
  projects:
    - "One project, as written"
  contact:   # for the owner's application info, not the master resume
    name: ""
    location: "The city and state as the header gives it"
    address: "A street address only, if the resume gives one"
    phone: ""
    email: ""
    links: ["a link or profile address, as written"]
# A part the resume does not have is "" or [] (an entry with empty values). Nothing is added to a field.
