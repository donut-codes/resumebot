import type { AgentSummary } from "../../api/types/agents";
import BotMark, { type Outfit } from "./BotMark";

/** The disguises in the order the work goes: the scout finds, the analyst researches, the writer drafts. */
const ORDER: Outfit[] = ["scout", "analyst", "writer"];

/** An agent as resumebot in each disguise it holds (one runner can hold several roles and takes them in turn). Only the
 * role its current work is for is lit; the rest are asleep. An agent with none of these roles is the plain robot. */
export default function AgentCrew({ agent, size = 32, className }: { agent: AgentSummary; size?: number; className?: string }) {
  const outfits = ORDER.filter((o) => agent.roles?.includes(o));
  const working = !agent.paused && agent.status === "working";
  const off = agent.status === "offline";
  const classes = ["agent-crew", off ? "off" : "", className ?? ""].filter(Boolean).join(" ");
  if (outfits.length === 0) {
    return (
      <span className={classes}>
        <BotMark size={size} active={working} className={working ? undefined : "bot-asleep"} />
      </span>
    );
  }
  return (
    <span className={classes}>
      {outfits.map((o) => {
        const lit = working && agent.current?.role === o;
        return <BotMark key={o} size={size} outfit={o} active={lit} className={lit ? undefined : "bot-asleep"} />;
      })}
    </span>
  );
}
