import type { BoardCard, EndedReason, InterviewInput, RoleDetail, RoleSummary, ScoredAsk } from "../../api/types/pipeline";
import { dateTimeInWords } from "../../lib/dates";
import { formatCount, formatDollars } from "../../lib/format";
import { safeHref } from "../../lib/links";
import { routeFor, serializeRoute } from "../../lib/route";
import { CLOSED, laneOf, type DetailState } from "../../state/pipelineStore";
import CloseMenu from "./CloseMenu";
import { AnswerIcon, BackIcon, LaunchIcon, type Mark } from "./icons";
import { buildingProgress, countsFirstHours, splitCheck } from "./model";
import InterviewsPart from "./InterviewsPart";
import { MarkedPosting } from "./MarkedPosting";
import PacketBar from "./PacketBar";
import PacketPart from "./PacketPart";
import StageControl from "./StageControl";
import Star from "./Star";

interface Props {
  card: BoardCard | null;
  detail: DetailState | undefined;
  endedReasons: EndedReason[];
  busy: boolean;
  backLabel: string;
  onBack: () => void;
  onStar: () => void;
  onMove: (to: string) => void;
  onClose: (reason: string) => void;
  onMoveBack: () => void;
  onAskPacket: (feedback: string) => void;
  onSavePacketFiles: (packetId: string) => void;
  /** The role's interviews (docs/PRD.md 15.2): each throws a refusal, in words, for the form or button that asked. */
  onAddInterview: (body: InterviewInput) => Promise<void>;
  onEditInterview: (id: string, body: InterviewInput) => Promise<void>;
  onRemoveInterview: (id: string) => Promise<void>;
  onPrepareInterview: (id: string) => Promise<void>;
  /** Reads the role again, while an interview's report is being made. */
  onRefresh: () => void;
  /** Changes a required item's grade with a reason (docs/PRD.md 20.8); the role is read again after. */
  onCorrect: (index: number, cls: string, why: string) => Promise<{ said?: string }>;
}

/** An ask's mark, by what it did to the fit (as the score card reads it): a gain, a penalty, a miss, or not known. */
function askMark(a: ScoredAsk): { mark: Mark; tone: string } {
  if (a.state !== "checked" || a.earned == null) return { mark: "unknown", tone: "unknown" };
  if (a.earned > 0) return { mark: "met", tone: "met" };
  if (a.earned < 0) return { mark: "no", tone: "no" };
  return { mark: "partly", tone: "miss" };
}

function paragraphs(text: string): string[] {
  return text.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean);
}

function Facts({ role, detail }: { role: BoardCard; detail: RoleDetail | null }) {
  const asks = (detail?.scoring.asks ?? []).filter((a) => a.state !== "unscored");
  const met = asks.filter((a) => (a.earned ?? 0) > 0).length;
  const fit = role.score ?? role.fit_pct;
  const employer = detail?.employer;
  const size = employer?.employees ? `${formatCount(employer.employees)} people` : null;
  return (
    <dl className="fi-facts">
      <div>
        <dt>Fit</dt>
        <dd>{fit == null ? "Not scored" : `${fit}%`}</dd>
        {countsFirstHours(role) && <dd className="fi-facts-sub">Its first hours count in this number</dd>}
        {asks.length > 0 && <dd className="fi-facts-sub">{met} of {asks.length} asks earned points</dd>}
      </div>
      <div>
        <dt>Pay</dt>
        <dd>{role.pay || "Not stated"}</dd>
      </div>
      <div>
        <dt>Ask for</dt>
        <dd>{role.ask ? formatDollars(role.ask) : "Not worked out yet"}</dd>
      </div>
      {employer && (size || employer.industry) && (
        <div>
          <dt>Company</dt>
          <dd>{size ?? employer.industry}</dd>
          {size && employer.industry && <dd className="fi-facts-sub">{employer.industry}</dd>}
        </div>
      )}
    </dl>
  );
}

/** The analyst's three lines: what the company does, what the role is, and why it is good for the owner. */
function Summary({ summary }: { summary: RoleSummary }) {
  return (
    <section className="fi-sec fi-summary" aria-label="Summary">
      <p>
        <b>The company</b> {summary.company}
      </p>
      <p>
        <b>The role</b> {summary.role}
      </p>
      <p>
        <b>Why it&rsquo;s for you</b> {summary.why}
      </p>
    </section>
  );
}

/** The role, open beside the list: who and where, its stage, its pay and ask, why it fits, its hard passes, the posting
 * and its history. On a phone it is a page of its own, with the way back to the list. */
export default function RolePane({ card, detail, endedReasons, busy, backLabel, onBack, onStar, onMove, onClose, onMoveBack, onAskPacket, onSavePacketFiles, onAddInterview, onEditInterview, onRemoveInterview, onPrepareInterview, onRefresh, onCorrect }: Props) {
  const data = detail?.data ?? null;
  const role: BoardCard | null = data?.role ?? card;
  if (!role) {
    return (
      <section className="fi-pane" aria-label="Role">
        <div className="fi-pane-in">
          <p className="fi-pane-note">{detail?.status === "error" ? detail.error : "Pick a role to see it here."}</p>
        </div>
      </section>
    );
  }
  const closed = laneOf(role.status) === CLOSED;
  const ready = role.status === "ready_to_launch";
  const posting = safeHref(data?.role.employer_url ?? role.url);
  const employerHref = role.employer_id ? serializeRoute(routeFor("employers", { employerId: role.employer_id })) : undefined;
  const building = buildingProgress(role);
  const { next: readyNext, check: readyCheck } = splitCheck(role);
  const asks = (data?.scoring.asks ?? []).filter((a) => a.state !== "unscored");
  const met = asks.filter((a) => (a.earned ?? 0) > 0).length;
  const gates = data?.gates ?? [];
  const passed = gates.filter((g) => g.result === "pass").length;
  // This role's own way in: the employer's application when it is known, else the posting. (Launch in Browser, on the
  // list, opens every Ready to Launch role; this opens only this one.)
  // The posting carries the marks of the role's graded qualifications and duties (docs/PRD.md 20.8).
  const requirements = data?.requirements ?? [];
  const preferred = data?.preferred ?? [];
  const responsibilities = data?.responsibilities ?? [];
  const graded = requirements.length + preferred.length + responsibilities.length > 0
    ? { correct: onCorrect, requirements, preferred, responsibilities } : null;
  const applyHref = safeHref(role.apply_url);
  const openHref = applyHref ?? posting;

  return (
    <section className="fi-pane" aria-label="Role">
      <div className="fi-pane-in" key={role.id}>
        <button type="button" className="fi-back" onClick={onBack}>
          <BackIcon className="fi-btn-icon" />
          {backLabel}
        </button>
        <header className="fi-head">
          <div>
            <h2>{role.title}</h2>
            <p className="fi-head-meta">
              {employerHref ? <a href={employerHref}>{role.company}</a> : <span>{role.company}</span>}
              {role.work?.words && role.work.words !== "Not stated" && <span>{role.work.words}</span>}
              {role.age?.words && <span>{role.age.from === "posted" ? "Posted" : "Found"} {role.age.words.toLowerCase()}{/^\d/.test(role.age.words) ? " ago" : ""}</span>}
              {posting && (
                <a href={posting} target="_blank" rel="noreferrer">
                  Original posting
                </a>
              )}
            </p>
          </div>
          <Star on={!!role.favorite} title={role.title} busy={busy} large onToggle={onStar} />
        </header>

        <div className="fi-mover">
          {closed ? (
            <>
              <span className="fi-closed-as">Closed: {role.next ?? role.status_label}</span>
              <button type="button" className="fi-btn" disabled={busy} onClick={onMoveBack}>
                Move back
              </button>
            </>
          ) : (
            <>
              <StageControl status={role.status} busy={busy} onMove={onMove} />
              <CloseMenu reasons={endedReasons} busy={busy} onClose={onClose} />
            </>
          )}
        </div>

        {ready && (role.opened_at || building || readyNext || readyCheck || openHref) && (
          <div className="fi-launch">
            {(role.opened_at || building || readyNext || readyCheck) && (
              <div className="fi-launch-text">
                {role.opened_at ? (
                  <>
                    <b>Opened in your browser</b>
                    <span>Mark it Applied when you have sent it</span>
                  </>
                ) : building ? (
                  <PacketBar progress={building} />
                ) : (
                  readyNext && <span>{readyNext}</span>
                )}
                {readyCheck && <span className="fi-launch-check">{readyCheck}</span>}
              </div>
            )}
            {openHref && (
              <a className="fi-btn fi-btn-go" href={openHref} target="_blank" rel="noreferrer">
                <LaunchIcon className="fi-btn-icon" />
                {applyHref ? "Open the application" : "Open the posting"}
              </a>
            )}
          </div>
        )}

        <Facts role={role} detail={data} />

        {data?.summary && <Summary summary={data.summary} />}

        {data && <PacketPart packets={data.packets} busy={busy} blocked={!!data.peak?.now} onAsk={onAskPacket} onSaveFiles={onSavePacketFiles} />}

        {data && (
          <InterviewsPart
            stage={role.status}
            interviews={data.interviews ?? []}
            onAdd={onAddInterview}
            onEdit={onEditInterview}
            onRemove={onRemoveInterview}
            onPrepare={onPrepareInterview}
            onRefresh={onRefresh}
          />
        )}

        {detail?.status === "loading" && !data && <p className="fi-pane-note">Reading the role&hellip;</p>}
        {detail?.status === "error" && <p className="fi-pane-note" role="alert">{detail.error}</p>}

        {asks.length > 0 && (
          <section className="fi-sec">
            <h3>
              Why it fits <span>{met} of {asks.length} earned points</span>
            </h3>
            <ul className="fi-checks">
              {asks.map((a) => {
                const { mark, tone } = askMark(a);
                return (
                  <li key={a.id}>
                    <span className={`fi-dot is-${tone}`}>
                      <AnswerIcon mark={mark} />
                    </span>
                    <div>
                      {a.says}
                      {a.evidence && <em>{a.evidence}</em>}
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        )}

        {gates.length > 0 && (
          <section className="fi-sec">
            <h3>
              Hard passes <span>{passed} of {gates.length} passed</span>
            </h3>
            <ul className="fi-checks">
              {gates.map((g) => {
                const mark: Mark = g.result === "pass" ? "met" : g.result === "fail" ? "no" : "unknown";
                return (
                  <li key={g.id}>
                    <span className={`fi-dot is-${mark}`}>
                      <AnswerIcon mark={mark} />
                    </span>
                    <div>
                      {g.says}
                      {g.fact && <em>{g.fact}</em>}
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        )}

        {data?.role.description && (
          <section className="fi-sec">
            <h3>The posting</h3>
            {data.role.description_from === "board" && (
              <p className="fi-note">
                This is the job board&rsquo;s copy of the posting: the employer&rsquo;s own posting could not be read.
              </p>
            )}
            <div className={graded ? "fi-desc fi-desc-marked" : "fi-desc"}>
              {graded ? (
                <MarkedPosting text={data.role.description} graded={graded} />
              ) : (
                paragraphs(data.role.description).map((p, i) => <p key={i}>{p}</p>)
              )}
            </div>
          </section>
        )}

        {data && data.timeline.length > 0 && (
          <section className="fi-sec">
            <h3>History</h3>
            <ol className="fi-hist">
              {data.timeline.map((e, i) => (
                <li key={i}>
                  {e.sentence}
                  {e.at && <small>{dateTimeInWords(e.at)}</small>}
                </li>
              ))}
            </ol>
          </section>
        )}
      </div>
    </section>
  );
}
