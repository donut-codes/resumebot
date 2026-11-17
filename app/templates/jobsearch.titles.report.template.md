<!-- Used by: the job-search app's title pass, as the shape of one `jobs.titles_report` call.
     Loaded: by the app's hand-out of the title pass's step (backend/app/jobsearch/tools.py); named by playbooks/jobsearch/search_round.bpmn.
     Placeholders: none. Context keys: none. -->
answers:
  - id: "The id, exactly as jobs.titles_next gave it"
    answer: yes   # yes | no
    why: "One short line, in plain words: why it could be, or what other kind of work it is"
    role: "The id of the one target role it matches (for a yes)"
  - id: "another-id"
    answer: no
    why: "A sales job"
    role: "The id of the target role it comes closest to (for a no), or none"
  - id: "the id of an item of kind role"   # a role the owner already has: which target role it matches
    role: "The id of that target role, or none"
  - id: "the id of an item of kind facts"   # a role whose header lines you read for the gates' facts
    kind: facts
    facts:
      work_mode: { value: "remote | hybrid | onsite", quote: "the words of lines it rests on" }   # only when lines state it
      location: { value: "where the work is", quote: "the words of lines it rests on" }
      pay: { value: "the pay as written", quote: "the words of lines it rests on" }
      leads_bd: { value: "yes | no | unknown", quote: "the words of bd_lines it rests on (the title or a sentence); a yes needs one" }   # only when asks holds leads_bd
      main_work: { value: "one of mw_words exactly as listed, or none", quote: "the words of mw_lines it rests on; a word needs one" }   # only when asks holds main_work
    rules:
      - { id: "a rule's id", answer: "fail | unknown", quote: "the words of lines that state what the rule forbids" }
