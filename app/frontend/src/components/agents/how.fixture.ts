// Typed fixtures for the How they work tests: the search process with its sub-process, one step the scout is
// sent, a real example, and a run. Only test files import this.
import type { HarnessExample, HarnessOverview, HarnessSent, HarnessStep, ProcessRunDetail } from "../../api/types/agents";

const SCOUT_SENT: HarnessSent = {
  role_files: [
    {
      kind: "instructions", name: "agents.scout.role", title: "Role", text: "# You are the scout\nYou search the boards.",
      chars: 40, placeholders: [], version: 1, edited: false, busy: null,
    },
  ],
  instruction: {
    kind: "instructions", name: "jobsearch.scout.instructions", title: "Instructions for this step",
    text: "# Search for roles\nReport with {{report_call}}.",
    chars: 49, placeholders: ["report_call"], version: 1, edited: false, busy: null,
  },
  template: {
    kind: "templates", name: "jobsearch.scout.report", title: "How it answers", text: "# Report template", chars: 17,
    placeholders: [], version: 1, edited: false, busy: null,
  },
  fields: [
    { key: "focus", means: "What this search looks at, in words.", group: "this step" },
    { key: "known", means: "How many roles from your tracker are already known.", group: "this step" },
  ],
  budget_chars: 200000,
  has_example: true,
};

function step(overrides: Partial<HarnessStep> & Pick<HarnessStep, "id" | "title">): HarnessStep {
  return {
    harness_id: null, type: "task", who: "Automatic", who_about: "Done by rule, with no agent asked.",
    role: null, actor: "", trigger: "", fired_by: "", enters_at: "", preconditions: "", inputs: "", rules: "",
    outputs: "", exceptions: "", owner_sees: "", source: "", criteria: null, trigger_kind: null, trigger_detail: null,
    trigger_subtype: null, calls: null, next: [], sent: null, ...overrides,
  };
}

export const OVERVIEW: HarnessOverview = {
  budget_chars: 200000,
  roles: [
    { role: "automatic", who: "Automatic", about: "Done by rule." },
    { role: "scout", who: "Scout", about: "Searches the boards." },
  ],
  processes: [
    {
      id: "jobsearch_round",
      slug: "jobsearch_round",
      top_level: true,
      children: ["jobsearch_screen_role"],
      name: "Search for roles",
      about: "One search for new roles.",
      data_model: [
        { path: "focus", type: "string", enum: [], description: "What this search looks at, in words." },
        { path: "stored.kept", type: "number", enum: [], description: "Roles kept for the page." },
        { path: "result", type: "object", enum: [], description: "Hidden." },
      ],
      steps: [
        step({
          id: "Start_round", title: "A search starts", type: "start", who: "Automatic", actor: "Job search",
          trigger: "You press Search now, or a day has passed since the last search.", trigger_kind: "message",
          trigger_detail: "A search starts", enters_at: "Open_run", next: [{ to: "Open_run", label: "" }],
        }),
        step({ id: "Open_run", title: "Read the gates", next: [{ to: "Scout_searches", label: "" }] }),
        step({
          id: "Scout_searches", harness_id: "jobsearch.scout", title: "The scout searches the boards", who: "Scout", role: "scout",
          who_about: "The scout looks.", sent: SCOUT_SENT, next: [{ to: "Screen_each", label: "" }],
          actor: "Scout, with jobs.report", trigger: "The gates are read.", preconditions: "A search is open.",
          inputs: "The match profile and the task.",
          rules: "Principle. It never applies.\nThe scout:\n1. Reads the profile (`jobs.profile`).\n2. Searches the boards.\n3. Reports each role with a line of evidence.",
          outputs: "The roles it found", exceptions: "A report with no roles ends the search.",
          owner_sees: "The roles to look at on the Pipeline.", source: "`jobsearch/missions/search_run.py`",
        }),
        step({
          id: "Screen_each", title: "Screen each role", type: "subprocess", who: "Automatic", calls: "jobsearch_screen_role",
          next: [{ to: "Gw_any", label: "" }],
        }),
        step({
          id: "Gw_any", title: "Is any role worth a look?", type: "decision", criteria: "Keep when it passes every gate.",
          next: [{ to: "Show", label: "Yes" }, { to: "E-done", label: "No" }],
          ways: [
            { flow: "f1", to: "Show", to_title: "Show the roles", name: "Yes", condition: "stored.kept > 0", meaning: "At least one role was kept.", default: false, firm_meaning: null },
            { flow: "f2", to: "E-done", to_title: "Search done", name: "No", condition: null, meaning: "", default: true, firm_meaning: "Nothing is ever applied to." },
          ],
        }),
        step({ id: "Show", title: "Show you the roles", who: "You", next: [{ to: "E-done", label: "" }] }),
        step({ id: "E-done", title: "Search done", type: "end", next: [] }),
      ],
    },
    {
      id: "jobsearch_screen_role",
      slug: "jobsearch_screen_role",
      top_level: false,
      children: [],
      name: "Screen one role",
      about: "One role against your hard gates.",
      steps: [
        step({
          id: "Start_role", title: "A role arrives", type: "start", who: "Automatic", trigger: "The round hands over a role.",
          trigger_kind: "message", trigger_detail: "A role arrives", next: [{ to: "Gate", label: "" }],
        }),
        step({
          id: "Gate", title: "Check the gates", who: "A tool", actor: "The gate check",
          rules: "1. Pay is at or over your floor.", next: [{ to: "End_role", label: "" }],
        }),
        step({ id: "End_role", title: "The role is stored", type: "end", next: [] }),
      ],
    },
  ],
};

export const EXAMPLE: HarnessExample = {
  task_id: "r1",
  title: "Search for roles",
  payload: { focus: "remote design roles", known: 41 },
  chars: 6000,
  budget_chars: 200000,
  parts: [{ key: "focus", chars: 40 }, { key: "known", chars: 20 }],
  source: "sent",
  label: "as sent to the scout",
  agent: "The scout",
  sent_at: "2026-10-04T15:42:00Z",
  changed_files: [{ kind: "instruction", name: "jobsearch.scout.instructions", now: "changed" }],
};

export const RUN: ProcessRunDetail = {
  id: "i1", process: "jobsearch_round", name: "Search for roles", slug: "jobsearch_round", alongside: false, status: "waiting",
  status_words: "Waiting at a step", step_title: "The scout searches the boards", started_at: "2026-10-04T15:00:00Z", ended_at: null,
  current: "Scout_searches", ended: false,
  passed: [{ step: "Start_round", at: "2026-10-04T15:00:00Z" }, { step: "Open_run", at: "2026-10-04T15:01:00Z" }],
  waiting_for: [{ step: "Scout_searches", words: "the scout's report" }],
  sent: { Scout_searches: [{ task_id: "r1", agent: "The scout", title: "Search for roles", sent_at: "2026-10-04T15:42:00Z" }] },
  parts: [],
  others: [{
    id: "i0", process: "jobsearch_round", name: "Search for roles", slug: "jobsearch_round", alongside: false, status: "completed",
    status_words: "Finished", step_title: "Search done", started_at: "2026-10-01T09:00:00Z", ended_at: "2026-10-01T09:30:00Z",
  }],
};
