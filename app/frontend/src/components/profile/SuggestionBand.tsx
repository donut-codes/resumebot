import { useId } from "react";
import type { Ask, Suggestion } from "../../api/types/profile";
import { pointsLabel } from "../../lib/asks";
import { BAND_WORDS } from "./profileSpec";
import type { Card } from "./setLogic";

/** A suggestion to put asks in one row that the page can still use, with what the card says of it. */
export interface BandCard {
  suggestion: Suggestion;
  /** The owner's own asks it names, in the order proposed, and the ones that would move into another part. */
  card: Card;
  /** The part it would sit in, by its name on the page. */
  part: string;
  /** The role whose tab it is for, by name; none for All. */
  role: string | null;
}

/** A suggestion to remove asks the graded requirements count again, with the asks of it that are still on the page. */
export interface OverlapCard {
  suggestion: Suggestion;
  asks: Ask[];
}

/** Points as they are written everywhere on the page: plus, minus (a true minus sign) or a bare 0. */
const signed = (n: number) => (n > 0 ? `+${n}` : n < 0 ? `−${Math.abs(n)}` : "0");
const signOf = (n: number) => (n > 0 ? "plus" : n < 0 ? "minus" : "none");

interface Props {
  cards: BandCard[];
  /** Only while the requirements are graded in the draft: asks the owner has that grading counts again. */
  overlaps?: OverlapCard[];
  onUse: (id: string) => void;
  /** Put a suggestion of either kind off. */
  onLater: (id: string) => void;
  /** Take the asks of an overlap suggestion out of the draft. */
  onDrop?: (id: string) => void;
}

/** The band at the head of Prioritization while an agent has left a suggestion the owner can still use.
 * First, asks the owner already has that cannot both be true of one posting, offered as one "first match" or "any of" row: a block
 * for each, with their own sentences in the order proposed (their points beside them, or the row's points once), one plain line
 * of why, and two buttons. Second, only while the requirements are graded in the draft, asks that count what grading counts:
 * their sentences with their points, why, and "Remove these asks". Nothing changes by itself: using one puts the change in the
 * page's draft, and the page's one Save keeps it. */
export default function SuggestionBand({ cards, overlaps = [], onUse, onLater, onDrop }: Props) {
  const uid = useId();
  if (cards.length === 0 && overlaps.length === 0) return null;
  return (
    <div className="fi-ss-sug-band">
      {cards.length > 0 && (
        <section className="fi-ss-sug-block" aria-labelledby={`${uid}-head`}>
          <h2 id={`${uid}-head`} className="fi-ss-sug-head" tabIndex={-1}>
            {BAND_WORDS.head}
          </h2>
          <p className="fi-ss-sug-lead">{BAND_WORDS.lead}</p>
          <ul className="fi-ss-sug-cards">
            {cards.map(({ suggestion, card, part, role }) => {
              const best = suggestion.kind === "best";
              const first = card.members[0].says;
              const List = best ? "ol" : "ul";
              return (
                <li key={suggestion.id} className="fi-ss-sug-card">
                  <p className="fi-ss-sug-kind">{`${best ? "First match" : "Any of"} in ${part}${role ? `, for ${role}` : ""}`}</p>
                  <div className="fi-ss-sug-body">
                    {!best && (
                      <p className="fi-ss-sug-points" data-sign={signOf(suggestion.points)}>
                        {signed(suggestion.points)}
                        <span className="fi-vh"> points, once</span>
                      </p>
                    )}
                    <List className="fi-ss-sug-asks">
                      {card.members.map((m) => (
                        <li key={m.id} className="fi-ss-sug-ask">
                          {best && (
                            <span className="fi-ss-sug-points" data-sign={signOf(m.points)}>
                              {signed(m.points)}
                              <span className="fi-vh"> points</span>
                            </span>
                          )}
                          <span className="fi-ss-sug-says">{m.says}</span>
                        </li>
                      ))}
                    </List>
                  </div>
                  <p className="fi-ss-sug-why">{suggestion.why}</p>
                  {card.moves.map((m) => (
                    <p key={m.id} className="fi-ss-sug-moves">
                      This moves {m.says} into {part}.
                    </p>
                  ))}
                  <div className="fi-ss-sug-actions">
                    <button type="button" className="fi-btn" aria-label={`${BAND_WORDS.use}: ${first}`} onClick={() => onUse(suggestion.id)}>
                      {BAND_WORDS.use}
                    </button>
                    <button type="button" className="fi-btn fi-btn-quiet" aria-label={`${BAND_WORDS.later}: ${first}`} onClick={() => onLater(suggestion.id)}>
                      {BAND_WORDS.later}
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      )}
      {overlaps.length > 0 && (
        <section className="fi-ss-sug-block" aria-labelledby={`${uid}-overlap`}>
          <h2 id={`${uid}-overlap`} className="fi-ss-sug-head" tabIndex={-1}>
            {BAND_WORDS.overlapHead}
          </h2>
          <p className="fi-ss-sug-lead">{BAND_WORDS.overlapLead}</p>
          <ul className="fi-ss-sug-cards">
            {overlaps.map(({ suggestion, asks }) => (
              <li key={suggestion.id} className="fi-ss-sug-card">
                <ul className="fi-ss-sug-asks">
                  {asks.map((a) => (
                    <li key={a.id} className="fi-ss-sug-ask">
                      <span className="fi-ss-sug-points" data-sign={signOf(a.points)}>
                        {pointsLabel(a)}
                        <span className="fi-vh"> points</span>
                      </span>
                      <span className="fi-ss-sug-says">{a.says}</span>
                    </li>
                  ))}
                </ul>
                <p className="fi-ss-sug-why">{suggestion.why}</p>
                <div className="fi-ss-sug-actions">
                  <button type="button" className="fi-btn" aria-label={`${BAND_WORDS.drop}: ${asks[0].says}`} onClick={() => onDrop?.(suggestion.id)}>
                    {BAND_WORDS.drop}
                  </button>
                  <button type="button" className="fi-btn fi-btn-quiet" aria-label={`${BAND_WORDS.later}: ${asks[0].says}`} onClick={() => onLater(suggestion.id)}>
                    {BAND_WORDS.later}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
