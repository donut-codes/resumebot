import { useEffect, useState } from "react";
import { api } from "../../api/client";
import type { Mission, MissionDetail, MissionRun, MissionSchedule, PeakView } from "../../api/types/agents";
import { useRefreshKey, useRoute } from "../../lib/appContext";
import { dateTimeInWords, fromOwnerLocalValue, ownerZoneLabel } from "../../lib/dates";
import { routeFor, serializeRoute } from "../../lib/route";
import { useAction } from "../../lib/useAction";
import { useResource } from "../../lib/useResource";
import HowTheyWork from "../agents/HowTheyWork";
import { Block, type BlockInk } from "../focus/parts";
import EmptyState from "../primitives/EmptyState";
import ErrorBanner from "../primitives/ErrorBanner";
import Loading from "../primitives/Loading";
import StateWords from "../primitives/StateWords";
import NumbersForm, { NO_LIMIT, type NumberValue, numbersWords } from "./MissionNumbers";

const OUTCOME_WORDS: Record<MissionRun["outcome"], string> = {
  running: "Running", done: "Done", failed: "Failed", skipped: "Skipped",
};

/** How a run ended, told in the ink of its job: done is teal, failed is red, and a run still going or put off is sun. */
const OUTCOME_INK: Record<MissionRun["outcome"], BlockInk> = { running: "saffron", done: "green", failed: "oxblood", skipped: "saffron" };

function Outcome({ run }: { run: MissionRun }) {
  return <span className="fi-badge fi-state" data-ink={OUTCOME_INK[run.outcome]}>{OUTCOME_WORDS[run.outcome] ?? run.outcome}</span>;
}

/** Claude's peak hours (docs/PRD.md 25.8): one section for every mission that uses Claude, its settings drawn from the
 * server's registry like a mission's own. */
function PeakSection({ peak, onSave, busy }: { peak: PeakView; onSave: (values: Record<string, NumberValue>, why: string) => void; busy: boolean }) {
  const [editing, setEditing] = useState(false);
  const canEdit = !!peak.numbers && peak.numbers.length > 0;
  return (
    <Block
      id="missions-peak"
      title={<>Claude&rsquo;s peak hours</>}
      aside={!editing && canEdit ? <button type="button" className="fi-btn" onClick={() => setEditing(true)}>Edit</button> : undefined}
      hint={<>Applies to every mission that uses Claude.{peak.avoid ? ` The window, in your time: ${peak.window}.` : " Not avoided: Claude work runs at any hour."}</>}
    >
      {peak.now && peak.resumes_words && (
        <p className="mission-peak-now">Peak hours now: no Claude work until {peak.resumes_words}.</p>
      )}
      {editing && canEdit && (
        <NumbersForm name="Claude's peak hours" numbers={peak.numbers ?? []} busy={busy} saveLabel="Save peak hours"
          onSave={(values, why) => { onSave(values, why); setEditing(false); }} />
      )}
    </Block>
  );
}

function stateWords(m: Mission): string {
  if (m.state === "on") return "On";
  return m.paused_until ? `Paused until ${dateTimeInWords(m.paused_until)}` : "Paused";
}

/** The standing work (PRD path 18): each mission with what it is for, when it next runs, how its last run ended and
 * whether it is on. One opens beside the list to run, pause or change it, with its playbook, runs and history. */
export default function MissionsScreen() {
  const refreshKey = useRefreshKey();
  const { route, go } = useRoute();
  const selected = route.missionId;
  const [changes, setChanges] = useState(0);
  const { data, error } = useResource(() => api.missions(), [refreshKey, changes], {
    fallback: "Couldn't load the missions.",
    liveArea: "missions",
  });
  const peak = useAction(async (values: Record<string, NumberValue>, why: string) => {
    await api.saveMissionNumbers(values, why);
    setChanges((n) => n + 1);
  }, { fallback: "Couldn't change it." });

  if (error && !data) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <Loading>Loading the missions&hellip;</Loading>;

  return (
    <div className="missions-screen">
      <p className="fi-head-meta fi-ag-about">The work that runs by itself: when each comes up, when its night is done, and how it last went.</p>
      <PeakSection peak={data.peak} busy={peak.busy} onSave={(values, why) => peak.run(values, why)} />
      {peak.error && <ErrorBanner as="p">{peak.error}</ErrorBanner>}
      <Block id="missions-list" title="The missions" count={data.missions.length} flush>
        {data.missions.length === 0 ? (
          <EmptyState>No missions yet.</EmptyState>
        ) : (
          <ul className="missions" aria-label="Missions">
            <li className="mission-cols" aria-hidden="true">
              <span>Mission</span>
              <span>State</span>
              <span>Schedule</span>
              <span>Last run</span>
            </li>
            {data.missions.map((m) => {
              const open = m.id === selected;
              return (
                <li key={m.id} className={`mission${open ? " mission-open" : ""}`} data-agent={m.agent ?? undefined}
                  aria-current={open ? "true" : undefined}>
                  <div className="mission-row">
                    <div className="mission-who">
                      {/* The name opens the mission below its line, and closes it again. */}
                      <a className="mission-name"
                        href={serializeRoute(routeFor("missions", { missionId: open ? null : m.id }))}>{m.name}</a>
                      <p className="mission-purpose">{m.purpose}</p>
                      <dl className="mission-facts">
                        {m.done_when && <div><dt>Done when</dt><dd>{m.done_when}</dd></div>}
                        {m.numbers.length > 0 && <div><dt>Runs with</dt><dd>{numbersWords(m.numbers)}</dd></div>}
                      </dl>
                    </div>
                    <div>
                      <StateWords words={stateWords(m)} ink={m.state === "on" ? "green" : "saffron"} className="mission-state" />
                    </div>
                    <dl className="mission-facts">
                      <div><dt className="mission-term-quiet">When</dt><dd className="mission-schedule">{m.schedule_words}</dd></div>
                      {!m.runs_elsewhere && (
                        <div><dt>Next</dt><dd>{m.next_run_at ? dateTimeInWords(m.next_run_at) : "–"}</dd></div>
                      )}
                    </dl>
                    <dl className="mission-facts">
                      <div>
                        <dt className="mission-term-quiet">Last</dt>
                        <dd>
                          {m.last_run ? (
                            <>
                              <Outcome run={m.last_run} /> {dateTimeInWords(m.last_run.started_at)}
                              {m.last_run.progress && ` · ${m.last_run.progress.text ?? `${m.last_run.progress.done} of ${m.last_run.progress.target}`}`}
                              {m.last_run.stopped && ` · stopped: ${m.last_run.stopped}`}
                              {m.runs_elsewhere && ` · ${m.last_run.summary}`}
                            </>
                          ) : m.checked_at ? (
                            `checked ${dateTimeInWords(m.checked_at)}`
                          ) : (
                            "never ran"
                          )}
                        </dd>
                      </div>
                    </dl>
                  </div>
                  {open && (
                    <div className="mission-body" role="region" aria-label="The mission">
                      <MissionPanel key={selected} id={selected} peak={data.peak}
                        onClose={() => go(routeFor("missions"))} onChanged={() => setChanges((n) => n + 1)} />
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Block>
    </div>
  );
}

function ScheduleEditor({ mission, onSave, busy }: { mission: MissionDetail; onSave: (s: MissionSchedule, why: string) => void; busy: boolean }) {
  const [kind, setKind] = useState<MissionSchedule["kind"]>(mission.schedule.kind);
  const [hours, setHours] = useState(String(mission.schedule.kind === "every" ? mission.schedule.hours : 24));
  const [at, setAt] = useState(mission.schedule.kind === "daily" ? mission.schedule.at : "02:00");
  const [why, setWhy] = useState("");
  const schedule: MissionSchedule = kind === "every" ? { kind, hours: Number(hours) } : kind === "daily" ? { kind, at }
    : kind === "after" ? { kind, mission: mission.schedule.kind === "after" ? mission.schedule.mission : "scout_boards" }
      : { kind };
  return (
    <form className="mission-form" onSubmit={(e) => { e.preventDefault(); onSave(schedule, why); }}>
      <label>
        <span>Runs</span>
        <select className="fi-input" value={kind} onChange={(e) => setKind(e.target.value as MissionSchedule["kind"])}>
          <option value="every">Every few hours</option>
          <option value="daily">Every day at a time</option>
          {mission.key !== "scout_boards" && <option value="after">When the search finishes</option>}
          <option value="on_demand">Only when I run it</option>
        </select>
      </label>
      {kind === "every" && (
        <label>
          <span>Hours between runs</span>
          <input type="number" className="fi-input" min={0.25} step={0.25} value={hours} onChange={(e) => setHours(e.target.value)} />
        </label>
      )}
      {kind === "daily" && (
        <label>
          <span>Time of day</span>
          <input type="time" className="fi-input" value={at} onChange={(e) => setAt(e.target.value)} />
        </label>
      )}
      <label>
        <span>Why (kept in the history)</span>
        <input type="text" className="fi-input" value={why} onChange={(e) => setWhy(e.target.value)} />
      </label>
      <button type="submit" className="fi-btn" disabled={busy}>Save schedule</button>
    </form>
  );
}

function SettingsEditor({ mission, onSave, busy }: { mission: MissionDetail; onSave: (s: Record<string, unknown>, why: string) => void; busy: boolean }) {
  const s = mission.settings as Record<string, unknown>;
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(Object.entries(s).map(([k, v]) => [k, v == null ? "" : String(v)])));
  const [why, setWhy] = useState("");
  if (Object.keys(s).length === 0) return null; // no settings, no form
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setValues({ ...values, [k]: e.target.value });
  // A count limit: a number, or -1 for no limit (the server holds it to the number's own range, and says so).
  const num = (k: string, label: string, help: string) => (
    <div className="mission-form-field" key={k}>
      <label htmlFor={`setting-${k}`}>{label}</label>
      <input id={`setting-${k}`} type="number" className="fi-input" min={NO_LIMIT} aria-describedby={`setting-${k}-help`}
        value={values[k] ?? ""} onChange={set(k)} />
      <p id={`setting-${k}-help`} className="fi-note">{help}</p>
    </div>
  );
  const out: Record<string, unknown> = Object.fromEntries(Object.entries(values).map(([k, v]) =>
    [k, ["keep_target", "max_sessions", "target", "count", "details_per_night", "again_after_days"].includes(k)
      ? Number(v) : v === "" && k === "finish_by" ? null : v]));
  return (
    <form className="mission-form" onSubmit={(e) => { e.preventDefault(); onSave(out, why); }}>
      {mission.key === "scout_boards" && (
        <>
          <label>
            <span>Focus (blank: the whole profile)</span>
            <input type="text" className="fi-input" value={values.focus ?? ""} onChange={set("focus")} />
          </label>
          {num("keep_target", "Number of matches", "The roles that pass your gates and reach Ready to Launch, over the night's searches. -1 for no limit.")}
          {num("max_sessions", "Most searches a night", "How many times the scout searches in a night. -1 for no limit.")}
          <p className="fi-note mission-form-hint">The scout keeps searching until every site is searched, or a limit here or under Runs with, the finish time, peak hours or Claude&rsquo;s usage limit stops it.</p>
        </>
      )}
      {mission.key === "analyse_finds" && num("target", "Roles to research a night", "The most roles researched at a time, the ones you moved by hand first and then the best ranked: after each search, and in this mission's own run. -1 for no limit.")}
      {mission.key === "research_employers" && (
        <>
          {num("target", "Employers to research a night", "The most employers researched in a night: those with a role in play first, then the one with the most open roles, then the never researched. -1 for no limit.")}
          <div className="mission-form-field">
            <label htmlFor="setting-again_after_days">Research again after (days)</label>
            <input id="setting-again_after_days" type="number" className="fi-input" min={1} aria-describedby="setting-again_after_days-help"
              value={values.again_after_days ?? ""} onChange={set("again_after_days")} />
            <p id="setting-again_after_days-help" className="fi-note">An employer researched within this many days is left alone.</p>
          </div>
        </>
      )}
      {mission.key === "read_boards" && num("details_per_night", "Descriptions to read a night", "How many postings' descriptions are read from the employer's page in a night. -1 for no limit.")}
      {"finish_by" in s && (
        <label>
          <span>Finish by (blank: no limit)</span>
          <input type="time" className="fi-input" value={values.finish_by ?? ""} onChange={set("finish_by")} />
        </label>
      )}
      <label>
        <span>Why (kept in the history)</span>
        <input type="text" className="fi-input" value={why} onChange={(e) => setWhy(e.target.value)} />
      </label>
      <button type="submit" className="fi-btn" disabled={busy}>Save done conditions</button>
    </form>
  );
}

/** How "Read in your browser" is set up, beside its runs: it is a Claude session scheduled on the owner's own computer,
 * which reaches the app's browser door. The setup command and the steps are the door's own. */
function BrowserSetup({ paused }: { paused: boolean }) {
  return (
    <Block id="mission-setup" level={3} title="Set up on your computer">
      <div className="mission-prose">
        <p>
          A Claude session scheduled on your computer reads the sites you marked under Where to look in your own Chrome
          and hands what it sees to the app. A Pathfinder run maps each site&rsquo;s recipe once,
          and a reading run follows it nightly. It never applies or fills in a form.
        </p>
        <ol>
          <li>On your computer, run <code>resumebot connect</code>. It points your computer&rsquo;s Claude at this app&rsquo;s browser door and gives it the browser token.</li>
          <li>Schedule a session there that follows the steps the browser door gives it.</li>
          {paused && <li>Resume this mission here. It is paused, so a session that calls in is told to stop.</li>}
        </ol>
      </div>
    </Block>
  );
}

/** One mission: what it is for, its controls, its schedule and settings, its runs, its playbook and its history. */
function MissionPanel({ id, peak, onClose, onChanged }: { id: string; peak: PeakView; onClose: () => void; onChanged: () => void }) {
  const refreshKey = useRefreshKey();
  const [changes, setChanges] = useState(0);
  const [pauseUntil, setPauseUntil] = useState("");
  const [editing, setEditing] = useState(false);
  const { data, error } = useResource(() => api.mission(id), [id, refreshKey, changes], {
    fallback: "Couldn't load this mission.",
    liveArea: "missions",
  });
  const done = () => { setChanges((n) => n + 1); onChanged(); };
  const runNow = useAction(async () => { await api.runMission(id); done(); }, { fallback: "Couldn't run it." });
  const pause = useAction(async () => {
    await api.pauseMission(id, pauseUntil ? fromOwnerLocalValue(pauseUntil) : null);
    setPauseUntil("");
    done();
  }, { fallback: "Couldn't pause it." });
  const resume = useAction(async () => { await api.resumeMission(id); done(); }, { fallback: "Couldn't resume it." });
  const save = useAction(async (change: { schedule?: MissionSchedule; settings?: Record<string, unknown>; why: string }) => {
    await api.changeMission(id, change);
    setEditing(false);
    done();
  }, { fallback: "Couldn't save the change." });
  const saveNumbers = useAction(async (values: Record<string, NumberValue>, why: string) => {
    await api.saveMissionNumbers(values, why);
    done();
  }, { fallback: "Couldn't save the settings." });

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !(e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement)) onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (error && !data) return <ErrorBanner>{error}</ErrorBanner>;
  if (!data) return <Loading>Loading the mission&hellip;</Loading>;
  const m = data;
  const busy = runNow.busy || pause.busy || resume.busy || save.busy || saveNumbers.busy;
  const problem = runNow.error ?? pause.error ?? resume.error ?? save.error ?? saveNumbers.error;

  return (
    <div className="mission-panel">
      <div className="mission-actions">
        {!m.runs_elsewhere && (
          <button type="button" className="fi-btn fi-btn-go" disabled={busy || (peak.now && m.needs_claude)}
            title={peak.now && m.needs_claude ? `Claude's peak hours: blocked until ${peak.resumes_words}` : undefined}
            onClick={() => runNow.run()}>Run now</button>
        )}
        {m.state === "on" ? (
          <>
            <label className="fi-vh" htmlFor="pause-until">Pause until, {ownerZoneLabel()} (blank: until you resume it)</label>
            <input id="pause-until" className="fi-input" type="datetime-local" value={pauseUntil} onChange={(e) => setPauseUntil(e.target.value)} />
            <button type="button" className="fi-btn" disabled={busy} onClick={() => pause.run()}>
              {pauseUntil ? "Pause until then" : "Pause"}
            </button>
          </>
        ) : (
          <button type="button" className="fi-btn" disabled={busy} onClick={() => resume.run()}>Resume</button>
        )}
        {!(m.runs_elsewhere && Object.keys(m.settings).length === 0) && (
          <button type="button" className="fi-btn fi-btn-quiet" onClick={() => setEditing((e) => !e)}>
            {editing ? "Done changing" : "Change schedule or done conditions"}
          </button>
        )}
        <button type="button" className="fi-btn fi-btn-quiet" onClick={onClose}>Fold up</button>
      </div>
      {peak.now && m.needs_claude && !m.runs_elsewhere && (
        <p className="mission-note">
          Claude&rsquo;s peak hours: Run now is off until {peak.resumes_words}. To run it now, edit Claude&rsquo;s peak hours above and uncheck &ldquo;Avoid Claude&rsquo;s peak hours&rdquo;.
        </p>
      )}
      {problem && <ErrorBanner as="p">{problem}</ErrorBanner>}
      {editing && (
        <div className="mission-edit">
          {!m.runs_elsewhere && (
            <ScheduleEditor mission={m} busy={busy} onSave={(schedule, why) => save.run({ schedule, why })} />
          )}
          <SettingsEditor mission={m} busy={busy} onSave={(settings, why) => save.run({ settings, why })} />
        </div>
      )}

      {m.runs_elsewhere && <BrowserSetup paused={m.state === "paused"} />}

      {m.numbers.length > 0 && (
        <Block id="mission-numbers" level={3} title="Runs with"
          hint="Saved with your profile settings, so the change shows in their history.">
          <NumbersForm name={`${m.name} settings`} numbers={m.numbers} busy={busy} saveLabel="Save settings"
            onSave={(values, why) => saveNumbers.run(values, why)} />
        </Block>
      )}

      <Block id="mission-runs" level={3} title="Runs" flush>
        {m.runs.length === 0 ? (
          <EmptyState small>
            {m.checked_at ? `Nothing to do so far; last checked ${dateTimeInWords(m.checked_at)}.` : "It hasn't run yet."}
          </EmptyState>
        ) : (
          <ol className="mission-runs">
            <li className="mission-cols" aria-hidden="true">
              <span>When</span>
              <span>How it ended</span>
              <span>What happened</span>
            </li>
            {m.runs.map((r) => (
              <li key={r.id} className="mission-line">
                <span className="mission-run-when">
                  {dateTimeInWords(r.started_at)} &middot;{" "}
                  {r.started_by === "you" ? "you ran it" : r.started_by === "browser" ? "your computer"
                    : r.started_by === "claude" ? "Claude, on demand" : r.started_by === "import" ? "the catch-up after an import" : "on schedule"}
                </span>
                <div><Outcome run={r} /></div>
                <div>
                  <span className="mission-run-summary">{r.summary}</span>
                  {r.instances.length > 0 && (
                    <div className="mission-run-links">
                      {r.instances.map((inst, i) => (
                        <a key={inst} href={serializeRoute(routeFor("agents", { agentsHow: true, liveRunId: inst }))}>
                          {r.instances.length > 1 ? `Run ${i + 1}` : "See the run"}
                        </a>
                      )).reduce<React.ReactNode[]>((acc, el, i) => (i ? [...acc, " · ", el] : [el]), [])}
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ol>
        )}
      </Block>

      {m.playbook && (
        <Block id="mission-playbook" level={3} title="Playbook">
          <div className="mission-playbook">
            <HowTheyWork only={m.playbook} />
          </div>
        </Block>
      )}

      <Block id="mission-history" level={3} title="History" flush>
        {m.changes.length > 0 && (
          <ol className="mission-history">
            <li className="mission-cols" aria-hidden="true">
              <span>When</span>
              <span>What changed</span>
            </li>
            {m.changes.map((c, i) => (
              <li key={i} className="mission-line">
                <span className="mission-run-when">{dateTimeInWords(c.at)} &middot; {c.by}</span>
                <div>
                  <div>{c.what}</div>
                  {c.why && <div className="mission-why">Why: {c.why}</div>}
                </div>
              </li>
            ))}
          </ol>
        )}
      </Block>
    </div>
  );
}
