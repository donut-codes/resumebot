import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { GapDashboard, Resume } from "../../api/types/me";
import { useRefreshKey } from "../../lib/appContext";
import { byNewest } from "../../lib/dates";
import { useAction } from "../../lib/useAction";
import { useResource } from "../../lib/useResource";
import { MeAlert, MeLoading } from "./MeSection";
import { blankRole, newRoleId } from "../profile/targetRoles";
import { useToast } from "../toast/ToastProvider";
import GapRoles from "./GapRoles";
import GapRun from "./GapRun";
import type { ClarifyActions } from "./GapClarify";
import { ADDED_WHY } from "./gaps";

/** What the owner does with the Coach's questions and proposals, as calls of the API (docs/PRD.md 38.13 to 38.16). */
const CLARIFY_ACTIONS: ClarifyActions = {
  askQuestions: (runId) => api.askGapQuestions(runId),
  suggestLines: (runId) => api.suggestGapLines(runId),
  answerCard: (cardId, action, text) => api.answerGapCard(cardId, action, text),
  acceptProposal: (id, text) => api.acceptGapProposal(id, text),
  rejectProposal: (id) => api.rejectGapProposal(id),
};

/** What is shown under the dashboard: a target role's run (the one going, else its latest), or the run for a title that was
 * typed (by its id). Nothing chosen: the run going, else the latest check. */
type Shown = { role: string } | { run: string } | null;

/** The run the page shows, from the dashboard and what was chosen. */
function runToShow(dash: GapDashboard | null, shown: Shown): { runId: string | null; roleId: string | null } {
  if (!dash) return { runId: null, roleId: null };
  if (shown && "run" in shown) return { runId: shown.run, roleId: null };
  const { active, failed, roles } = dash;
  let roleId: string | null;
  if (shown) roleId = shown.role;
  else if (active) {
    if (!active.role_id) return { runId: active.id, roleId: null };
    roleId = active.role_id;
  } else {
    const latest = roles.filter((r) => r.last_run?.at).sort((a, b) => byNewest(a.last_run?.at, b.last_run?.at))[0];
    roleId = latest?.id ?? null;
  }
  if (roleId === null) return { runId: null, roleId };
  if (active && active.role_id === roleId) return { runId: active.id, roleId };
  const role = roles.find((r) => r.id === roleId);
  if (role?.last_run) return { runId: role.last_run.id, roleId };
  return { runId: failed && failed.role_id === roleId ? failed.id : null, roleId };
}

interface Props {
  /** The master resume as loaded, and the version of it: what Add to resume saves from, naming the version it was made from. */
  resume: Resume;
  resumeVersion: number;
  /** The master resume was saved (the page's copy, and the version now in force). */
  onSavedResume: (resume: Resume, version: number) => void;
  /** Whether an agent can take work now, and why not in words: the same rule, and sentence, as Import your resume. */
  ready: boolean;
  why: string | null;
  whyId: string;
}

/** Find Gaps (docs/PRD.md path 38), the second page of My Resume: what the postings for a target role ask that the master
 * resume does not yet show. The dashboard is a row for each target role and a field for a title that is not one; below it, the
 * run for the role opened, which keeps its latest. A run that works shows how many postings it has read, and sends no
 * notification. Nothing is written to the resume unless the owner clicks Add to resume. */
export default function FindGaps({ resume, resumeVersion, onSavedResume, ready, why, whyId }: Props) {
  const refreshKey = useRefreshKey();
  const { showToast } = useToast();
  const [shown, setShown] = useState<Shown>(null);
  const [going, setGoing] = useState(false);
  const [added, setAdded] = useState<string | null>(null);

  const dash = useResource(() => api.findGaps(), [refreshKey], {
    fallback: "Couldn't load Find Gaps.",
    liveArea: "gaps",
    onData: (d) => setGoing(d.active !== null),
  });
  // The picker's list is read the first time the picker opens, not with the dashboard.
  const [pickerOpened, setPickerOpened] = useState(false);
  const jobs = useResource(() => api.gapJobs(), [pickerOpened, refreshKey], {
    enabled: pickerOpened,
    fallback: "Couldn't load your roles.",
  });
  const { runId, roleId } = runToShow(dash.data, shown);
  const run = useResource(() => api.gapRun(runId ?? ""), [runId, refreshKey], {
    enabled: runId !== null,
    reset: true,
    liveArea: "gaps",
    onError: () => dash.reload(), // the run was replaced by a newer one, or is gone
  });

  // The resume was saved (Add to resume here, or on the master resume page): what was graded against it is now older.
  const lastVersion = useRef(resumeVersion);
  const { reload: reloadDash } = dash;
  const { reload: reloadRun } = run;
  useEffect(() => {
    if (lastVersion.current === resumeVersion) return;
    lastVersion.current = resumeVersion;
    reloadDash();
    if (runId !== null) reloadRun();
  }, [resumeVersion, runId, reloadDash, reloadRun]);

  const start = useAction(
    async (target: { role_id: string } | { title: string } | { opportunity_id: string }) => {
      const { id } = await api.startGaps(target);
      setShown("role_id" in target ? { role: target.role_id } : { run: id });
      dash.reload();
    },
    { fallback: "Couldn't start it." },
  );

  const repeat = useAction(
    async (how: "again" | "sample") => {
      if (!runId) return;
      if (how === "again") await api.checkGapsAgain(runId);
      else await api.newGapSample(runId);
      setShown(roleId ? { role: roleId } : shown);
      dash.reload();
    },
    { fallback: "Couldn't start it." },
  );

  const addRole = useAction(
    async (name: string, forRun: string) => {
      const profile = await api.profile();
      const roles = profile.criteria.target_roles ?? [];
      const next = { ...profile.criteria, target_roles: [...roles, blankRole(newRoleId(name, roles), name)] };
      await api.saveProfile("criteria", next, ADDED_WHY, profile.versions.criteria ?? 0);
      setAdded(forRun);
      showToast("Added as a target role");
      dash.reload();
    },
    { fallback: "Couldn't add it as a target role." },
  );

  if (!dash.data) return dash.error ? <MeAlert>{dash.error}</MeAlert> : <MeLoading>Loading Find Gaps&hellip;</MeLoading>;

  const view = run.data;
  const changed = () => {
    dash.reload();
    run.reload();
  };
  return (
    <div className="fi-me-page">
      <GapRoles
        roles={dash.data.roles}
        shown={roleId}
        onOpen={(id) => setShown({ role: id })}
        onStart={(target) => void start.run(target)}
        jobs={jobs.data}
        onOpenPicker={() => setPickerOpened(true)}
        jobRuns={dash.data.job_runs}
        shownRun={shown && "run" in shown ? shown.run : runId}
        onOpenRun={(id) => setShown({ run: id })}
        ready={ready}
        why={why}
        whyId={whyId}
        going={going}
        busy={start.busy || repeat.busy}
        error={start.error}
      />
      {jobs.error && <MeAlert>{jobs.error}</MeAlert>}
      {dash.data.failed && !shown && !runId && <MeAlert>{dash.data.failed.error || "The last run stopped."}</MeAlert>}
      {runId !== null && !view && !run.error && <MeLoading>Loading the run&hellip;</MeLoading>}
      {runId !== null && !view && run.error && <MeAlert>{run.error}</MeAlert>}
      {view && (
        <GapRun
          key={view.id}
          run={view}
          clarifyActions={CLARIFY_ACTIONS}
          resume={resume}
          resumeVersion={resumeVersion}
          ready={ready}
          why={why}
          whyId={whyId}
          going={going}
          onCheckAgain={() => void repeat.run("again")}
          onNewSample={() => void repeat.run("sample")}
          onAddRole={() => void addRole.run(view.title, view.id)}
          addedRole={added === view.id}
          busy={repeat.busy || addRole.busy}
          error={repeat.error ?? addRole.error}
          onChanged={changed}
          onSavedResume={onSavedResume}
        />
      )}
    </div>
  );
}
