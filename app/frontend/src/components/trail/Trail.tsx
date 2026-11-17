import { useRef, useState } from "react";
import { useResource } from "../../lib/useResource";
import ErrorBanner from "../primitives/ErrorBanner";
import Loading from "../primitives/Loading";
import type { TrailEntry, TrailFilter, TrailMark, TrailPage } from "../../api/types/agents";
import { dateInWords, localDay, timeInWords } from "../../lib/dates";

export interface TrailLoadOptions {
  filter: TrailFilter;
  before?: string;
  limit: number;
}

interface Props {
  /** Fetches one page of the trail for whatever scope this instance shows. */
  load: (opts: TrailLoadOptions) => Promise<TrailPage>;
  /** Changes when the scope or the data underneath changed (an agent, a refresh), to reload from the top. */
  loadKey: string;
  /** Show the "Everything" / "Needs a look" switch. */
  filters?: boolean;
  /** How many raw events to load at a time. */
  limit?: number;
  /** Offer "Older" when there is more. */
  paged?: boolean;
  /** What to say when there is nothing. */
  empty?: string;
  /** Accessible name of the filter group. */
  filterLabel?: string;
  /** Make a line's search or role open that thing. Without them it is plain text. */
  onOpenSearch?: (id: string) => void;
  onOpenRole?: (id: string) => void;
  /** A line for a search an agent was handed gets "What was sent": the instructions that search carried. */
  onOpenSent?: (runId: string) => void;
}

/** Where the lines of a trail can lead. */
type Openers = Pick<Props, "onOpenSearch" | "onOpenRole" | "onOpenSent">;

/** The one link a line carries: to its search or its role. Nothing when it can't open. */
function lineLink(entry: TrailEntry, o: Openers): { label: string; open: () => void } | null {
  const link = entry.link;
  if (link?.kind === "search_run" && o.onOpenSearch) return { label: "See the search", open: () => o.onOpenSearch!(link.id) };
  if (link?.kind === "opportunity" && o.onOpenRole) return { label: "See the role", open: () => o.onOpenRole!(link.id) };
  return null;
}

const FILTERS: { key: TrailFilter; label: string }[] = [
  { key: "all", label: "Everything" },
  { key: "needs_look", label: "Needs a look" },
];

/** A small drawn mark: a check for "matched the plan", a detour for "didn't". Shape, not only colour,
 *  carries the meaning; the label is there for screen readers and on hover. Neutral messages get none. */
function Mark({ mark }: { mark: TrailMark }) {
  if (mark === "neutral") return null;
  const label = mark === "on" ? "Matched the plan" : "Didn't match the plan";
  return (
    <span className={`trail-mark trail-mark-${mark}`} title={label}>
      <svg viewBox="0 0 16 16" width="14" height="14" role="img" aria-label={label}>
        {mark === "on" ? <path d="M3.5 8.5l3 3 6-7" /> : <path d="M3 13V8.5a3 3 0 013-3h5.5M9 2.5l3 3-3 3" />}
      </svg>
    </span>
  );
}

/** The one "Show more": the agent's own words, its evidence and its steps, all in one place. */
function DetailsBody({ entry }: { entry: TrailEntry }) {
  const d = entry.details;
  if (!d) return null;
  return (
    <div className="trail-details">
      {d.words && <p className="trail-words">&ldquo;{d.words}&rdquo;</p>}
      {d.evidence && (
        <div>
          <div className="trail-label">{d.words ? "Evidence" : "What it said"}</div>
          <p className="trail-evidence">{d.evidence}</p>
        </div>
      )}
      {d.steps.length > 0 && (
        <div>
          <div className="trail-label">Steps</div>
          <ul className="trail-steps">
            {d.steps.map((s, i) => (
              <li key={`${s.at}-${i}`}>{s.text}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

/** One message: its sentence, then "Show more" (or the list a collapsed line opens into) under it.
 *  `time` is shown on the line only when the message is not the first of its speaker's group, whose
 *  header already carries it. */
function Message({ entry, time, openers }: { entry: TrailEntry; time: boolean; openers: Openers }) {
  const [open, setOpen] = useState(false);
  const isGroup = !!entry.entries && entry.entries.length > 0;
  const hasDetails = !isGroup && !!entry.details;
  const link = lineLink(entry, openers);
  return (
    <li className={`trail-msg${entry.tier === "routine" ? " trail-msg-routine" : ""}`}>
      <div className="trail-line">
        <span className={`trail-sentence${entry.tier === "needs_you" ? " trail-sentence-strong" : ""}`}>
          {entry.sentence}
          <Mark mark={entry.mark} />
        </span>
        {time && <time className="trail-time trail-time-inline" dateTime={entry.at}>{timeInWords(entry.at)}</time>}
      </div>
      {(isGroup || hasDetails) && (
        <button
          type="button"
          className="fi-link trail-more"
          aria-expanded={open}
          aria-label={`${isGroup ? (open ? "Hide the list" : "Show the list") : "Show more"}: ${entry.sentence}`}
          onClick={() => setOpen((v) => !v)}
        >
          {isGroup ? (open ? "Hide the list" : "Show the list") : "Show more"}
        </button>
      )}
      {link && (isGroup || hasDetails) && <span className="fi-note" aria-hidden="true"> · </span>}
      {link && (
        <button type="button" className="fi-link trail-more" onClick={link.open}>
          {link.label}
        </button>
      )}
      {entry.sent_task_id && openers.onOpenSent && (
        <>
          {(link || isGroup || hasDetails) && <span className="fi-note" aria-hidden="true"> · </span>}
          <button type="button" className="fi-link trail-more" onClick={() => openers.onOpenSent!(entry.sent_task_id!)}>
            What was sent
          </button>
        </>
      )}
      {open && hasDetails && <DetailsBody entry={entry} />}
      {open && isGroup && (
        <ul className="trail-msgs trail-nested">
          {entry.entries!.map((child) => (
            <Message key={child.id} entry={child} time openers={openers} />
          ))}
        </ul>
      )}
    </li>
  );
}

/** A run of consecutive messages from one speaker on one day, under one name. */
interface Speech {
  key: string;
  who: string;
  entries: TrailEntry[];
}

/** One stretch of the chat: a day's divider, then that day's speeches, newest first like the list itself. */
interface Day {
  day: string;
  label: string;
  speeches: Speech[];
}

/** Newest-first entries as chat: a divider each time the day changes, and consecutive messages by the
 *  same speaker under one name. */
function chatOf(items: TrailEntry[]): Day[] {
  const days: Day[] = [];
  for (const entry of items) {
    const day = localDay(entry.at);
    let current = days[days.length - 1];
    if (!current || current.day !== day) {
      current = { day, label: dateInWords(entry.at), speeches: [] };
      days.push(current);
    }
    const last = current.speeches[current.speeches.length - 1];
    if (last && last.who === entry.who) {
      last.entries.push(entry);
    } else {
      current.speeches.push({ key: entry.id, who: entry.who, entries: [entry] });
    }
  }
  return days;
}

function SpeechView({ speech, openers }: { speech: Speech; openers: Openers }) {
  const first = speech.entries[0];
  return (
    <li className="trail-speech">
      <div className="trail-head">
        <strong className="trail-who">{speech.who}</strong>
        <time className="trail-time" dateTime={first.at}>{timeInWords(first.at)}</time>
      </div>
      <ul className="trail-msgs">
        {speech.entries.map((entry, i) => (
          <Message key={entry.id} entry={entry} time={i > 0} openers={openers} />
        ))}
      </ul>
    </li>
  );
}

function Chat({ items, openers }: { items: TrailEntry[]; openers: Openers }) {
  return (
    <ul className="trail-list">
      {chatOf(items).map((day) => (
        <li key={day.day} className="trail-day-block">
          <div className="trail-day">{day.label}</div>
          <ul className="trail-speeches">
            {day.speeches.map((speech) => (
              <SpeechView key={speech.key} speech={speech} openers={openers} />
            ))}
          </ul>
        </li>
      ))}
    </ul>
  );
}

/**
 * The one trail: what happened, newest first, as plain sentences with who did it and whether it matched the
 * plan. Used for an agent's activity (and later a task's). Each line has at most one "Details"; a run of the same
 * kind of event collapses to one line that opens into its list.
 */
export default function Trail({
  load, loadKey, filters = false, limit = 20, paged = true,
  empty = "Nothing yet.", filterLabel = "Filter what happened",
  onOpenSearch, onOpenRole, onOpenSent,
}: Props) {
  const openers: Openers = { onOpenSearch, onOpenRole, onOpenSent };
  const [filter, setFilter] = useState<TrailFilter>("all");
  const [items, setItems] = useState<TrailEntry[] | null>(null);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [olderError, setOlderError] = useState<string | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;
  // Which first page the list is made of: a new first page (another filter, a fresh load) makes an "Older" still on its
  // way stale, so it does not add the old filter's rows to the new list.
  const generation = useRef(0);

  // The first page; "Older" adds to it from here, so the list is the screen's own once this fills it.
  const { error } = useResource(
    () => {
      generation.current++;
      return loadRef.current({ filter, limit });
    },
    [filter, limit, loadKey],
    {
      fallback: "Couldn't load this.",
      onData: (page) => {
        setItems(page.items ?? []);
        setNextBefore(page.next_before ?? null);
        setOlderError(null);
        setLoadingMore(false);
      },
    },
  );

  function older() {
    if (!nextBefore) return;
    const mine = generation.current;
    setLoadingMore(true);
    setOlderError(null);
    loadRef
      .current({ filter, limit, before: nextBefore })
      .then((page) => {
        if (generation.current !== mine) return;
        setItems((prev) => [...(prev ?? []), ...page.items]);
        setNextBefore(page.next_before);
      })
      // The button stays, so it can be tried again.
      .catch(() => {
        if (generation.current === mine) setOlderError("Couldn't load the older ones.");
      })
      .finally(() => {
        if (generation.current === mine) setLoadingMore(false);
      });
  }

  return (
    <div className="trail">
      {filters && (
        <div className="fi-pills trail-filters" role="group" aria-label={filterLabel}>
          {FILTERS.map((f) => (
            <button
              type="button"
              key={f.key}
              aria-pressed={filter === f.key}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
            </button>
          ))}
        </div>
      )}
      {error ? (
        <ErrorBanner>{error}</ErrorBanner>
      ) : items === null ? (
        <Loading as="p">Loading&hellip;</Loading>
      ) : items.length === 0 ? (
        <p className="fi-note trail-empty">
          {filter === "needs_look" ? "Nothing needs a look." : filter === "highlights" ? "Nothing to highlight yet." : empty}
        </p>
      ) : (
        <>
          <Chat items={items} openers={openers} />
          {paged && nextBefore && (
            <button type="button" className="fi-btn trail-older" disabled={loadingMore} onClick={older}>
              {loadingMore ? "Loading…" : "Older"}
            </button>
          )}
          {olderError && <ErrorBanner as="p">{olderError}</ErrorBanner>}
        </>
      )}
    </div>
  );
}
