// One process the "How they work" page draws (the search for roles), converted to the process step and edge shapes
// (./types.ts) so ProcessView draws it, as it draws every process. Pure: no React, no
// fetching.
//
// The backend reads each process from its BPMN file (written from the process specification) with the process
// engine's own importer, so a step arrives as the importer reads it:
//
//   start events (what fires the process: a message or a timer) -> the tasks, each in the lane of its actor ->
//   a question as an exclusive gateway whose answers are labelled lines -> the ends
//
// The actor (the lane) is the step's `performer`, the same "Who" every process shows. A step that loops to
// itself is not drawn as a line; a loop back to an earlier step is, and ProcessView shows it as a rework path.
// A call activity is a `subprocess` step, drawn as the view's sub-process and marked as a call to another process.
// A process with no start event of its own gets one start event into its first step; one that has its own
// (every start event the file draws, with its short name) gets no extra "Start" in front of them.
import type { HarnessProcess, HarnessStep } from "../../api/types/agents";
import { START_ID, type ProcessEdge, type ProcessStep, type StepType } from 'process-view'

export { START_ID }

const STEP_TYPE: Record<HarnessStep['type'], StepType> = {
  start: 'event_start',
  task: 'task',
  decision: 'gateway_exclusive',
  subprocess: 'subprocess',
  end: 'event_end',
}

export function harnessToProcess(process: HarnessProcess): { steps: ProcessStep[]; edges: ProcessEdge[] } {
  const versionId = `harness-${process.id}`
  const steps: ProcessStep[] = []
  const edges: ProcessEdge[] = []
  const known = new Set(process.steps.map((s) => s.id))
  const hasStart = process.steps.some((s) => s.type === 'start')

  if (!hasStart) {
    steps.push({
      id: START_ID,
      version_id: versionId,
      step_type: 'event_start',
      label: 'Start',
      description: '',
      position_x: 0,
      position_y: 0,
      interrupting: true,
    })
  }
  process.steps.forEach((s, i) => {
    const messageStart = s.type === 'start' && s.trigger_kind === 'message'
    steps.push({
      id: s.id,
      version_id: versionId,
      step_type: STEP_TYPE[s.type] ?? 'task',
      label: s.title.replace(/`/g, ''),
      description: '',
      position_x: 0,
      position_y: (i + 1) * 100,
      // Who does it: the lane, in the specification's words, as the same "Who" chip every process shows.
      performer: s.who || null,
      // A call activity is drawn as the view's sub-process (double ring) and says it calls another process.
      ...(s.type === 'subprocess' ? { bpmn_type: 'bpmn:callActivity' } : {}),
      decision_criteria: s.criteria ? s.criteria.replace(/`/g, '') : s.criteria,
      // A schedule is drawn as one ("On a schedule: every minute"). A message start says what fires it in the
      // step's own Trigger and Fired by (the whole sentence, in the specification's words), so the generic
      // "A message arrives" line and its repeat of the name are left out.
      event_trigger: messageStart ? null : s.trigger_kind,
      event_trigger_detail: messageStart ? null : s.trigger_detail,
      event_trigger_subtype: messageStart ? null : s.trigger_subtype,
      interrupting: true,
    })
  })
  if (!hasStart && process.steps.length > 0) {
    edges.push({
      id: `${START_ID}->${process.steps[0].id}`,
      version_id: versionId,
      source_step_id: START_ID,
      target_step_id: process.steps[0].id,
      label: null,
      is_default: false,
    })
  }
  for (const s of process.steps) {
    for (const n of s.next) {
      if (n.to === s.id || !known.has(n.to)) continue
      edges.push({
        id: `${s.id}->${n.to}`,
        version_id: versionId,
        source_step_id: s.id,
        target_step_id: n.to,
        label: n.label || null,
        is_default: false,
      })
    }
  }
  return { steps, edges }
}
