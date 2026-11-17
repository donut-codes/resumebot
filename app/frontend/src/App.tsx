import { lazy, Suspense, useEffect, useReducer, useState } from "react";
import { api, errorMessage } from "./api/client";
import FocusInbox from "./components/focus/FocusInbox";
import NotificationsScreen from "./components/inbox/NotificationsScreen";
import EmployersScreen from "./components/employers/EmployersScreen";
import MeScreen from "./components/me/MeScreen";
import SearchLeaveGuard from "./components/search/SearchLeaveGuard";
import SearchScreen from "./components/search/SearchScreen";
import ErrorBanner from "./components/primitives/ErrorBanner";
import ErrorBoundary from "./components/primitives/ErrorBoundary";
import Loading from "./components/primitives/Loading";
import Shell, { tabLabel } from "./components/Shell";
import { ToastProvider } from "./components/toast/ToastProvider";
import { RefreshProvider, RouteProvider, useRoute } from "./lib/appContext";
import { setOwnerZone } from "./lib/dates";
import { useOwnerNewDay } from "./lib/useOwnerNewDay";
import { blankRoute, serializeRoute, type Tab } from "./lib/route";
import { createEmployersStore, EmployersStoreProvider } from "./state/employersStore";
import { createInboxStore, InboxStoreProvider } from "./state/inboxStore";
import { createPipelineStore, PipelineStoreProvider } from "./state/pipelineStore";
import { createSearchStore, SearchStoreProvider } from "./state/searchStore";

// How they work draws the process with xyflow, dagre and d3-force; the Agent Settings screen loads when it is first opened,
// so those stay out of the initial bundle. (The Missions tab is part of it.)
const AgentsScreen = lazy(() => import("./components/agents/AgentsScreen"));

/** The shell and the screen the address names. */
function Main() {
  const { route, hash, go } = useRoute();
  // Clicking the tab you are on loads that screen again.
  const [refreshKey, bumpRefresh] = useReducer((k: number) => k + 1, 0);
  // The Pipeline's data: one store for the app's life, so what it read outlasts a visit to another tab.
  const [pipeline] = useState(() => createPipelineStore());
  // Notifications' data and the bar's needs-you count: one store, started by the bar, kept fresh by the live stream.
  const [inbox] = useState(() => createInboxStore());
  // Employers' data: one store, started while the screen is open, kept fresh by the live stream.
  const [employers] = useState(() => createEmployersStore());
  // Search Settings' data: one store, started while the screen is open, kept fresh by the live stream.
  const [search] = useState(() => createSearchStore());

  // A tab left open past the owner's midnight, or a laptop woken the next day, reads every screen again: "today", ages and
  // the day a task is put off to would otherwise stay yesterday's.
  useOwnerNewDay(() => {
    bumpRefresh();
    void inbox.reload();
  });

  function selectTab(t: Tab) {
    // A tab always lands on its front page.
    const next = blankRoute(t);
    if (serializeRoute(next) === window.location.hash) bumpRefresh();
    go(next);
  }

  // The window title names the page you are on, so a tab in the browser (and a screen reader) can tell them apart.
  useEffect(() => {
    document.title = `${tabLabel(route.tab)} · resumebot`;
  }, [route.tab]);

  return (
    <PipelineStoreProvider store={pipeline}>
    <RefreshProvider value={refreshKey}>
      <InboxStoreProvider store={inbox}>
      <EmployersStoreProvider store={employers}>
      <SearchStoreProvider store={search}>
        <SearchLeaveGuard />
        <ToastProvider>
          <Shell tab={route.tab} onTab={selectTab}>
            <ErrorBoundary resetKey={hash}>
              <Suspense fallback={<Loading>Loading&hellip;</Loading>}>
                {route.tab === "board" && <FocusInbox />}
                {route.tab === "employers" && <EmployersScreen />}
                {route.tab === "me" && <MeScreen />}
                {route.tab === "profile" && <SearchScreen />}
                {route.tab === "inbox" && <NotificationsScreen />}
                {(route.tab === "agents" || route.tab === "missions") && <AgentsScreen />}
              </Suspense>
            </ErrorBoundary>
          </Shell>
        </ToastProvider>
      </SearchStoreProvider>
      </EmployersStoreProvider>
      </InboxStoreProvider>
    </RefreshProvider>
    </PipelineStoreProvider>
  );
}

export default function App() {
  // There is no sign-in and no setup: the page waits until the server answers /me, so a server that is not there is
  // said at once instead of by every screen below.
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .me()
      .then((me) => {
        // Before anything is drawn: every time on the page is written on the owner's clock.
        setOwnerZone(me.timezone);
        if (live) setReady(true);
      })
      .catch((err) => live && setError(errorMessage(err, "Couldn't reach resumebot.")));
    return () => {
      live = false;
    };
  }, []);

  if (error) return <ErrorBanner style={{ margin: "var(--fi-s9)" }}>{error}</ErrorBanner>;
  if (!ready) return <Loading>Loading resumebot&hellip;</Loading>;

  return (
    <RouteProvider>
      <Main />
    </RouteProvider>
  );
}
