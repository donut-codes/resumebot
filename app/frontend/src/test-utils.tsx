// Shared helpers for the component tests: a JSON response, a fetch stub that puts the real fetch back after each
// test, the /me answer, and `render`, which puts a screen where the app puts it. Only test files import this.
import { act, render as renderInto, type RenderOptions, type RenderResult } from "@testing-library/react";
import { useState, type ReactElement, type ReactNode } from "react";
import type { Me } from "./api/types/me";
import { RefreshProvider, RouteProvider } from "./lib/appContext";
import { createInboxStore, InboxStoreProvider } from "./state/inboxStore";

/** Notifications' store for one rendered screen, reading the stubbed `fetch`. */
function TestInbox({ children }: { children: ReactNode }) {
  const [store] = useState(() => createInboxStore());
  return <InboxStoreProvider store={store}>{children}</InboxStoreProvider>;
}

/** A resolved JSON `Response`, the shape every stubbed `fetch` handler returns. */
export function json(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

// The handler may type its input however it likes (a string URL, `RequestInfo | URL`, nothing at all): `never`
// accepts all of them, so a handler written for one call style needs no cast at the call site.
type FetchHandler = (input: never, init?: RequestInit) => Promise<Response> | Response;

let realFetch: typeof fetch | undefined;

/** Replace `fetch` for the current test. test-setup.ts calls `restoreFetch` after every test, so a stub never
 *  leaks into the next one. Stubbing again inside a test (to wrap or change the answer) is fine. */
export function stubFetch(handler: FetchHandler): void {
  if (!realFetch) realFetch = globalThis.fetch;
  globalThis.fetch = handler as unknown as typeof fetch;
}

/** Put the real `fetch` back, if a test stubbed it. Called from test-setup.ts after each test. */
export function restoreFetch(): void {
  if (realFetch) {
    globalThis.fetch = realFetch;
    realFetch = undefined;
  }
}

/** What GET /api/v1/me answers. */
export const ME: Me = { version: "1.0.0" };

interface AppOptions {
  /** The address the screen is rendered at ("#/employers/e1"); the screen reads its route from it, and follows it. */
  hash?: string;
  /** What the app's refresh counter says. */
  refreshKey?: number;
  /** Also hold Notifications' store, as the app does (a screen that shows it, or the bar's count, asks for it). */
  inbox?: boolean;
}

/** The app's contexts around a screen, as App provides them: the address, the refresh counter. */
export function AppContexts({ children, refreshKey = 0, inbox = false }: { children: ReactNode } & Pick<AppOptions, "refreshKey" | "inbox">) {
  return (
    <RouteProvider>
      <RefreshProvider value={refreshKey}>{inbox ? <TestInbox>{children}</TestInbox> : children}</RefreshProvider>
    </RouteProvider>
  );
}

/** Renders a screen where the app puts it: with the address (`hash`) and the refresh counter, which is what a screen
 * reads instead of being handed. */
export function render(ui: ReactElement, { hash, refreshKey, inbox, ...options }: AppOptions & Omit<RenderOptions, "wrapper"> = {}): RenderResult {
  if (hash !== undefined) window.history.replaceState(null, "", hash || "#/");
  return renderInto(ui, { wrapper: ({ children }) => <AppContexts refreshKey={refreshKey} inbox={inbox}>{children}</AppContexts>, ...options });
}

/** Let every request a screen has already started land, inside `act`: a test that ends while a stubbed load is still on
 *  its way would have the screen update after the test, outside `act`, and React warns. Used as `afterEach(settle)` in a
 *  file whose tests do not each wait for every load. A load can start another (a list, then its details), so it waits a
 *  few rounds. */
export async function settle(): Promise<void> {
  for (let round = 0; round < 5; round++) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}
