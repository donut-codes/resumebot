# process-view

A BPMN process shown and edited as text.

```tsx
import { BpmnProcessView } from 'process-view'

<BpmnProcessView
  bpmn={xml}                 // the BPMN file, as text: the file the view starts from
  editable                   // shows the Edit toggle
  onChange={(xml) => ...}    // the whole new file after every edit; the component never saves
  onValidate={async (xml) => ({ ok, problems })}   // the host's engine says whether it can run it
  marks={{ Step_a: 'passed', Step_b: 'current' }}  // optional: where a running instance is
/>
```

- **Display**: the Process flow graph (a force-directed node-link graph) and the numbered Steps list, drawn from the file.
- **Edit mode**: each step's fields are text (name, lane, actor, trigger, preconditions, inputs, numbered rules, outputs,
  exceptions, what the household sees, source; instructions, decision criteria, commands, prompts; owner, roles, people,
  software, documents, controls; a decision's ways out with label, plain meaning and the rule for taking it). List
  operations: add a step after one, remove a step (joining its one way in to its one way out), move a step up or down on
  its path, add or remove a way out. The graph redraws live and cannot be edited directly.
- **BPMN in, BPMN out** (`parseBpmn`, `serializeBpmn`): a file that was only read is written back byte for byte; an edit
  changes only the lines it edits; ids, unknown elements and attributes, and the diagram are kept.
- **No host styles needed**: the package's buttons, muted text and hidden text are its own (`pv-btn`, `pv-btn-quiet`,
  `pv-btn-sm`, `pv-muted`, `pv-visually-hidden`). Colours and sizes come from CSS variables with fallbacks (`--ink`,
  `--line`, `--surface`, `--space-*`, ...); a host that defines them (a host app can) gets its own look.

## Who holds the draft

**The view does.** `bpmn` is the file the view starts from, not the draft.

- While there are edits the view keeps the edited text itself and reports each new text through `onChange` (the whole
  file, never a patch). The host may keep it for a Save button, feed it straight back into `bpmn`, or ignore it.
- **Feeding `onChange` back into `bpmn` is fine.** The text the view last reported is recognised and is not a new file.
- **Discard** goes back to the file the edits started from and reports it through `onChange`, so the host's copy is
  the file again too. It is offered whenever there are edits.
- **A different `bpmn` while there are edits** is not taken. The edits stay, and the view says "This process changed
  since you started editing. Discard your edits to see the new version." Discard then shows the host's new file. With no
  edits, a different `bpmn` simply replaces what is shown.
- **`editable` turning false** ends editing, and edits not sent are dropped (reported through `onChange`, like Discard).
- **`processId`** picks a process of the file by its id (the first when not given). An id the file does not have is
  said in plain words; the view never shows another process in its place.

## Props

`BpmnProcessView`:

| Prop | What it is |
| --- | --- |
| `bpmn` | The BPMN file, as text. The file the view starts from (see above). |
| `processId` | Which process of the file to show. The first when not given. |
| `editable` | Shows the Edit toggle. Without it the view only reads. |
| `onChange(xml)` | The whole new file after every edit, and the starting file again on Discard. |
| `onValidate(xml)` | Asks the host whether the edited file is one it can run; returns `{ ok, problems }` or a promise of it. Called when editing starts (for the file as loaded) and a moment after typing stops. Only the answer to the latest call is shown. A call that throws or rejects is shown as a check that could not be run; an answer with no `problems` as a plain refusal. A host with side effects in `onValidate` should ignore answers that arrive after a newer call. |
| `marks` | A running instance's place: `{ [stepId]: 'current' \| 'passed' }`. The current step is drawn and listed as "Here now", the passed ones as "Passed". Wins over `currentStepId`. |
| `currentStepId` | The step a run is at, for a host that knows it by id instead of through `marks`. |
| `currentWord` | What the current step's mark says. "Here now" unless the host says otherwise (a run that ended: "Ended here"). |
| `live` | `true` (default): the graph runs its live simulation, so a step can be dragged. `false`: it settles once and stays still (tests, a run's view). |
| `performerOnRows` | `true`: each step's actor is on its row, under the title, instead of a "Who" chip inside the opened step. |
| `renderDetail(step, node, editing)` | More for the host to add under a step's own detail, in read and edit mode. `node` is undefined for a step the file has no element for. |
| `actions` | Shown beside the Edit toggle, e.g. the host's Save or Send button. |
| `showWays` | `false` when the host shows a decision's ways out in its own detail (read mode only; the editor always has them). Default `true`. |
| `showSpec` | `true` to also show each step's engineering specification (actor, trigger, rules, ...). Off by default: it is not for the household. |
| `onEditingChange(editing)` | Said when the editor starts or stops, including when it stops because `editable` turned false. Not called while React is working out a state change, so it fires once per change. |

`ProcessView` (`steps`, `edges`, plus `live`, `renderDetail`, `performerOnRows`, `marks`, `currentWord`, `modeControl`,
`focusStepId`, `startExpanded`) draws a process from steps and edges you build yourself; `BpmnProcessView` builds them
from the file. `focusStepId` is a request to select and open one step, used once; asking again is a new value.

Two views on one page do not touch each other: every lookup is inside the view's own element, the section ids have a
per-instance suffix, and the arrow keys and Escape work only inside the view that has focus. A click on a step selects
it; a click inside its opened detail does not.

Tests run with the host's vitest (`packages/process-view/src/__tests__`). See `PROVENANCE.md` at the repository root.
