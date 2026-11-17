import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import type { Ask, Group } from "../../api/types/profile";
import { newId } from "../../lib/ids";
import GrowingText from "../primitives/GrowingText";
import AskDecidedBy, { byEmployerRecord, canChooseWhoDecides } from "./AskDecidedBy";
import AskVerify from "./AskVerify";
import { EARNS_NOTHING } from "../../lib/asks";
import { AskFacts, PointsInput } from "./EditParts";
import { DECIDER_WORDS, SET_WORDS } from "./profileSpec";
import { idsOf, worthMoreBelow } from "./setLogic";

interface RowProps {
  group: Group;
  /** The id of a statement whose sentence takes keyboard focus as it appears (the first of a row just added). */
  focus: string | null;
  /** Every id the document uses, so a statement added here gets one that is free. */
  taken?: readonly { id: string }[];
  /** Takes keyboard focus on its heading as it appears (a row just made from a suggestion); `onFocused` tells the page it
   * has, so the page asks for it once. */
  focusHead?: boolean;
  onFocused?: () => void;
  onChange: (next: Group) => void;
  onRemove: () => void;
}

type Words = (typeof SET_WORDS)[keyof typeof SET_WORDS];

/** What a row is called to a screen reader: its first sentence, or "a new row" while it has none. */
const nameOf = (group: Group, words: Words): string => group.members.find((m) => m.says.trim() !== "")?.says ?? words.blank;

/** A new statement, ready to write in: a reader's ask in the row's part and tab, one point in a first match row, none in an
 * "any of" row (that row has its points on the row). */
function newStatement(group: Group, taken: readonly { id: string }[]): Ask {
  return {
    id: newId({ name: SET_WORDS.best.row }, [...taken, ...idsOf([], [group])]),
    section: group.section,
    says: "",
    points: group.kind === "best" ? 1 : 0,
    checked_by: "reader",
    roles: [...(group.roles ?? [])],
  };
}

/** One statement on its row: the same row as an ask's, the points in their ruled column (a statement of an "any of" row has
 * none, its cell is left empty so the rule runs on), the sentence beside them, then the value the app decides it from (when
 * it does), then its quiet buttons. */
function StatementRow({
  statement, words, points, moves, focus, onChange, onRemove,
}: {
  statement: Ask;
  words: Words;
  /** A statement of a first match row has points of its own; one of an "any of" row has none. */
  points: boolean;
  /** Move up and Move down, where there is a place to move to; none in an "any of" row, whose order means nothing. */
  moves?: { up?: () => void; down?: () => void };
  focus: boolean;
  onChange: (next: Ask) => void;
  onRemove: () => void;
}) {
  const name = statement.says || words.unnamed;
  return (
    <li className="fi-ss-row">
      <div className="fi-ss-points-cell">
        {points && <PointsInput value={statement.points} label={`Points for ${name}`} onChange={(next) => onChange({ ...statement, points: next })} />}
      </div>
      <div className="fi-ss-body">
        <GrowingText
          className="fi-input"
          aria-label={words.sentence}
          placeholder="For example: The role manages a team"
          // The statement just added takes the keyboard, as an ask just added does.
          // eslint-disable-next-line jsx-a11y/no-autofocus -- it is what the owner just made, and the cursor belongs in it
          autoFocus={focus}
          oneLine
          value={statement.says}
          onText={(says) => onChange({ ...statement, says })}
        />
        {points && statement.points < 0 && <p className="fi-ss-sentence" role="note">{EARNS_NOTHING}</p>}
        <AskVerify ask={statement} name={name} onChange={onChange} />
        <AskDecidedBy ask={statement} name={name} onChange={onChange} />
        <AskFacts ask={statement} name={name} onChange={onChange} />
        <div className="fi-ss-foot">
          {moves?.up && (
            <button type="button" className="fi-link" data-move={`${statement.id}:up`} aria-label={`Move up: ${name}`} onClick={moves.up}>
              Move up
            </button>
          )}
          {moves?.down && (
            <button type="button" className="fi-link" data-move={`${statement.id}:down`} aria-label={`Move down: ${name}`} onClick={moves.down}>
              Move down
            </button>
          )}
          <button type="button" className="fi-link is-danger" aria-label={`Remove ${name}`} onClick={onRemove}>
            Remove
          </button>
          {statement.checked_by === "app" && <span className="fi-badge fi-badge-quiet">automatic</span>}
        </div>
      </div>
    </li>
  );
}

/** The frame of a row: a named block in the part's ink, headed by what it is, with its one plain line. */
function Frame({
  group, words, headId, hintId, lead, focusHead = false, onFocused, children,
}: {
  group: Group;
  words: Words;
  headId: string;
  hintId: string;
  /** The "any of" row's own points, at its head, in the points column. */
  lead?: ReactNode;
  focusHead?: boolean;
  onFocused?: () => void;
  children: ReactNode;
}) {
  const head = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (!focusHead) return;
    head.current?.focus();
    onFocused?.();
  }, [focusHead, onFocused]);
  return (
    <div className="fi-ss-set" role="group" aria-labelledby={headId} aria-describedby={hintId} data-kind={group.kind}>
      <div className={lead ? "fi-ss-set-head is-points" : "fi-ss-set-head"}>
        {lead && <div className="fi-ss-points-cell">{lead}</div>}
        <div className="fi-ss-set-head-text">
          <h3 id={headId} className="fi-ss-set-title" ref={head} tabIndex={-1}>
            {words.title}
          </h3>
          <p id={hintId} className="fi-ss-note">
            {words.hint}
          </p>
        </div>
      </div>
      {children}
    </div>
  );
}

/** The row's two buttons: one to add a statement to it, and, apart, the one that takes the whole row away. */
function Foot({ words, name, onAdd, onRemove, onWhole }: { words: Words; name: string; onAdd: () => void; onRemove: () => void; onWhole?: () => void }) {
  return (
    <div className="fi-ss-foot is-set">
      <button type="button" className="fi-link" aria-label={`${words.moreFor}: ${name}`} onClick={onAdd}>
        {words.more}
      </button>
      {onWhole && (
        <button type="button" className="fi-link" aria-label={`${DECIDER_WORDS.wholeRow}: ${name}`} onClick={onWhole}>
          {DECIDER_WORDS.wholeRow}
        </button>
      )}
      <button type="button" className="fi-link is-danger fi-ss-set-remove" aria-label={`${words.remove}: ${name}`} onClick={onRemove}>
        {words.remove}
      </button>
    </div>
  );
}

/** "First match (top wins)": statements, best first, each a sentence and its points; the first one that is true counts. Moved
 * with Move up and Move down, which keep the keyboard on the button pressed and say, for a screen reader, where the statement
 * is now. One quiet line says when a statement lower down is worth more than the one above it; it never stops a save. Nothing
 * here saves: the page has one Save. */
export function FirstMatchRow({ group, focus, taken = [], focusHead, onFocused, onChange, onRemove }: RowProps) {
  const words = SET_WORDS.best;
  const uid = useId();
  const root = useRef<HTMLDivElement>(null);
  const [moved, setMoved] = useState<{ id: string; dir: "up" | "down" } | null>(null);
  const [status, setStatus] = useState("");
  const [added, setAdded] = useState<string | null>(null);
  const name = nameOf(group, words);
  const statements = group.members;

  // A moved statement's row is the same row in a new place, and the browser drops the keyboard when it moves: put it back on
  // the button that was pressed, or on the other one when the move took the statement to an end.
  useEffect(() => {
    if (!moved) return;
    const buttons = Array.from(root.current?.querySelectorAll<HTMLElement>("[data-move]") ?? []);
    const find = (dir: string) => buttons.find((b) => b.getAttribute("data-move") === `${moved.id}:${dir}`);
    (find(moved.dir) ?? find(moved.dir === "up" ? "down" : "up"))?.focus();
  }, [moved]);

  const setStatement = (i: number, next: Ask) => onChange({ ...group, members: statements.map((m, j) => (j === i ? next : m)) });
  const move = (i: number, to: number) => {
    const next = [...statements];
    const [statement] = next.splice(i, 1);
    next.splice(to, 0, statement);
    onChange({ ...group, members: next });
    const dir = to < i ? "up" : "down";
    setMoved({ id: statement.id, dir });
    setStatus(`Moved ${dir}: ${statement.says || words.unnamed}. Now statement ${to + 1} of ${statements.length}.`);
  };
  const add = () => {
    const statement = newStatement(group, taken);
    onChange({ ...group, members: [...statements, statement] });
    setAdded(statement.id);
  };
  // A first match row is either all Claude's or all the app's. With some statements the app's and some Claude's, the row offers
  // the same for all: every plain statement still Claude's is let to the app, from the same fact and comparison, its number
  // left blank for the owner to type. (A mixed row that is saved is refused by the model, in its own words.)
  const decided = statements.find(byEmployerRecord);
  const mixed = decided !== undefined && statements.some((s) => !byEmployerRecord(s) && canChooseWhoDecides(s));
  const wholeRow = () =>
    onChange({
      ...group,
      members: statements.map((s) =>
        decided && !byEmployerRecord(s) && canChooseWhoDecides(s) ? { ...s, checked_by: "app", fact: decided.fact, op: decided.op, value: "" } : s,
      ),
    });

  return (
    <div ref={root}>
      <Frame group={group} words={words} headId={`${uid}-head`} hintId={`${uid}-hint`} focusHead={focusHead} onFocused={onFocused}>
        <ol className="fi-ss-set-rows">
          {statements.map((statement, i) => (
            <StatementRow
              key={statement.id}
              statement={statement}
              words={words}
              points
              moves={{ up: i > 0 ? () => move(i, i - 1) : undefined, down: i < statements.length - 1 ? () => move(i, i + 1) : undefined }}
              focus={statement.id === focus || statement.id === added}
              onChange={(next) => setStatement(i, next)}
              onRemove={() => onChange({ ...group, members: statements.filter((_, j) => j !== i) })}
            />
          ))}
        </ol>
        {worthMoreBelow(statements) && (
          <p className="fi-ss-note" role="note">
            {words.worthMore}
          </p>
        )}
        <Foot words={words} name={name} onAdd={add} onRemove={onRemove} onWhole={mixed ? wholeRow : undefined} />
      </Frame>
      <p className="fi-vh" role="status">
        {status}
      </p>
    </div>
  );
}

/** "Any of these": statements, each a sentence with no points of its own, and one points box for the row at its head. The row
 * earns its points once, when any one statement is true. The order of statements means nothing, so they have no Move up or
 * Move down. */
export function AnyOfRow({ group, focus, taken = [], focusHead, onFocused, onChange, onRemove }: RowProps) {
  const words = SET_WORDS.any;
  const uid = useId();
  const [added, setAdded] = useState<string | null>(null);
  const name = nameOf(group, words);
  const statements = group.members;

  const setStatement = (i: number, next: Ask) => onChange({ ...group, members: statements.map((m, j) => (j === i ? next : m)) });
  const add = () => {
    const statement = newStatement(group, taken);
    onChange({ ...group, members: [...statements, statement] });
    setAdded(statement.id);
  };

  return (
    <Frame
      group={group}
      words={words}
      headId={`${uid}-head`}
      hintId={`${uid}-hint`}
      focusHead={focusHead}
      onFocused={onFocused}
      lead={<PointsInput value={group.points ?? 0} label={`Points for the row: ${name}`} onChange={(points) => onChange({ ...group, points })} />}
    >
      <ul className="fi-ss-set-rows">
        {statements.map((statement, i) => (
          <StatementRow
            key={statement.id}
            statement={statement}
            words={words}
            points={false}
            focus={statement.id === focus || statement.id === added}
            onChange={(next) => setStatement(i, next)}
            onRemove={() => onChange({ ...group, members: statements.filter((_, j) => j !== i) })}
          />
        ))}
      </ul>
      <Foot words={words} name={name} onAdd={add} onRemove={onRemove} />
    </Frame>
  );
}
