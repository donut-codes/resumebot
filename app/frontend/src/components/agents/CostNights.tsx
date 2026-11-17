import { api } from "../../api/client";
import type { CostNight } from "../../api/types/agents";
import { useRefreshKey } from "../../lib/appContext";
import { useResource } from "../../lib/useResource";
import { Block } from "../focus/parts";

const LABELS: Record<string, string> = {
  analyst: "Researcher",
  browser_reader: "Browser reader",
  writer: "Writer",
  reader: "Reader",
  employer: "Employer research",
  titles: "Gate facts and role tags",
  parser: "Reading postings and resumes",
  scout: "Search",
  pathfinder: "Pathfinder",
  site: "Site setup",
  other: "Other",
};

function dollars(n: number): string {
  return `$${n.toFixed(2)}`;
}

/** A small cost per unit: cents when under a dollar, three places under a cent. */
function each(n: number): string {
  return n < 0.01 ? `$${n.toFixed(3)}` : dollars(n);
}

function dayWords(night: string): string {
  return new Date(`${night}T12:00:00`).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

/** The nightly cost benchmark, one quiet section on the Agents page: what last night's Claude sessions cost at API prices,
 * by kind of work and per unit it produced, and the last seven nights' totals. It is the number to set the budget by. Shown
 * only once a night has been worked out. */
export default function CostNights() {
  const refreshKey = useRefreshKey();
  const { data: nights } = useResource(() => api.listCostNights(), [refreshKey]);
  if (!nights || nights.length === 0) return null;
  const last: CostNight = nights[0];
  const kinds = Object.entries(last.kinds).sort((a, b) => b[1].usd - a[1].usd);
  const top = Math.max(...nights.map((n) => n.total.usd), 0.01);
  const ready = last.roles.usd_per_ready;
  return (
    <Block id="agents-cost" title="Night cost" count={dollars(last.total.usd)} hint={`${dayWords(last.night)}, at API prices.`}>
      <div className="agent-cost">
        <ul className="agent-cost-kinds" aria-label="Last night's cost by kind of work">
          {kinds.map(([kind, k]) => (
            <li key={kind} className="agent-cost-kind">
              <span className="agent-cost-name">{LABELS[kind] ?? kind}</span>
              <span className="agent-cost-usd">{dollars(k.usd)}</span>
              <span className="agent-cost-unit fi-note">
                {k.per_unit_usd != null && k.unit ? `${each(k.per_unit_usd)} per ${k.unit}` : `${k.sessions} sessions`}
              </span>
            </li>
          ))}
        </ul>
        <p className="agent-cost-roles fi-note">
          {last.roles.ready > 0 && ready != null
            ? `${last.roles.ready} roles reached Ready to Launch: ${dollars(ready)} each, all work counted. `
            : "No role reached Ready to Launch. "}
          {last.roles.gated_roles > 0 && `${dollars(last.roles.gated_usd)} went on ${last.roles.gated_roles} roles that were later gated out.`}
        </p>
        {nights.length > 1 && (
          <ol className="agent-cost-trend" aria-label="The last nights' cost">
            {[...nights].reverse().map((n) => (
              <li key={n.night} className="agent-cost-night" title={`${dayWords(n.night)}: ${dollars(n.total.usd)}`}>
                <span className="agent-cost-bar" style={{ height: `${Math.max(4, Math.round((n.total.usd / top) * 100))}%` }} />
                <span className="agent-cost-night-usd">{dollars(n.total.usd)}</span>
                <span className="agent-cost-night-day fi-note">{n.night.slice(5)}</span>
              </li>
            ))}
          </ol>
        )}
      </div>
    </Block>
  );
}
