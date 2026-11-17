<!-- Used by: the job-search app's arbitrator seat, as the shape of its one reply.
     Loaded: by the seat hand-out (backend/app/jobsearch/seats.py) at the arbitrator step of playbooks/jobsearch/build_packet.bpmn.
     Placeholders: none. Context keys: none. -->
rulings:
  - {id: "F1", real: true, why: "One sentence.", status: "open", fix: "the line's new wording, or what to drop"}   # status: open | fixed_already | cleared
fix_list:
  - {doc: "resume", line: "first words of the line now", change: "the exact new wording, or the cut"}   # doc: resume | letter
