// What a step says about itself, read-only: a decision's ways out, who and with what. The step's engineering
// specification (the untitled documentation: Actor, Trigger, Rules ... with function and table names) is the file's
// own record for developers and is not shown unless the host asks (`showSpec`); what the household reads about a step
// is its tagged instructions and decision criteria, which the Steps list draws. A heading with nothing under it is
// left out.
import type { BpmnNode, BpmnProcess, Refs, SpecFields } from './bpmn/model'
import { isGateway } from './bpmn/model'
import { RichText } from './richText'

/** The specification's headings in the order a step shows them. */
export const SPEC_SECTIONS: { label: string; key: keyof SpecFields }[] = [
  { label: 'Actor', key: 'actor' },
  { label: 'Trigger', key: 'trigger' },
  { label: 'Fired by', key: 'firedBy' },
  { label: 'Enters at', key: 'entersAt' },
  { label: 'Preconditions', key: 'preconditions' },
  { label: 'Inputs', key: 'inputs' },
  { label: 'Rules', key: 'rules' },
  { label: 'Outputs', key: 'outputs' },
  { label: 'Exceptions', key: 'exceptions' },
  { label: 'What the household sees', key: 'householdSees' },
  { label: 'Source', key: 'source' },
]

export function refRows(refs: Refs): { label: string; text: string }[] {
  const rows: { label: string; text: string }[] = []
  if (refs.owner) rows.push({ label: 'Owner', text: refs.owner })
  if (refs.roles.length) rows.push({ label: 'Roles', text: refs.roles.join(', ') })
  if (refs.people.length) rows.push({ label: 'People', text: refs.people.join(', ') })
  if (refs.software.length) rows.push({ label: 'Software', text: refs.software.join(', ') })
  if (refs.documents.length) rows.push({ label: 'Documents', text: refs.documents.join(', ') })
  if (refs.controls.length) {
    rows.push({
      label: 'Controls',
      text: refs.controls.map((c) => (c.framework ? `${c.reference} (${c.framework})` : c.reference)).join(', '),
    })
  }
  return rows
}

export default function StepDetail({
  node, process, showWays = true, showSpec = false,
}: {
  node: BpmnNode
  process: BpmnProcess
  /** false when the host shows a decision's ways out itself. */
  showWays?: boolean
  /** true to also show the engineering specification under its headings. Off by default: it is not for the household. */
  showSpec?: boolean
}) {
  const shown = showSpec ? SPEC_SECTIONS.map((s) => ({ ...s, text: node.spec[s.key] })).filter((s) => s.text.trim()) : []
  const ways = showWays && isGateway(node) ? process.flows.filter((f) => f.source === node.id) : []
  const refs = refRows(node.refs)
  const repeats = node.multiInstance
  if (shown.length === 0 && ways.length === 0 && refs.length === 0 && !repeats) return null
  return (
    <dl className="pv-spec">
      {repeats && (
        <div className="pv-spec-row">
          <dt>Repeats</dt>
          <dd>
            <p>
              Once for each {repeats.elementVariable ? <code>{repeats.elementVariable}</code> : 'item'}
              {repeats.collection && (
                <>
                  {' '}in <code>{repeats.collection}</code>
                </>
              )}
              {repeats.sequential ? ', one after another.' : ', side by side.'}
            </p>
          </dd>
        </div>
      )}
      {shown.map(({ label, text }) => (
        <div key={label} className="pv-spec-row">
          <dt>{label}</dt>
          <dd>
            <RichText text={text} />
          </dd>
        </div>
      ))}
      {ways.length > 0 && (
        <div className="pv-spec-row">
          <dt>Ways out</dt>
          <dd>
            <ul className="pv-ways">
              {ways.map((f) => (
                <li key={f.id}>
                  <strong>{f.name || 'Unnamed way out'}</strong>
                  {f.id === node.defaultFlow && <span className="pv-way-default"> (when nothing else applies)</span>}
                  {f.meaning && <span>: {f.meaning}</span>}
                  {f.condition && (
                    <>
                      {' '}
                      <code>{f.condition}</code>
                    </>
                  )}
                </li>
              ))}
            </ul>
          </dd>
        </div>
      )}
      {refs.map(({ label, text }) => (
        <div key={label} className="pv-spec-row">
          <dt>{label}</dt>
          <dd>
            <p>{text}</p>
          </dd>
        </div>
      ))}
    </dl>
  )
}
