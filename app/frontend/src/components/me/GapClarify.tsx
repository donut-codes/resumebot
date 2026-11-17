import { useId, useState } from "react";
import type { GapCard, GapClarify as Clarify, GapProposal, Resume } from "../../api/types/me";
import { useAction } from "../../lib/useAction";
import { MeAlert } from "./MeSection";
import { cardsByTheme, MIN_ANSWER, placeWords } from "./gaps";

/** What the owner can ask of the Coach and do with its cards and proposals; the screen that owns the page's calls (FindGaps)
 * supplies them, so this part holds no call of its own. */
export interface ClarifyActions {
  askQuestions: (runId: string) => Promise<unknown>;
  suggestLines: (runId: string) => Promise<unknown>;
  answerCard: (cardId: string, action: "answer" | "skip" | "not_true", text: string) => Promise<unknown>;
  /** Accept a proposal (with the owner's own words, if they edited them): the master as saved and its version. */
  acceptProposal: (id: string, text?: string) => Promise<{ version: number; resume: Resume }>;
  rejectProposal: (id: string) => Promise<unknown>;
}

interface Props {
  runId: string;
  actions: ClarifyActions;
  /** The Coach's cards and proposals for the run, and what can be asked of it now. */
  clarify: Clarify;
  /** Whether an agent can take work now, and why not in words (peak hours...): the same rule as Import your resume. */
  ready: boolean;
  why: string | null;
  whyId: string;
  /** The run is read again (a card was answered, a job was asked for, a proposal was decided). */
  onChanged: () => void;
  /** A proposal was accepted: the master resume as saved, and its new version. */
  onSavedResume: (resume: Resume, version: number) => void;
}

/** Working through the gaps with the Coach (docs/PRD.md 38.13 to 38.17), under a finished run started from a job: its
 * questions as cards (answer in your own words, Skip, Not true), Suggest lines (master lines in your own words, each citing
 * your answer and saying where it goes) and each proposal's Accept, Edit and Reject. Only Accept writes the master. */
export default function GapClarify({ runId, actions, clarify, ready, why, whyId, onChanged, onSavedResume }: Props) {
  const going = clarify.coach !== null && (clarify.coach.state === "waiting" || clarify.coach.state === "working");
  const describedBy = !ready && why ? whyId : undefined;
  const offTitle = !ready ? (why ?? undefined) : undefined;
  const ask = useAction(
    async (which: "questions" | "lines") => {
      if (which === "questions") await actions.askQuestions(runId);
      else await actions.suggestLines(runId);
      onChanged();
    },
    { fallback: "Couldn't start it." },
  );
  const groups = cardsByTheme(clarify.cards);
  const waiting = clarify.proposals.filter((p) => p.status === "proposed");
  const decided = clarify.proposals.filter((p) => p.status !== "proposed");

  return (
    <div className="fi-me-gap-clarify">
      <div className="fi-me-gap-clarify-head">
        <h3>Work through the gaps</h3>
        <div className="fi-me-gap-run-actions">
          <button
            type="button"
            className="fi-btn"
            disabled={!ready || going || ask.busy || !clarify.can_ask}
            title={offTitle}
            aria-describedby={describedBy}
            onClick={() => void ask.run("questions")}
          >
            Work through the gaps
          </button>
          <button
            type="button"
            className="fi-btn"
            disabled={!ready || going || ask.busy || !clarify.can_propose}
            title={offTitle}
            aria-describedby={describedBy}
            onClick={() => void ask.run("lines")}
          >
            Suggest lines
          </button>
        </div>
      </div>
      {clarify.coach && clarify.coach.state !== "failed" && (
        <p className="fi-me-gap-run-line" role="status">
          {clarify.coach.step}
        </p>
      )}
      {clarify.coach?.state === "failed" && <MeAlert>{clarify.coach.error || "The Coach stopped."}</MeAlert>}
      {ask.error && <MeAlert>{ask.error}</MeAlert>}

      {groups.map((g) => (
        <div key={g.theme} className="fi-me-gap-card-group">
          <h4>
            {g.theme} <span className="fi-badge fi-me-tag">{g.grade === "T" ? "Transferable" : "Unproven"}</span>
          </h4>
          {g.cards.map((c) => (
            <Card key={`${c.id}-${c.state}`} card={c} actions={actions} onChanged={onChanged} />
          ))}
        </div>
      ))}

      {waiting.length > 0 && (
        <div className="fi-me-gap-proposals">
          <h4>Lines the Coach suggests</h4>
          {waiting.map((p) => (
            <Proposal key={p.id} proposal={p} actions={actions} onChanged={onChanged} onSavedResume={onSavedResume} />
          ))}
        </div>
      )}
      {decided.length > 0 && (
        <ul className="fi-me-gap-hidden">
          {decided.map((p) => (
            <li key={p.id}>
              <span>{p.text}</span>
              <span className="fi-me-quiet">{p.status === "accepted" ? "Added to your resume" : "Rejected"}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** One question: its box for the owner's words and Answer, Skip and Not true. A card the owner marked not true is closed. */
function Card({ card, actions, onChanged }: { card: GapCard; actions: ClarifyActions; onChanged: () => void }) {
  const uid = useId();
  const [words, setWords] = useState(card.answer);
  const act = useAction(
    async (action: "answer" | "skip" | "not_true") => {
      await actions.answerCard(card.id, action, action === "answer" ? words.trim() : "");
      onChanged();
    },
    { fallback: "Couldn't save that." },
  );
  const closed = card.state === "not_true" || !card.can_change;
  return (
    <div className="fi-me-gap-card" data-state={card.state}>
      <label className="fi-me-gap-label" htmlFor={`${uid}-words`}>
        {card.question}
      </label>
      {card.state === "not_true" ? (
        <p className="fi-me-quiet">Marked not true: you won&rsquo;t be asked about this for this role again.</p>
      ) : (
        <textarea
          id={`${uid}-words`}
          className="fi-input"
          value={words}
          disabled={closed}
          onChange={(e) => setWords(e.target.value)}
          aria-label={card.question}
        />
      )}
      {card.state === "skipped" && <p className="fi-me-quiet">Skipped</p>}
      {card.state === "answered" && !closed && <p className="fi-me-quiet">Answered</p>}
      {!closed && (
        <div className="fi-me-gap-card-actions">
          <button type="button" className="fi-btn" disabled={act.busy || words.trim().length < MIN_ANSWER} onClick={() => void act.run("answer")}>
            Answer
          </button>
          <button type="button" className="fi-btn" disabled={act.busy} onClick={() => void act.run("skip")}>
            Skip
          </button>
          <button type="button" className="fi-btn" disabled={act.busy} onClick={() => void act.run("not_true")}>
            Not true
          </button>
        </div>
      )}
      {act.error && <MeAlert>{act.error}</MeAlert>}
    </div>
  );
}

/** One proposed line: its words, where it goes, the answer it came from, and Accept, Edit and Reject. Accept saves a new
 * version of the master; Edit opens the words for the owner to rewrite before Accept saves their own. */
export function Proposal({
  proposal,
  actions,
  onChanged,
  onSavedResume,
}: {
  proposal: GapProposal;
  actions: ClarifyActions;
  onChanged: () => void;
  onSavedResume: (resume: Resume, version: number) => void;
}) {
  const uid = useId();
  const [editing, setEditing] = useState(false);
  const [words, setWords] = useState(proposal.text);
  const accept = useAction(
    async () => {
      const saved = await actions.acceptProposal(proposal.id, editing ? words.trim() : undefined);
      onSavedResume(saved.resume, saved.version);
      onChanged();
    },
    { fallback: "Couldn't add it to your resume." },
  );
  const reject = useAction(
    async () => {
      await actions.rejectProposal(proposal.id);
      onChanged();
    },
    { fallback: "Couldn't reject it." },
  );
  const busy = accept.busy || reject.busy;
  return (
    <div className="fi-me-gap-proposal">
      {editing ? (
        <textarea id={`${uid}-words`} className="fi-input" value={words} onChange={(e) => setWords(e.target.value)} aria-label="The line, in your words" />
      ) : (
        <p className="fi-me-gap-proposal-line">{proposal.text}</p>
      )}
      <p className="fi-me-gap-proposal-place">{placeWords(proposal.place)}</p>
      {proposal.why && <p className="fi-me-quiet">{proposal.why}</p>}
      <p className="fi-me-gap-quote">
        <b>{proposal.source === "owner.claude" ? "What your Claude says you told it:" : "From your answer:"}</b> {proposal.quote}
      </p>
      <div className="fi-me-gap-card-actions">
        <button type="button" className="fi-btn" disabled={busy || (editing && !words.trim())} onClick={() => void accept.run()}>
          Accept
        </button>
        {!editing && (
          <button type="button" className="fi-btn" disabled={busy} onClick={() => setEditing(true)}>
            Edit
          </button>
        )}
        <button type="button" className="fi-btn" disabled={busy} onClick={() => void reject.run()}>
          Reject
        </button>
      </div>
      {(accept.error ?? reject.error) && <MeAlert>{(accept.error ?? reject.error) as string}</MeAlert>}
    </div>
  );
}
