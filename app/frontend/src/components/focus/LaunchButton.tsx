import type { ApplyGo } from "../../api/types/pipeline";
import { LaunchIcon } from "./icons";

/** Where Launch in Browser is, in words, while it runs or once it ended. */
export function launchWords(went: ApplyGo | null): string | null {
  if (!went || went.phase === "idle") return null;
  if (went.phase === "asked" || went.phase === "checking") return "Checking today's roles";
  if (went.phase === "building") {
    const count = `Building packets: ${went.built} of ${went.total}`;
    const now = went.current;
    if (!now) return count;
    const role = now.company ? `${now.title} at ${now.company}` : now.title;
    return `${count} · ${now.step ? `${role}: ${now.step}` : role}`;
  }
  if (went.phase === "opening") return `Opening tabs: ${went.opened.length} of ${went.total}`;
  if (went.phase === "stopped") return went.notes[0] ?? "Launch stopped";
  const n = went.opened.length;
  return n ? `Opened ${n} ${n === 1 ? "role" : "roles"} in your browser` : (went.notes[0] ?? "Nothing to open");
}

/** Launch in Browser: builds each Ready to Launch role's packet and opens its tab as the packet is done. */
export default function LaunchButton({ went, onLaunch }: { went: ApplyGo | null; onLaunch: () => void }) {
  const running = !!went && !went.done && went.phase !== "idle";
  return (
    <button type="button" className="fi-btn fi-btn-go" disabled={running} onClick={onLaunch}>
      <LaunchIcon className="fi-btn-icon" />
      {running ? "Launching" : "Launch in Browser"}
    </button>
  );
}
