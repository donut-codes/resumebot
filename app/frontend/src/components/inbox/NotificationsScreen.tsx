// Notifications (the screens rebuild spec, screen 1): answer what the app cannot decide, and see what it told you.
// The rail holds the four parts (Needs you, Notifications, What agents did, Past decisions), the list the part's items,
// and the pane the open one. Everything comes from the inbox store, which follows the live stream; nothing here polls
// or calls the API. Answering folds the row out and opens the next one.

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Task, TaskOption } from "../../api/types/inbox";
import { useRefreshKey, useRoute } from "../../lib/appContext";
import { timeAgoInWords } from "../../lib/dates";
import { routeFor, type InboxSection } from "../../lib/route";
import { NARROW, useMedia } from "../../lib/useMedia";
import { asksToday, needsYou, useInbox, useInboxStore } from "../../state/inboxStore";
import GrowingText from "../primitives/GrowingText";
import { AgentIcon, AskIcon, BellIcon, CheckIcon } from "../focus/icons";
import { FactStrip, ListHead, Pane, PaneHead, Rail, RailEntry, RailGroup, Row } from "../focus/parts";
import { agentItems, linkTo, matching, needsItems, noteItems, ordered, PARTS, pastItems, putOffWords, shortAge, type Item } from "./model";

/** How long a folded row takes to leave (the Pipeline's roll). */
const FOLD_MS = 420;

const EMPTY: Record<InboxSection, string> = {
  needs: "Nothing needs you right now.",
  notifications: "Nothing to tell you yet.",
  activity: "The agents have nothing open or finished lately.",
  decisions: "Nothing decided yet.",
};

function Lead({ item }: { item: Item }) {
  const Icon = item.kind === "task" ? AskIcon : item.kind === "note" ? BellIcon : item.kind === "agent" ? AgentIcon : CheckIcon;
  return (
    <span className={`fi-kind${item.kind === "task" && !item.quiet ? " is-yours" : ""}`} aria-hidden="true">
      <Icon className="fi-kind-icon" />
    </span>
  );
}

function LinkLine({ item }: { item: Item }) {
  const to = linkTo(item.task?.link ?? item.note?.link ?? item.record?.link ?? null, item.task?.kind);
  if (!to) return null;
  return (
    <p className="fi-body">
      <a href={to.href}>{to.words}</a>
    </p>
  );
}

/** A decision's or a task's answers: the options (the suggested one first) or Done, then Not now and Skip. An option
 * that asks for the owner's words opens a box for them first. */
function Answers({ task, onLeave }: { task: Task; onLeave: () => void }) {
  const store = useInboxStore();
  const today = useInbox((s) => s.today);
  const busy = useInbox((s) => !!s.busy[task.id]);
  const refused = useInbox((s) => (s.refused?.taskId === task.id ? s.refused.words : null));
  const [asking, setAsking] = useState<TaskOption | null>(null);
  const [words, setWords] = useState("");
  const options = task.options ?? [];
  const asksNow = asksToday(task, today);

  if (asking) {
    const close = () => {
      setAsking(null);
      setWords("");
      store.clearRefused();
    };
    return (
      <form
        className="fi-ask"
        onSubmit={(e) => {
          e.preventDefault();
          // The words may be refused (too long to send an agent): the row leaves only when they are taken.
          void store.answer(task, asking, words.trim() || undefined).then((ok) => ok && onLeave());
        }}
        ref={(form) => form?.querySelector("textarea")?.focus()}
      >
        <label htmlFor={`ask-${task.id}`}>{asking.note}</label>
        <GrowingText
          id={`ask-${task.id}`}
          minRows={3}
          onKeyDown={(e) => e.key === "Escape" && close()}
          value={words}
          onText={(t) => {
            setWords(t);
            if (refused) store.clearRefused();
          }}
        />
        {refused && (
          <p className="fi-alert" role="alert">
            {refused}
          </p>
        )}
        <div className="fi-actions">
          <button type="submit" className="fi-btn fi-btn-go" disabled={busy}>
            {asking.label}
          </button>
          <button type="button" className="fi-btn fi-btn-quiet" disabled={busy} onClick={close}>
            Cancel
          </button>
        </div>
      </form>
    );
  }

  return (
    <div className="fi-actions" role="group" aria-label="Your answer">
      {options.length > 0 ? (
        ordered(options, task.recommended).map((o) => (
          <button
            key={o.id}
            type="button"
            className={`fi-btn${o.id === task.recommended ? " fi-btn-go" : ""}`}
            disabled={busy}
            title={o.description || undefined}
            onClick={() => {
              if (o.note) return setAsking(o);
              onLeave();
              void store.answer(task, o);
            }}
          >
            {o.label}
          </button>
        ))
      ) : (
        <button type="button" className="fi-btn fi-btn-go" disabled={busy} onClick={() => {
            onLeave();
            void store.done(task);
          }}>
          Done
        </button>
      )}
      <span className="fi-actions-gap" />
      {asksNow && (
        <button type="button" className="fi-btn fi-btn-quiet" disabled={busy} onClick={() => {
            onLeave();
            void store.notNow(task);
          }}>
          Not now
        </button>
      )}
      <button type="button" className="fi-btn fi-btn-quiet" disabled={busy} onClick={() => {
          onLeave();
          void store.skip(task);
        }}>
        Skip
      </button>
    </div>
  );
}

/** The open item: its head, its answers when it waits on the owner, its words and its link. */
function ItemPane({ item, today, onLeave }: { item: Item; today: string | null; onLeave: () => void }) {
  const { task, note, record } = item;
  let meta: ReactNode[] = item.meta;
  let body: string | null = null;
  if (item.kind === "task" && task) {
    meta = [...item.meta.filter((m) => m !== putOffWords(task, today)), putOffWords(task, today), `Asked ${timeAgoInWords(task.created_at)}`];
    body = task.detail;
  } else if (item.kind === "agent" && task) {
    meta = [...item.meta, timeAgoInWords(item.at)];
    body = task.detail;
  } else if (note) {
    meta = [...item.meta, timeAgoInWords(note.created_at)];
    body = note.body;
  } else if (record) {
    body = record.why;
  }
  return (
    <>
      <PaneHead title={item.title} meta={meta} />
      {task && <FactStrip facts={task.facts ?? []} />}
      {item.kind === "task" && task && <Answers task={task} onLeave={onLeave} />}
      {body && <p className="fi-body">{body}</p>}
      <LinkLine item={item} />
    </>
  );
}

export default function NotificationsScreen() {
  const store = useInboxStore();
  const { route, go } = useRoute();
  const narrow = useMedia(NARROW);
  const part = route.inboxSection;
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const [onItem, setOnItem] = useState(false);
  const [leaving, setLeaving] = useState<{ item: Item; at: number } | null>(null);
  const foldTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const status = useInbox((s) => s.status);
  const error = useInbox((s) => s.error);
  const today = useInbox((s) => s.today);
  const mine = useInbox((s) => s.mine);
  const theirs = useInbox((s) => s.theirs);
  const done = useInbox((s) => s.done);
  const notes = useInbox((s) => s.notifications);
  const unread = useInbox((s) => s.unread);
  const past = useInbox((s) => s.past);
  const notice = useInbox((s) => s.notice);
  const needs = useInbox(needsYou);

  useEffect(() => store.start(), [store]);
  // Clicking the tab you are on reads it again.
  const refreshKey = useRefreshKey();
  useEffect(() => {
    if (refreshKey > 0) void store.reload();
  }, [refreshKey, store]);
  useEffect(() => {
    if (part === "decisions" && store.getState().past.status === "idle") void store.loadPast();
  }, [part, store]);
  useEffect(() => () => {
    if (foldTimer.current) clearTimeout(foldTimer.current);
  }, []);

  const all = useMemo<Item[]>(() => {
    if (part === "needs") return needsItems(mine, today);
    if (part === "notifications") return noteItems(notes);
    if (part === "activity") return agentItems(theirs, done);
    return pastItems(past.items);
  }, [part, mine, today, notes, theirs, done, past.items]);
  const items = useMemo(() => matching(all, query), [all, query]);
  // A row folding out stays drawn where it was until its fold is done.
  const rows = useMemo(() => {
    if (!leaving || items.some((i) => i.id === leaving.item.id)) return items;
    return [...items.slice(0, leaving.at), leaving.item, ...items.slice(leaving.at)];
  }, [items, leaving]);

  const openId = items.some((i) => i.id === picked) ? picked : narrow ? null : (items[0]?.id ?? null);
  const open = items.find((i) => i.id === openId) ?? null;
  const label = PARTS.find((p) => p.key === part)?.label ?? "Notifications";

  const pickPart = (key: InboxSection) => {
    setQuery("");
    setPicked(null);
    setOnItem(false);
    go(routeFor("inbox", { inboxSection: key }));
  };
  const openItem = (item: Item) => {
    setPicked(item.id);
    setOnItem(true);
    if (item.note && !item.note.read_at) void store.read(item.note.id);
  };

  /** The open task is being answered: its row folds out and the next one opens (on a phone, the list comes back). */
  const leave = (item: Item) => {
    const at = items.findIndex((i) => i.id === item.id);
    const next = items[at + 1] ?? items[at - 1] ?? null;
    setPicked(next?.id ?? null);
    if (narrow) setOnItem(false);
    setLeaving({ item, at: Math.max(0, at) });
    if (foldTimer.current) clearTimeout(foldTimer.current);
    foldTimer.current = setTimeout(() => setLeaving(null), FOLD_MS);
  };

  if (status === "error" && all.length === 0) {
    return (
      <div className="fi">
        <p className="fi-alert" role="alert">
          {error}{" "}
          <button type="button" className="fi-link" onClick={() => void store.reload()}>
            Try again
          </button>
        </p>
      </div>
    );
  }

  const counts: Record<InboxSection, number | null> = {
    needs: needs || null,
    notifications: unread || null,
    activity: null,
    decisions: null,
  };
  const loading = part === "decisions" ? past.status === "loading" && past.items.length === 0 : status !== "ready" && all.length === 0;
  const showPane = !narrow || onItem;

  return (
    <div className={`fi${onItem ? " is-on-role" : ""}`}>
      <Rail label="Notifications">
        <RailGroup>
          {PARTS.slice(0, 2).map((p) => (
            <RailEntry key={p.key} label={p.label} count={counts[p.key]} yours={p.key === "needs"} current={part === p.key} onPick={() => pickPart(p.key)} />
          ))}
        </RailGroup>
        <RailGroup label="Kept for you">
          {PARTS.slice(2).map((p) => (
            <RailEntry key={p.key} label={p.label} count={counts[p.key]} current={part === p.key} onPick={() => pickPart(p.key)} />
          ))}
        </RailGroup>
      </Rail>
      <section className="fi-list" aria-label={label}>
        <ListHead
          find={{ value: query, onChange: setQuery, placeholder: "Find in notifications", label: "Find in notifications" }}
          title={label}
          action={
            part === "notifications" && unread > 0 ? (
              <button type="button" className="fi-btn fi-btn-quiet" onClick={() => void store.readAll()}>
                Mark all read
              </button>
            ) : null
          }
        >
          {notice && (
            <p className="fi-alert" role="alert">
              {notice}{" "}
              <button type="button" className="fi-link" onClick={store.dismissNotice}>
                Dismiss
              </button>
            </p>
          )}
        </ListHead>
        {loading ? (
          <p className="fi-empty">Loading…</p>
        ) : (
          <ul className="fi-rows">
            {rows.length === 0 && <li className="fi-empty">{query.trim() ? "Nothing matches." : EMPTY[part]}</li>}
            {rows.map((item) => (
              <Row
                key={item.id}
                title={item.title}
                meta={item.meta.map((m) => (
                  <span key={m}>{m}</span>
                ))}
                lead={<Lead item={item} />}
                end={
                  <span className="fi-item-age">
                    {shortAge(item.at)}
                    {item.kind === "note" && <span className={`fi-unread${item.quiet ? " is-off" : ""}`} aria-label={item.quiet ? undefined : "Not read yet"} role={item.quiet ? undefined : "img"} />}
                  </span>
                }
                current={item.id === openId}
                quiet={item.quiet}
                leaving={leaving?.item.id === item.id && !items.some((i) => i.id === item.id)}
                onOpen={() => openItem(item)}
              />
            ))}
          </ul>
        )}
        {part === "decisions" && past.next && (
          <button type="button" className="fi-btn fi-more" disabled={past.loadingMore} onClick={() => void store.loadEarlier()}>
            {past.loadingMore ? "Loading…" : "Show earlier"}
          </button>
        )}
      </section>
      {showPane && (
        <Pane openKey={open?.id ?? "none"} label={open ? open.title : label} backLabel={label} onBack={() => setOnItem(false)}>
          {open ? <ItemPane item={open} today={today} onLeave={() => leave(open)} /> : <p className="fi-pane-note">{items.length ? "Pick one to read it here." : ""}</p>}
        </Pane>
      )}
    </div>
  );
}
