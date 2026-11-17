import { safeHref } from "../../lib/links";
import PacketBar from "./PacketBar";
import Star from "./Star";
import type { RowView } from "./model";

interface Props {
  row: RowView;
  current: boolean;
  leaving: boolean;
  busy: boolean;
  /** The words searched, marked in the title and the employer. */
  mark: string;
  onOpen: (id: string) => void;
  onStar: (id: string) => void;
}

function Marked({ text, mark }: { text: string; mark: string }) {
  const words = mark.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return <>{text}</>;
  const lower = text.toLowerCase();
  for (const w of words) {
    const i = lower.indexOf(w);
    if (i >= 0) {
      return (
        <>
          {text.slice(0, i)}
          <mark>{text.slice(i, i + w.length)}</mark>
          {text.slice(i + w.length)}
        </>
      );
    }
  }
  return <>{text}</>;
}

/** One role in the list: its number, title, employer, the standard facts, what is wrong with it if anything is, and its
 * star. A new role's number has its first hours in it (docs/PRD.md 24.15), which its New tag says in points. */
export default function RoleRow({ row, current, leaving, busy, mark, onOpen, onStar }: Props) {
  const open = () => row.openable && onOpen(row.id);
  const href = row.openable ? undefined : safeHref(row.url);
  return (
    <li
      data-flip={row.id}
      className={`fi-row${leaving ? " is-leaving" : ""}${busy ? " is-busy" : ""}`}
      aria-current={current ? "true" : undefined}
    >
      <span
        className={`fi-fit${row.fit == null ? " is-none" : row.fit >= 70 ? " is-high" : ""}`}
        aria-label={row.fit == null ? "Not scored yet" : `Fit ${row.fit}`}
      >
        {row.fit == null ? "–" : row.fit}
      </span>
      <div className="fi-row-text">
        {row.stage && <span className={`fi-badge fi-badge-stage is-${row.stageTone}`}>{row.stage}</span>}
        {row.openable ? (
          <button type="button" className="fi-row-title" onClick={open}>
            <Marked text={row.title} mark={mark} />
          </button>
        ) : href ? (
          <a className="fi-row-title" href={href} target="_blank" rel="noreferrer">
            <Marked text={row.title} mark={mark} />
          </a>
        ) : (
          <span className="fi-row-title">
            <Marked text={row.title} mark={mark} />
          </span>
        )}
        {row.newTag && <span className="fi-badge fi-badge-new">{row.newTag}</span>}
        <div className="fi-row-meta">
          <span>
            <Marked text={row.company} mark={mark} />
          </span>
          {row.meta.map((m) => (
            <span key={m}>{m}</span>
          ))}
          {row.pay && <span className="fi-row-pay">{row.pay}</span>}
        </div>
        {row.progress ? (
          <div className="fi-row-next is-yours">
            <PacketBar progress={row.progress} words={row.next ?? undefined} />
          </div>
        ) : (
          row.next && <div className={`fi-row-next is-${row.nextTone}`}>{row.next}</div>
        )}
        {row.check && <div className="fi-row-check">{row.check}</div>}
      </div>
      {row.openable ? <Star on={row.favorite} title={row.title} busy={busy} onToggle={() => onStar(row.id)} /> : <span />}
    </li>
  );
}
