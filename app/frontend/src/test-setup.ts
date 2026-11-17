import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";
import { setOwnerZone } from "./lib/dates";
import { restoreFetch } from "./test-utils";

// CI runners are slower than a developer's machine; the 1s default for findBy/waitFor flaked on a cold first render.
// 5s still fails fast on a real hang.
configure({ asyncUtilTimeout: 5000 });

// jsdom logs "not implemented" for window.scrollTo.
window.scrollTo = () => {};

// jsdom has no ResizeObserver, which @xyflow/react (the process view) needs just to mount -- without this every
// test that renders it throws before any assertion runs, in components that never even touch resizing.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (!("ResizeObserver" in globalThis)) {
  (globalThis as unknown as { ResizeObserver: typeof ResizeObserverStub }).ResizeObserver = ResizeObserverStub;
}

// A test that stubbed fetch (stubFetch in test-utils.tsx) gets the real one back before the next test, and the address goes
// back to the front page (a screen reads where it is from the address).
afterEach(() => {
  restoreFetch();
  window.history.replaceState(null, "", "/");
});

// The owner's zone (what /me tells the page) is not the browser's (vite.config.ts sets TZ to Pacific/Pago_Pago, UTC-11):
// every test sees times on the owner's clock, UTC+14, unless it sets another.
beforeEach(() => setOwnerZone("Pacific/Kiritimati"));
