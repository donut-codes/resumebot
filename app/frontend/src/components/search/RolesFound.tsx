import { useEffect, useState } from "react";
import type { SiteRole, SiteRolesPage } from "../../api/types/where";
import { shortDateInWords } from "../../lib/dates";
import { roleAddress } from "../../lib/route";
import { stageFor } from "../../lib/stages";

/** How many roles a page of them holds, and how many more each Show more adds. */
export const ROLES_PAGE = 20;

const CLOSED = new Set(["gated_out", "rejected", "gone_quiet", "withdrew"]);

/** The roles a place brought in that reached Found, newest first, a page at a time: each its title (a link to it in the
 * Pipeline), its stage, its fit and the day it was found. */
export default function RolesFound({ load, total }: { load: (offset: number, limit: number) => Promise<SiteRolesPage>; total?: number }) {
  const [items, setItems] = useState<SiteRole[] | null>(null);
  const [count, setCount] = useState(total ?? 0);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    load(0, ROLES_PAGE).then(
      (page) => {
        if (!live) return;
        setItems(page.items);
        setCount(page.total);
      },
      () => live && setFailed(true),
    );
    return () => {
      live = false;
    };
  }, [load]);
  const more = async () => {
    setBusy(true);
    try {
      const page = await load(items?.length ?? 0, ROLES_PAGE);
      setItems((had) => [...(had ?? []), ...page.items.filter((r) => !(had ?? []).some((h) => h.id === r.id))]);
      setCount(page.total);
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };
  if (failed && !items) return <p className="fi-alert">Couldn&rsquo;t load the roles.</p>;
  if (!items) return <p className="fi-ss-quiet">Loading…</p>;
  if (items.length === 0) return <p className="fi-ss-quiet">No roles found here yet.</p>;
  return (
    <>
      <ul className="fi-ss-lines">
        {items.map((r) => {
          const stage = stageFor(r.status);
          const cls = r.status === "ready_to_launch" ? " is-yours" : CLOSED.has(r.status) ? " is-closed" : "";
          const bits = [r.company, r.fit_pct != null ? `Fit ${r.fit_pct}%` : null, r.found_at ? `Found ${shortDateInWords(r.found_at)}` : null].filter(Boolean);
          return (
            <li key={r.id} className="fi-ss-role">
              <div>
                <a className="fi-ss-role-title" href={roleAddress(r.id)}>
                  {r.title}
                </a>
                <div className="fi-ss-quiet">{bits.join(" · ")}</div>
              </div>
              <span className={`fi-badge fi-badge-stage${cls}`}>{stage?.label ?? r.status.replace(/_/g, " ")}</span>
            </li>
          );
        })}
      </ul>
      {count > items.length && (
        <button type="button" className="fi-btn fi-btn-quiet fi-ss-more" disabled={busy} onClick={() => void more()}>
          {busy ? "Loading…" : `Show more (${count - items.length} more)`}
        </button>
      )}
    </>
  );
}
