import { useEffect } from "react";
import type { Tab } from "../lib/route";
import { needsYou, useInbox, useInboxStore } from "../state/inboxStore";
import BotMark from "./primitives/BotMark";

interface Props {
  tab: Tab;
  onTab: (t: Tab) => void;
  children: React.ReactNode;
}

type NavTab = { key: Tab; label: string };

// The six pages, in one line: the owner's own work (My Resume, Search Settings, the Pipeline, Employers), then the app
// talking to the owner and the owner steering it (Notifications, Agent Settings).
const TABS: NavTab[] = [
  { key: "me", label: "My Resume" },
  { key: "profile", label: "Search Settings" },
  { key: "board", label: "Pipeline" },
  { key: "employers", label: "Employers" },
  { key: "inbox", label: "Notifications" },
  { key: "agents", label: "Agent Settings" },
];

/** The tab that holds a route: the Missions are a tab of Agent Settings. */
function homeOf(tab: Tab): Tab {
  if (tab === "missions") return "agents";
  return tab;
}

/** The name of the page a tab opens, for the window title. */
export function tabLabel(tab: Tab): string {
  const here = homeOf(tab);
  return TABS.find((t) => t.key === here)?.label ?? "resumebot";
}

/** The bar over every page (each page fills the window below it, edge to edge): the robot and the name (the way home), the six pages as tabs, and on Notifications the
 * needs-you count, which follows the live stream (state/inboxStore.ts). */
export default function Shell({ tab, onTab, children }: Props) {
  const here = homeOf(tab);
  const inbox = useInboxStore();
  useEffect(() => inbox.start(), [inbox]);
  const needs = useInbox(needsYou);
  return (
    <div className="app-shell">
      <header className="fi-bar">
        <button type="button" className="fi-brand" aria-label="resumebot home" onClick={() => onTab("board")}>
          <BotMark size={32} />
          <span className="fi-brand-name">resumebot</span>
        </button>
        <nav className="fi-tabs" aria-label="Pages">
          <ul>
            {TABS.map((t) => (
              <li key={t.key}>
                <button type="button" className="fi-tab" aria-current={here === t.key ? "page" : undefined} onClick={() => onTab(t.key)}>
                  {t.label}
                  {t.key === "inbox" && needs > 0 && <span className="fi-pill">{needs}</span>}
                </button>
              </li>
            ))}
          </ul>
        </nav>
      </header>
      <main className="app-main">{children}</main>
    </div>
  );
}
