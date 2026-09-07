import { defineConfig } from "@playwright/test";
import fs from "node:fs";

// Prefer the distro Chromium when present so CI/dev machines do not need a
// Playwright browser download. Fall back to Playwright's bundled Chromium.
const SYSTEM_CHROMIUM = "/usr/sbin/chromium";

export default defineConfig({
  testDir: "./tests",
  // One flaky-nav retry max; no token-wasting retries.
  retries: 1,
  workers: 1,
  // Overall budget: 10-15 min for the whole run; a single spec gets up to 10.
  timeout: 10 * 60 * 1000,
  expect: {
    // Explicit waits, no sleeps: generous per-assertion timeout for slow
    // first-boot (sandbox image pull, uvx, LLM latency).
    timeout: 30_000,
  },
  outputDir: "./test-results",
  use: {
    headless: true,
    launchOptions: {
      // Prefer the distro Chromium when present so no browser download is
      // needed. (executablePath belongs under launchOptions.)
      ...(fs.existsSync(SYSTEM_CHROMIUM)
        ? { executablePath: SYSTEM_CHROMIUM }
        : {}),
      // Required when running as root inside containers.
      args: ["--no-sandbox", "--disable-dev-shm-usage"],
    },
    // No video/trace: the run handles real credentials and traces can record
    // request bodies.
    video: "off",
    trace: "off",
    screenshot: "only-on-failure",
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
  },
  reporter: [["list"], ["html", { outputFolder: "./playwright-report" }]],
});
