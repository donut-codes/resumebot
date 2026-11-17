// The parts every Focus screen is built from (the screens rebuild spec, "Shared parts"): the rail and its entries,
// the list's head and rows, the pane, its head, sheets and facts strip, and the controls (switch, field, saved line,
// menu, slider). Each draws only, from props, with the Focus tokens (styles/focus/parts.css); none reads a store or the
// API, so a screen wires them and a later look replaces them.

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import GrowingText from "../primitives/GrowingText";
import CountRoll from "./CountRoll";
import { BackIcon, ChevronIcon, FindIcon } from "./icons";

// --- the rail ----------------------------------------------------------------------------------------------------------

/** The screen's parts, down the left (one sideways strip on a phone). */
export function Rail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <nav className="fi-rail" aria-label={label}>
      {children}
    </nav>
  );
}

/** A group of entries, with a small head when it has a name. */
export function RailGroup({ label, children }: { label?: string; children: ReactNode }) {
  return (
    <div className="fi-rail-group">
      {label && <div className="fi-rail-head">{label}</div>}
      {children}
    </div>
  );
}

interface RailEntryProps {
  label: ReactNode;
  /** Its count; none shows nothing. */
  count?: number | null;
  current: boolean;
  onPick: () => void;
  /** It waits on the owner: its count is the sun pill. */
  yours?: boolean;
  small?: boolean;
  /** One of the parts of the entry above it: set in from the edge. */
  nested?: boolean;
  dim?: boolean;
  /** A note after the name (a search's matches in it). */
  note?: ReactNode;
  /** The part has an address of its own: the entry is a link to it (so it can be copied and opened in a new window), and
   * a plain click is `onPick`. Without it the entry is a button. */
  href?: string;
}

/** One part of the screen, with its live count. */
export function RailEntry({ label, count, current, onPick, yours, small, nested, dim, note, href }: RailEntryProps) {
  const cls = ["fi-stage", yours && "is-yours", small && "is-small", nested && "is-nested", dim && "is-dim"].filter(Boolean).join(" ");
  const inside = (
    <>
      <span className="fi-stage-name">
        {label}
        {note}
      </span>
      {count != null ? <CountRoll className="fi-stage-n" value={count} /> : <span />}
    </>
  );
  if (href !== undefined) {
    return (
      <a
        href={href}
        className={cls}
        aria-current={current ? "page" : undefined}
        onClick={(e) => {
          // A click that means something else (a new window, a new tab) is left to the browser.
          if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
          e.preventDefault();
          onPick();
        }}
      >
        {inside}
      </a>
    );
  }
  return (
    <button type="button" className={cls} aria-pressed={current} onClick={onPick}>
      {inside}
    </button>
  );
}

// --- the list ----------------------------------------------------------------------------------------------------------

interface ListHeadProps {
  /** The find field, when the list has one. */
  find?: { value: string; onChange: (q: string) => void; placeholder: string; label: string };
  title: ReactNode;
  /** The list's one primary action, beside its title. */
  action?: ReactNode;
  children?: ReactNode;
}

/** The list's head: the find field, the part's name as the title and its one action, and anything it must say. */
export function ListHead({ find, title, action, children }: ListHeadProps) {
  return (
    <div className="fi-list-head">
      {find && (
        <label className="fi-find">
          <span className="fi-vh">{find.label}</span>
          <FindIcon className="fi-find-icon" />
          <input type="search" value={find.value} placeholder={find.placeholder} autoComplete="off" onChange={(e) => find.onChange(e.target.value)} />
        </label>
      )}
      <div className="fi-list-title">
        <h1>{title}</h1>
        {action}
      </div>
      {children}
    </div>
  );
}

interface RowProps {
  title: ReactNode;
  meta?: ReactNode;
  /** The chip at the row's start (a count, an icon). */
  lead?: ReactNode;
  /** What sits at the row's end (its age, a switch). A control here sits above the row's reach. */
  end?: ReactNode;
  current?: boolean;
  /** Read already: its title is quiet. */
  quiet?: boolean;
  leaving?: boolean;
  onOpen: () => void;
}

/** One item in a list: the whole row opens it; at most one control of its own sits at its end. */
export function Row({ title, meta, lead, end, current, quiet, leaving, onOpen }: RowProps) {
  const cls = ["fi-item", quiet && "is-quiet", leaving && "is-leaving"].filter(Boolean).join(" ");
  return (
    <li className={cls} aria-current={current ? "true" : undefined}>
      {lead ?? <span />}
      <div className="fi-row-text">
        <button type="button" className="fi-row-title" onClick={onOpen}>
          {title}
        </button>
        {meta && <div className="fi-row-meta">{meta}</div>}
      </div>
      <div className="fi-item-end">{end}</div>
    </li>
  );
}

// --- the pane ----------------------------------------------------------------------------------------------------------

interface PaneProps {
  /** Changes when another item opens, so the pane arrives again. */
  openKey: string;
  label: string;
  onBack: () => void;
  backLabel: string;
  children: ReactNode;
}

/** The open item, on the ground, at the pane's measure; under 1100px it lays over the list with Back. */
export function Pane({ openKey, label, onBack, backLabel, children }: PaneProps) {
  return (
    <section className="fi-pane" aria-label={label}>
      <div className="fi-pane-in" key={openKey}>
        <button type="button" className="fi-back" onClick={onBack}>
          <BackIcon className="fi-btn-icon" />
          {backLabel}
        </button>
        {children}
      </div>
    </section>
  );
}

/** The pane's head: the name, its meta line, and anything that sits beside the name. */
export function PaneHead({ title, meta, aside }: { title: ReactNode; meta?: ReactNode[]; aside?: ReactNode }) {
  const shown = (meta ?? []).filter(Boolean);
  return (
    <div className="fi-head">
      <div>
        <h2>{title}</h2>
        {shown.length > 0 && (
          <p className="fi-head-meta">
            {shown.map((m, i) => (
              <span key={i}>{m}</span>
            ))}
          </p>
        )}
      </div>
      {aside && <div className="fi-head-aside">{aside}</div>}
    </div>
  );
}

/** A white sheet on the pane, with its title and count when it has them. */
export function Sheet({ title, count, children }: { title?: ReactNode; count?: ReactNode; children: ReactNode }) {
  return (
    <section className="fi-sec">
      {title && (
        <h3>
          {title}
          {count != null && <span>{count}</span>}
        </h3>
      )}
      <div className="fi-sheet">{children}</div>
    </section>
  );
}

export interface Fact {
  label: ReactNode;
  value: ReactNode;
  sub?: ReactNode;
}

/** The facts an item rests on, side by side. */
export function FactStrip({ facts }: { facts: Fact[] }) {
  if (facts.length === 0) return null;
  return (
    <dl className="fi-facts">
      {facts.map((f, i) => (
        <div key={i}>
          <dt>{f.label}</dt>
          <dd>{f.value}</dd>
          {f.sub && <dd className="fi-facts-sub">{f.sub}</dd>}
        </div>
      ))}
    </dl>
  );
}

/** The state inks a block's heading can carry as a dot (the words that say the state are always in the block). */
export type BlockInk = "green" | "oxblood" | "saffron" | "graphite" | "cobalt";

interface BlockProps {
  /** The block's id: the heading is `${id}-head`, and the block names itself by it. */
  id: string;
  title: ReactNode;
  /** A figure after the name, in meta. */
  count?: ReactNode;
  /** At the right end of the head: buttons and links. */
  aside?: ReactNode;
  /** One plain line under the head. */
  hint?: ReactNode;
  /** 2 for a screen's blocks, 3 for a block inside one. */
  level?: 2 | 3;
  /** The state the block is about, as a small dot before its name (the words that say it travel in the block). */
  ink?: BlockInk;
  /** The body holds a list of its own (rows divided by hairlines), so it takes no padding. */
  flush?: boolean;
  className?: string;
  children?: ReactNode;
}

/** A named white sheet on the pane: its heading with a figure and tools at the right, a line of words, and its body. */
export function Block({ id, title, count, aside, hint, level = 2, ink, flush = false, className = "", children }: BlockProps) {
  const Tag = level === 2 ? "h2" : "h3";
  return (
    <section className={`fi-block ${className}`.trim()} id={id} aria-labelledby={`${id}-head`} data-ink={ink}>
      <div className="fi-block-head">
        <Tag id={`${id}-head`} className="fi-block-title">
          <span>{title}</span>
          {count !== undefined && count !== null && (
            <>
              {" "}
              <span className="fi-block-count">{count}</span>
            </>
          )}
        </Tag>
        {aside && <div className="fi-block-aside">{aside}</div>}
      </div>
      {hint && <p className="fi-block-hint">{hint}</p>}
      <div className={flush ? "fi-block-body is-flush" : "fi-block-body"}>{children}</div>
    </section>
  );
}

export interface PillTab {
  key: string;
  label: ReactNode;
  current: boolean;
  /** The part has an address of its own: the tab is a link to it, and a plain click is `onPick`. */
  href?: string;
  onPick: () => void;
}

/** A row of choices on a pane, as pills on a track: links when each has an address, else buttons that swap in place. `sub` is
 * the lighter row under another. */
export function PillRow({ label, tabs, sub = false }: { label: string; tabs: PillTab[]; sub?: boolean }) {
  return (
    <nav className={`fi-pills${sub ? " is-sub" : ""}`} aria-label={label}>
      {tabs.map((t) =>
        t.href !== undefined ? (
          <a
            key={t.key}
            href={t.href}
            aria-current={t.current ? "page" : undefined}
            onClick={(e) => {
              if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
              e.preventDefault();
              t.onPick();
            }}
          >
            {t.label}
          </a>
        ) : (
          <button key={t.key} type="button" aria-pressed={t.current} onClick={t.onPick}>
            {t.label}
          </button>
        ),
      )}
    </nav>
  );
}

// --- controls ----------------------------------------------------------------------------------------------------------

interface SwitchProps {
  checked: boolean;
  onChange: (on: boolean) => void;
  label: string;
  disabled?: boolean;
  /** Words shown beside the track, inside the switch (so they switch it too): an actions row's labelled switch. */
  shown?: ReactNode;
}

/** A switch, 44px to the touch, that says its own name to a screen reader. */
export function Switch({ checked, onChange, label, disabled, shown }: SwitchProps) {
  return (
    <button type="button" role="switch" className={`fi-switch${shown ? " is-labelled" : ""}`} aria-checked={checked} aria-label={label} disabled={disabled} onClick={() => onChange(!checked)}>
      <span className="fi-switch-track" aria-hidden="true" />
      {shown && <span className="fi-switch-words">{shown}</span>}
    </button>
  );
}

interface FieldProps {
  label: ReactNode;
  value: string;
  /** Called when the field loses focus with a value that changed. */
  onSave: (value: string) => void;
  /** Long text: a box that grows with what is written. */
  long?: boolean;
  type?: "text" | "number";
  placeholder?: string;
}

/** A field that saves itself when it loses focus. It takes a new value from outside unless the owner is typing in it.
 * There is no cap on what is written. */
export function Field({ label, value, onSave, long, type = "text", placeholder }: FieldProps) {
  const id = useId();
  const [draft, setDraft] = useState(value);
  const [editing, setEditing] = useState(false);
  const [seen, setSeen] = useState(value);
  if (value !== seen) {
    setSeen(value);
    if (!editing) setDraft(value);
  }
  const commit = () => {
    setEditing(false);
    if (draft !== value) onSave(draft);
  };
  return (
    <div className="fi-field">
      <label htmlFor={id}>{label}</label>
      {long ? (
        <GrowingText
          id={id}
          className="fi-input"
          minRows={2}
          value={draft}
          placeholder={placeholder}
          onFocus={() => setEditing(true)}
          onText={setDraft}
          onBlur={commit}
        />
      ) : (
        <input
          id={id}
          className="fi-input"
          type={type}
          value={draft}
          placeholder={placeholder}
          onFocus={() => setEditing(true)}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
        />
      )}
    </div>
  );
}

interface SavedLineProps {
  state: "idle" | "saving" | "saved" | "failed";
  onUndo?: () => void;
  /** Why it did not go, in words (in place of "Not saved"). */
  error?: string | null;
  /** What follows on the line once saved (a way to say why). */
  children?: ReactNode;
}

/** The quiet line that says a change was saved, with Undo; or that it is on its way, or did not go. */
export function SavedLine({ state, onUndo, error, children }: SavedLineProps) {
  if (state === "idle") return null;
  return (
    <p className={`fi-saved${state === "failed" ? " is-failed" : ""}`} role="status">
      {state === "saving" ? "Saving…" : state === "saved" ? "Saved" : error || "Not saved"}
      {state === "saved" && onUndo && (
        <button type="button" className="fi-link" onClick={onUndo}>
          Undo
        </button>
      )}
      {state === "saved" && children}
    </p>
  );
}

export interface MenuItem {
  key: string;
  label: ReactNode;
}

interface MenuProps {
  /** The button's words. */
  label: ReactNode;
  /** What the open menu asks, at its top (and its name to a screen reader). */
  heading: string;
  items: MenuItem[];
  onPick: (key: string) => void;
  disabled?: boolean;
}

/** A button that opens a short menu under it; picking one closes it. A click away or Escape closes it too. */
export function Menu({ label, heading, items, onPick, disabled }: MenuProps) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => !box.current?.contains(e.target as Node) && setOpen(false);
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);
  return (
    <div className="fi-close" ref={box}>
      <button type="button" className="fi-btn" aria-expanded={open} aria-haspopup="menu" disabled={disabled} onClick={() => setOpen((o) => !o)}>
        {label}
        <ChevronIcon className="fi-btn-icon" />
      </button>
      {open && (
        <div className="fi-close-menu" role="menu" aria-label={heading}>
          <div className="fi-close-head">{heading}</div>
          {items.map((item) => (
            <button
              key={item.key}
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                onPick(item.key);
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

interface SliderProps {
  label: string;
  value: number;
  min?: number;
  max?: number;
  step?: number;
  /** The words at each end, or what they say for the value the slider shows as it moves. */
  ends?: [ReactNode, ReactNode] | ((value: number) => [ReactNode, ReactNode]);
  /** What a screen reader says of the value shown. */
  valueText?: (value: number) => string;
  /** Called once, when the owner lets go (a drag, or a key), with the value they left it at. */
  onCommit: (value: number) => void;
}

/** A slider that says its value as it moves and saves only when let go. */
export function Slider({ label, value, min = 0, max = 100, step = 1, ends, valueText, onCommit }: SliderProps) {
  const [shown, setShown] = useState(value);
  const [seen, setSeen] = useState(value);
  if (value !== seen) {
    setSeen(value);
    setShown(value);
  }
  const letGo = () => {
    if (shown !== value) onCommit(shown);
  };
  const words = typeof ends === "function" ? ends(shown) : ends;
  return (
    <div className="fi-slider">
      {words && (
        <div className="fi-slider-ends" aria-hidden="true">
          <span>{words[0]}</span>
          <span>{words[1]}</span>
        </div>
      )}
      <input
        type="range"
        aria-label={label}
        min={min}
        max={max}
        step={step}
        value={shown}
        aria-valuetext={valueText?.(shown)}
        onChange={(e) => setShown(Number(e.target.value))}
        onPointerUp={letGo}
        onKeyUp={letGo}
        onBlur={letGo}
      />
    </div>
  );
}
