---
kind: porter.explore
domain: porter
title: Look into something
risk: read_only
data:
  subject: {type: string, required: true, means: "What to look into, in a few words"}
  question: {type: string, required: true, means: "What the owner needs to know about it"}
do:
  runner: model
done_means: "What was found is recorded with evidence, or it couldn't be found and where it looked."
check:
  by: porter
  test: "Evidence is recorded."
  causes: [not_attempted]
result:
  answer:  {type: string, required: true, means: "What was found, in plain words"}
  sources: {type: list, required: true, means: "Where each fact came from; an empty list only when nothing was found"}
enabled: true
needs: null
---
Look into the subject with your own read-only tools and answer the question. Change nothing anywhere. Complete the task with `result` `{answer, sources}` (what you found, and where each fact came from), the answer as the summary and the sources as the evidence; if you couldn't find it, complete it off plan and say where you looked.
