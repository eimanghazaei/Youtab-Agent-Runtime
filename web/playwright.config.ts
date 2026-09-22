import { defineConfig, devices } from "@playwright/test";

/**
 * Web Playwright config for the Agent Runtime `/runtime` surface.
 *
 * Runs a NORMAL browser (Chromium) against the REAL `youtab dashboard`
 * backend, which serves the built SPA on the same origin and injects the
 * session token into index.html — no Electron, no IPC.
 *
 * Boot the backend yourself, or let the `webServer` block do it. The backend
 * must have `youtab_agent_cli` importable; point `YOUTAB_AGENT_DASHBOARD_PYTHON`
 * at that interpreter and set `PYTHONPATH` to the monorepo root.
 *
 *   YOUTAB_AGENT_DASHBOARD_PYTHON=<python.exe> \
 *   YOUTAB_AGENT_PYTHONPATH=<monorepo-root> \
 *   npx playwright test
 */

const PORT = Number(process.env.YOUTAB_AGENT_DASHBOARD_PORT ?? 9119);
const BASE_URL =
  process.env.YOUTAB_AGENT_DASHBOARD_URL ?? `http://127.0.0.1:${PORT}`;

const PYTHON = process.env.YOUTAB_AGENT_DASHBOARD_PYTHON;

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
  // Only manage the backend when a Python interpreter is provided; otherwise
  // assume the operator already booted `youtab dashboard` at BASE_URL.
  webServer: PYTHON
    ? {
        command: `"${PYTHON}" -m youtab_agent_cli.main dashboard --no-open --insecure --port ${PORT}`,
        url: `${BASE_URL}/api/status`,
        reuseExistingServer: true,
        timeout: 180_000,
        env: {
          ...(process.env.YOUTAB_AGENT_PYTHONPATH
            ? { PYTHONPATH: process.env.YOUTAB_AGENT_PYTHONPATH }
            : {}),
        },
      }
    : undefined,
});
