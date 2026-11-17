import react from "@vitejs/plugin-react";
import { configDefaults, defineConfig } from "vitest/config";

// The page tests run with the browser in a zone far from the owner's (test-setup.ts gives the owner Pacific/Kiritimati), so
// a time written in the browser's zone instead of the owner's fails a test. Set here, before the workers start, so they
// inherit it; a worker cannot change its own.
(globalThis as unknown as { process: { env: Record<string, string> } }).process.env.TZ = "Pacific/Pago_Pago";

export default defineConfig({
  plugins: [react()],
  resolve: {
    // The process-view package (../packages/process-view) is linked in; its imports of these must be the app's own.
    dedupe: ["react", "react-dom", "@xyflow/react"],
  },
  server: {
    // The linked package lives beside the frontend, not inside it.
    fs: { allow: [".."] },
    // The app answers on its local port (app/scripts/local/stack.py): the API, the health check and the Excel export.
    proxy: {
      "/health": "http://localhost:8430",
      "/api": "http://localhost:8430",
      "/export.xlsx": "http://localhost:8430",
    },
  },
  build: {
    // Two pages: the app's shell, and the first tab of the browser jobs' window (browser-window.html: plain words in the app's
    // look, no script), which the app serves beside the shell (backend/app/web.py) and the browser bridge opens (app/scout/own-chrome.mjs).
    rollupOptions: { input: { index: "index.html", "browser-window": "browser-window.html" } },
    // The app is served locally, so the biggest chunk (573 kB) is no problem; the default 500 kB warning in the
    // setup build only reads like a failure to someone installing it.
    chunkSizeWarningLimit: 600,
  },
  test: {
    environment: "jsdom",
    globals: true,
    // The package's own tests run with the app's (the package is linked from ../packages/process-view).
    include: ["src/**/*.{test,spec}.{ts,tsx}", "../packages/*/src/**/*.test.{ts,tsx}"],
    // Two of the package's test files read the BPMN files of the project this package came from (../playbooks/*.bpmn, found by name: check_one_item
    // and eight more), which this repo does not have: its own processes are in playbooks/jobsearch. They cover the
    // package's editor and BPMN round trip, which this app does not use (How they work only reads).
    exclude: [
      ...configDefaults.exclude,
      "../packages/process-view/src/__tests__/bpmn.test.ts",
      "../packages/process-view/src/__tests__/BpmnProcessView.test.tsx",
      // Five more read that project's playbooks (see app/PROVENANCE.md).
      "../packages/process-view/src/__tests__/engineParity.test.ts",
      "../packages/process-view/src/__tests__/fieldRoundTrip.test.ts",
      "../packages/process-view/src/__tests__/fileDamage.test.ts",
      "../packages/process-view/src/__tests__/specRoundTrip.test.ts",
      "../packages/process-view/src/__tests__/viewState.test.tsx",
    ],
    // vmThreads reuses one jsdom per worker instead of building it per file
    // (jsdom setup was ~half of the run time); tests stay isolated by VM context.
    pool: "vmThreads",
    setupFiles: ["./src/test-setup.ts"],
    // Stylesheets reach tests as empty modules by default; src/visually-hidden.test.ts reads the shared ones.
    css: { include: [/src\/styles\/.*\.css/] },
  },
});
