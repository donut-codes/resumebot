// Test help for How they work's editing (docs/PRD.md path 16): the editor draws a process from its BPMN file, which the
// page fetches. A test describes a process as the page's own steps (HarnessProcess); this writes the BPMN file for it
// and answers the calls the process's tab makes besides the overview: its file and its versions. Only test files import
// this.
import type { FileVersion, HarnessProcess, HarnessStep, ProcessHistory } from "../../api/types/agents";
import { json } from "../../test-utils";
import { OVERVIEW } from "./how.fixture";

const esc = (text: string) => text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const attr = (text: string) => esc(text).replace(/"/g, "&quot;");

const ELEMENT: Record<HarnessStep["type"], string> = {
  start: "startEvent", task: "task", decision: "exclusiveGateway", subprocess: "callActivity", end: "endEvent",
};

/** A step's specification as the documentation text a BPMN file carries it in. */
function documentation(s: HarnessStep): string {
  const rules: string[] = [];
  if (s.fired_by) rules.push(`Fired by: ${s.fired_by}`);
  if (s.enters_at) rules.push(`Enters at: ${s.enters_at}`);
  if (s.preconditions) rules.push(`Preconditions. ${s.preconditions}`);
  if (s.inputs) rules.push(`Inputs. ${s.inputs}`);
  if (s.rules) rules.push(...(rules.length ? ["Processing rules."] : []), s.rules);
  if (s.exceptions) rules.push(`Exceptions. ${s.exceptions}`);
  const parts: string[] = [];
  if (s.actor) parts.push(`Actor: ${s.actor}`);
  if (s.trigger) parts.push(`Trigger: ${s.trigger}`);
  if (rules.length) parts.push(`Rules:\n${rules.join("\n")}`);
  if (s.outputs) parts.push(`Outputs: ${s.outputs}`);
  if (s.owner_sees) parts.push(`What the owner sees: ${s.owner_sees}`);
  if (s.source) parts.push(`Source: ${s.source}`);
  return parts.join("\n");
}

export function bpmnOf(process: HarnessProcess): string {
  const lanes = [...new Set(process.steps.map((s) => s.who).filter(Boolean))];
  const known = new Set(process.steps.map((s) => s.id));
  const out: string[] = [];
  out.push(
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:porter="http://porter.local/bpmn/ext" id="Definitions_1" targetNamespace="http://porter.local/bpmn">',
    `  <bpmn:process id="Process_${process.id}" name="${attr(process.name)}" porter:key="${process.id}">`,
  );
  if (lanes.length) {
    out.push(`    <bpmn:laneSet id="LaneSet_1">`);
    lanes.forEach((who, i) => {
      out.push(`      <bpmn:lane id="Lane_${i}" name="${attr(who)}">`);
      for (const s of process.steps.filter((x) => x.who === who)) out.push(`        <bpmn:flowNodeRef>${attr(s.id)}</bpmn:flowNodeRef>`);
      out.push("      </bpmn:lane>");
    });
    out.push("    </bpmn:laneSet>");
  }
  for (const s of process.steps) {
    const extra = s.type === "subprocess" && s.calls ? ` calledElement="${attr(s.calls)}"` : "";
    out.push(`    <bpmn:${ELEMENT[s.type]} id="${attr(s.id)}" name="${attr(s.title)}"${extra}>`);
    const doc = documentation(s);
    if (doc) out.push(`      <bpmn:documentation>${esc(doc)}</bpmn:documentation>`);
    if (s.criteria) out.push(`      <bpmn:documentation id="${attr(s.id)}_criteria">${esc(s.criteria)}</bpmn:documentation>`);
    if (s.type === "start" && s.trigger_kind === "message") out.push("      <bpmn:messageEventDefinition/>");
    out.push(`    </bpmn:${ELEMENT[s.type]}>`);
  }
  let n = 0;
  for (const s of process.steps) {
    for (const next of s.next) {
      if (!known.has(next.to)) continue;
      const name = next.label ? ` name="${attr(next.label)}"` : "";
      out.push(`    <bpmn:sequenceFlow id="Flow_${++n}"${name} sourceRef="${attr(s.id)}" targetRef="${attr(next.to)}"/>`);
    }
  }
  out.push("  </bpmn:process>", "</bpmn:definitions>");
  return out.join("\n");
}

/** A saved version as the history lists it. */
export function version(number: number, overrides: Partial<FileVersion> = {}): FileVersion {
  return {
    number, source: number === 1 ? "shipped" : "edit", at: "2026-10-12T09:00:00Z", by: number === 1 ? null : "Tester",
    why: number === 1 ? "The file as it first shipped." : `Why ${number}`, current: false, restored: null,
    can_restore: true, summary: [], loads: true, problem: null, ...overrides,
  };
}

/** The history of one process: version 1 as shipped, in use, unless the test gives its own. */
export function history(key: string, versions: FileVersion[] = [version(1, { current: true, can_restore: false })]): ProcessHistory {
  const inUse = versions.find((v) => v.current)?.number ?? 1;
  return { process: key, running: { number: inUse, note: null }, versions };
}

/** The calls a process's tab makes besides the overview: its file (`number` is the version in use) and its versions.
 * `undefined` when the URL is neither. */
export function howProcessRoutes(
  url: string, opts: { number?: number; history?: ProcessHistory; processes?: HarnessProcess[] } = {},
): Promise<Response> | undefined {
  const found = /\/harness\/processes\/([^/?]+)\/(bpmn|versions)(?:\/(\d+))?/.exec(url);
  if (!found) return undefined;
  const process = (opts.processes ?? OVERVIEW.processes).find((p) => p.id === found[1]);
  if (!process) return json({ detail: "there is no such process" }, 404);
  if (found[2] === "bpmn") return json({ process: found[1], number: opts.number ?? 1, xml: bpmnOf(process), note: null });
  if (found[3]) return json({ process: found[1], number: Number(found[3]), xml: bpmnOf(process) });
  return json(opts.history ?? history(found[1]));
}
