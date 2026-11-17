// process-view: a BPMN process shown and edited as text.
//
//   <BpmnProcessView bpmn={xml} editable onChange={save} onValidate={check} />
//
// BPMN in, BPMN out (parseBpmn / serializeBpmn), the Process flow graph and the Steps list drawn from it, and an
// edit mode in the list. The package imports nothing from the app that uses it.
import './editor.css'

export { default as BpmnProcessView } from './BpmnProcessView'
export type { BpmnProcessViewProps, Validation } from './BpmnProcessView'
export { default as ProcessView } from './ProcessView'
export type { ProcessViewProps } from './ProcessView'
export { comparePathNumbers, embedCondition, FLOW_MIN_ZOOM, isDefaultPath } from './ProcessView'
export { default as StepDetail, SPEC_SECTIONS } from './StepDetail'
export { default as StepEditor } from './StepEditor'
export { Inline, RichText, blocksOf } from './richText'
export { seedForWideBoard } from './livingGraphKit'
export { analyzeFlow, buildSections, numberSteps } from './processFlow'

export * from './bpmn/model'
export { parseBpmn } from './bpmn/parse'
export { serializeBpmn } from './bpmn/serialize'
export { processToGraph, START_ID } from './bpmn/graph'
export {
  addStepAfter, addWayOut, moveStep, removeStep, removeWayOut, updateFlow, updateNode, whyNot,
} from './bpmn/edit'
export { applySpec, formatRuleItems, parseRuleItems, readSpec, renumber } from './bpmn/spec'

export type { ProcessEdge, ProcessStep, ProcessVersionGraph, StepType } from './types'
