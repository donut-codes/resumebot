import type { BlockInk } from "../focus/parts";

/** The most words a state may have and still be set as a tag. */
export const TAG_MAX_WORDS = 3;

/** How many words a state is told in. */
export function wordCount(words: string): number {
  return words.trim().split(/\s+/).filter(Boolean).length;
}

interface Props {
  /** The state, in words. */
  words: string;
  /** The state's ink; none for a state with no job (off, set aside). */
  ink?: BlockInk;
  /** A short state that needs the owner carries the lamp in front of its word (a long one carries its square). */
  lamp?: boolean;
  className?: string;
  /** For a control that describes itself by this state (aria-describedby). */
  id?: string;
}

/** A state told in words, set the one way it can be read: a badge (a word or three on a wash of the state's ink) when it
 * is three words or fewer ("Paused", "On a task"), and, longer, an ordinary line of body type with a small dot of the
 * state's ink in front, because a sentence in a badge is not read (styles/focus/agents.css `.fi-state-words`). Every
 * screen that tells a state the app words ("Waits: the scouting mission is paused", "Paused until Oct 12, 3:00 AM") sets
 * it here, so the choice is made in one place. */
export default function StateWords({ words, ink, lamp = false, className = "", id }: Props) {
  const extra = className ? ` ${className}` : "";
  if (wordCount(words) <= TAG_MAX_WORDS) {
    return (
      <span id={id} className={`fi-badge fi-state${extra}`} data-ink={ink}>
        {lamp && <span className="fi-state-lamp" aria-hidden="true" />}
        {words}
      </span>
    );
  }
  return (
    <span id={id} className={`fi-state-words${extra}`} data-ink={ink}>
      {words}
    </span>
  );
}
