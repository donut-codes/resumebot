<!-- Used by: the job-search app's facts gate seat (a packet's facts read), as the shape of its one reply.
     Loaded: by the seat hand-out (backend/app/jobsearch/seats.py) at the facts steps of playbooks/jobsearch/build_packet.bpmn.
     Placeholders: none. Context keys: none. -->
claims:                                   # every factual claim in the cover letter, listed before you judge
  - {sentence: "first words", claim: "...", line: "master line id or NONE", words: "the master words that hold it"}
failures:
  - {doc: "resume", check: "trace", line: "first words of the line", reason: "One sentence."}   # doc: resume | letter; check: trace | phrasings | code
dismissed: ["the code flag's text, for each flag that is not a real fault"]
