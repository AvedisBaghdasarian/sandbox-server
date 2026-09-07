/**
 * Full user-path E2E against real containers.
 *
 * Flow (all UI steps performed like a real person in headless Chromium):
 *  1. Dismiss telemetry, skip onboarding, open Manage Backends, add a local
 *     backend pointing at the sandbox-server (type + submit), and select it.
 *  2. Create a provider connection (openai + custom base_url + runtime key).
 *  3. Create + activate an LLM profile linked to that connection.
 *  4. Start a conversation, send one short message, prove the agent talks
 *     (chat reply token + trivial terminal output).
 *
 * Secrets: the LLM api key arrives via the E2E_LLM_API_KEY env var, is kept
 * in memory only, filled into a password field, and never logged. Failure
 * messages that could echo credential-bearing responses are redacted.
 *
 * Run via ./run-e2e.sh (builds stack, runs this spec, tears down).
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

const FRONTEND_URL = process.env.E2E_FRONTEND_URL ?? "http://localhost:8080";
const BACKEND_URL = process.env.E2E_BACKEND_URL ?? "http://localhost:3000";
const SESSION_KEY = process.env.E2E_SESSION_KEY ?? "";
const LLM_API_KEY = process.env.E2E_LLM_API_KEY ?? "";
const CONNECTION_NAME = process.env.E2E_CONNECTION_NAME ?? "e2e-openai";
const PROFILE_NAME = process.env.E2E_PROFILE_NAME ?? "e2e-profile";
const MODEL_ID =
  process.env.E2E_MODEL_ID ?? "openai/muse-spark-1.3-contributor";
const PROVIDER_BASE_URL =
  process.env.E2E_PROVIDER_BASE_URL ?? "https://opencode.ai/zen/go/v1";

const REPLY_TOKEN = "PING_OK";
const BASH_TOKEN = "E2E_TRIVIAL_OK";

/**
 * Click that tolerates the lazily-appearing telemetry overlay: on failure,
 * clear it and retry once. (Explicit waits elsewhere; this only hardens the
 * act step against a known overlay race.)
 */
async function resilientClick(page: Page, locator: Locator) {
  try {
    await locator.click({ timeout: 10_000 });
  } catch {
    await dismissTelemetryDialog(page);
    await locator.click({ timeout: 15_000 });
  }
}
async function dismissTelemetryDialog(page: Page) {
  const confirm = page.getByTestId("confirm-telemetry-preferences");
  if (await confirm.isVisible({ timeout: 3_000 }).catch(() => false)) {
    const box = page.getByRole("checkbox", { name: /anonymous usage data/i });
    if (await box.count()) {
      if (await box.first().isChecked().catch(() => false)) {
        await box.first().uncheck();
      }
    }
    await confirm.click();
    await expect(confirm).toHaveCount(0, { timeout: 30_000 });
  }
}

test.describe("full user path on real containers", () => {
  test.beforeEach(async ({ page }) => {
    // Telemetry opt-out only. Deliberately NOT setting `openhands-onboarded`
    // so the real first-run backend form flow is exercised.
    await page.addInitScript(() => {
      window.localStorage.setItem("analytics-consent", "false");
      window.localStorage.setItem("openhands-telemetry-consent", "denied");
      window.localStorage.setItem("openhands-telemetry-first-use", "true");
    });
  });

  test("backend, connection, profile, and a talking conversation", async ({
    page,
    request,
  }) => {
    test.setTimeout(10 * 60 * 1000);
    const t0 = Date.now();
    const elapsed = () => `${((Date.now() - t0) / 1000).toFixed(1)}s`;

    expect(
      SESSION_KEY,
      "E2E_SESSION_KEY must be set by run-e2e.sh",
    ).toBeTruthy();
    expect(
      LLM_API_KEY,
      "E2E_LLM_API_KEY must be set by run-e2e.sh (read from .env `api_key`)",
    ).toBeTruthy();

    // Best-effort API cleanup so retries/idempotent reruns start fresh.
    // (Server-side state only; every UI step below still runs in the browser.
    // Backends live in browser localStorage, which is already fresh per run.)
    const apiHeaders = { "X-Session-API-Key": SESSION_KEY };
    try {
      await request.delete(
        `${BACKEND_URL}/api/profiles/${encodeURIComponent(PROFILE_NAME)}`,
        { headers: apiHeaders },
      );
    } catch {
      // missing profile or unreachable; the UI flow will surface real errors
    }
    try {
      const list = await request.get(
        `${BACKEND_URL}/api/llm/provider-connections`,
        { headers: apiHeaders },
      );
      if (list.ok()) {
        const items = (await list.json()) as Array<{
          id: string;
          display_name: string;
        }>;
        for (const item of items.filter(
          (c) => c.display_name === CONNECTION_NAME,
        )) {
          await request.delete(
            `${BACKEND_URL}/api/llm/provider-connections/${encodeURIComponent(
              item.id,
            )}`,
            { headers: apiHeaders },
          );
        }
      }
    } catch {
      // best effort only
    }

    // ------------------------------------------------------------------
    // 1. Add local backend via the real backend form.
    //
    // This image serves the SPA under /canvas and its onboarding wizard has
    // no backend phase (a seeded "Local" backend already exists), so the
    // real-person path is: telemetry dialog -> skip onboarding -> Manage
    // Backends modal -> Add Backend form -> select the new backend.
    // ------------------------------------------------------------------
    await page.goto(FRONTEND_URL, { waitUntil: "domcontentloaded" });
    const appBase = await page.evaluate(
      () => `${window.location.origin}/canvas`,
    );

    // Telemetry dialog (if shown): opt out like a privacy-minded person.
    await dismissTelemetryDialog(page);

    // Entry: the telemetry dialog (z-70 overlay) and the onboarding wizard
    // can appear lazily in any order, and telemetry intercepts clicks on
    // everything beneath it. Drive to the app shell: whenever telemetry is
    // present clear it first, otherwise advance/skip onboarding.
    // Ends with the shell visible (backend-selector) and both dialogs gone.
    await expect
      .poll(
        async () => {
          const tel = page.getByTestId("confirm-telemetry-preferences");
          if (await tel.count()) {
            const box = page.getByRole("checkbox", {
              name: /anonymous usage data/i,
            });
            if (await box.count()) {
              try {
                if (await box.first().isChecked()) {
                  await box.first().uncheck();
                }
              } catch {
                // keep going; confirm below still dismisses the dialog
              }
            }
            try {
              await tel.click({ timeout: 5_000 });
            } catch {
              // overlay race; the next poll iteration retries
            }
            return "telemetry";
          }
          if (await page.getByTestId("onboarding-modal").count()) {
            const skip =
              (await page.getByTestId("onboarding-skip").count())
                ? page.getByTestId("onboarding-skip")
                : page.getByRole("button", { name: /skip for now/i });
            if (await skip.count()) {
              try {
                await skip.first().click({ timeout: 5_000 });
              } catch {
                // intercepted by a just-appeared overlay; retry next round
              }
            } else {
              const next = page.getByTestId("onboarding-agent-next");
              if (await next.count()) {
                try {
                  await next.click({ timeout: 5_000 });
                } catch {
                  // retry next round
                }
              }
            }
            return "onboarding";
          }
          if (await page.getByTestId("backend-selector").count()) {
            return "shell";
          }
          return "waiting";
        },
        { timeout: 180_000 },
      )
      .toBe("shell");

    // Open Manage Backends through the backend selector dropdown.
    await expect(page.getByTestId("backend-selector")).toBeVisible({
      timeout: 60_000,
    });
    await resilientClick(page, page.getByTestId("backend-selector"));
    await resilientClick(page, page.getByTestId("manage-backends-menu-item"));
    await expect(page.getByTestId("manage-backends-modal")).toBeVisible();

    // Add Backend form: choose the agent-server (local) option.
    await resilientClick(page, page.getByTestId("manage-backends-add"));
    await expect(
      page.getByTestId("add-backend-chooser"),
      "backend chooser should be visible in the add form",
    ).toBeVisible({ timeout: 30_000 });
    await resilientClick(page, page.getByTestId("add-backend-option-agent-server"));
    await expect(
      page.getByTestId("add-backend-agent-server-panel"),
    ).toBeVisible();

    // Local (not remote) agent-server location.
    const locationToggle = page.getByTestId("add-backend-location");
    if (await locationToggle.isVisible().catch(() => false)) {
      const localOption = locationToggle.getByRole("button", {
        name: /local/i,
      });
      if (await localOption.count()) {
        await resilientClick(page, localOption.first());
      }
    }

    await page.getByTestId("add-backend-name").fill("e2e-local");
    await page.getByTestId("add-backend-host").fill(BACKEND_URL);
    // Password field: value is masked, never logged.
    await page.getByTestId("add-backend-api-key").fill(SESSION_KEY);
    await resilientClick(page, page.getByTestId("add-backend-submit"));

    // Reachable/green: the add form closes and the new row is listed.
    await expect(page.getByTestId("add-backend-modal")).toHaveCount(0, {
      timeout: 60_000,
    });
    // Adding may navigate away and unmount the manage modal; reopen it if so.
    if (
      await page
        .getByTestId("manage-backends-modal")
        .isVisible({ timeout: 5_000 })
        .catch(() => false)
    ) {
      // already open
    } else {
      await resilientClick(page, page.getByTestId("backend-selector"));
      await resilientClick(page, page.getByTestId("manage-backends-menu-item"));
      await expect(page.getByTestId("manage-backends-modal")).toBeVisible({
        timeout: 30_000,
      });
    }
    const backendRow = page.getByTestId("manage-backends-row-e2e-local");
    await expect(
      backendRow,
      "new backend should be listed in Manage Backends",
    ).toBeVisible({ timeout: 60_000 });
    console.log(`[${elapsed()}] backend added and listed`);

    // Activate it: the row button enables once health is green.
    const rowButton = backendRow.getByRole("button").first();
    await expect(rowButton, "new backend should become selectable").toBeEnabled({
      timeout: 120_000,
    });
    await resilientClick(page, rowButton);
    // Selecting closes the manage modal.
    await expect(page.getByTestId("manage-backends-modal")).toHaveCount(0, {
      timeout: 30_000,
    });
    console.log(`[${elapsed()}] backend selected and active`);

    // Backend-level sanity (not a UI shortcut: the UI already proved green).
    const serverInfo = await request.get(`${BACKEND_URL}/server_info`, {
      headers: { "X-Session-API-Key": SESSION_KEY },
    });
    expect(serverInfo.ok()).toBeTruthy();

    // ------------------------------------------------------------------
    // 2. Provider connection: provider=openai + custom base_url + key.
    // ------------------------------------------------------------------
    await page.goto(`${appBase}/settings/llm`, {
      waitUntil: "domcontentloaded",
    });
    await dismissTelemetryDialog(page);
    await expect(page.getByTestId("add-provider-connection")).toBeVisible({
      timeout: 60_000,
    });
    await resilientClick(page, page.getByTestId("add-provider-connection"));
    await expect(page.getByTestId("provider-connection-modal")).toBeVisible();

    await page.getByTestId("provider-connection-name-input").fill(
      CONNECTION_NAME,
    );

    // Provider autocomplete: the testid sits on the combobox input itself.
    // Type to filter, then pick the matching option.
    const providerCombo = page.getByTestId(
      "provider-connection-provider-input",
    );
    await resilientClick(page, providerCombo);
    await providerCombo.fill("openai");
    const providerOption = page.locator('[data-testid^="provider-item-"]', {
      hasText: /^openai$/i,
    });
    if (await providerOption.count()) {
      await resilientClick(page, providerOption.first());
    } else {
      // Fall back to the first listbox option mentioning openai.
      await resilientClick(
        page,
        page.getByRole("option", { name: /openai/i }).first(),
      );
    }

    // Password field: masked, never logged.
    await page.getByTestId("provider-connection-api-key-input").fill(
      LLM_API_KEY,
    );
    await page.getByTestId("provider-connection-base-url-input").fill(
      PROVIDER_BASE_URL,
    );
    await dismissTelemetryDialog(page);
    await resilientClick(page, page.getByTestId("provider-connection-submit"));

    // Connection listed.
    await expect(
      page.getByTestId("provider-connection-row").filter({
        hasText: CONNECTION_NAME,
      }),
      "provider connection should be listed after save",
    ).toBeVisible({ timeout: 60_000 });
    console.log(`[${elapsed()}] provider connection listed`);

    // ------------------------------------------------------------------
    // 3. Profile linked to the connection (no inline api_key), then active.
    // ------------------------------------------------------------------
    await dismissTelemetryDialog(page);
    await resilientClick(page, page.getByTestId("add-llm-profile"));
    await expect(page.getByTestId("profile-name-input")).toBeVisible();
    await page.getByTestId("profile-name-input").fill(PROFILE_NAME);

    // Use the "All" tab for exact model entry + connection linking.
    const allToggle = page.getByTestId("sdk-section-all-toggle");
    if (await allToggle.isVisible()) {
      await resilientClick(page, allToggle);
    }
    await expect(page.getByTestId("llm-custom-model-input")).toBeVisible({
      timeout: 30_000,
    });
    await page.getByTestId("llm-custom-model-input").fill(MODEL_ID);

    // Link the provider connection (inline key/base-url inputs hide).
    // Same combobox pattern: the testid is on the input itself.
    const connectionCombo = page.getByTestId("llm-provider-connection-input");
    await expect(connectionCombo).toBeVisible({ timeout: 30_000 });
    const selectConnection = async () => {
      await resilientClick(page, connectionCombo);
      await connectionCombo.fill(CONNECTION_NAME);
      const option = page.getByRole("option", { name: CONNECTION_NAME });
      await expect(option.first()).toBeVisible({ timeout: 15_000 });
      await resilientClick(page, option.first());
    };
    await selectConnection();
    // A committed link hides the inline credential inputs. If the selection
    // did not commit (stale listbox, missed click), retry once from scratch.
    if (await page.getByTestId("llm-api-key-input").count()) {
      await resilientClick(page, connectionCombo);
      await connectionCombo.fill("");
      await selectConnection();
    }
    await expect(
      page.getByTestId("llm-api-key-input"),
      "linking the connection should hide the inline API key input",
    ).toHaveCount(0, { timeout: 15_000 });

    await dismissTelemetryDialog(page);
    await resilientClick(page, page.getByTestId("save-profile-btn"));
    // Back on the list with the new profile present.
    await expect(
      page.getByTestId("profile-row").filter({ hasText: PROFILE_NAME }),
      "profile should be listed after save",
    ).toBeVisible({ timeout: 60_000 });
    console.log(`[${elapsed()}] profile listed`);

    // The row must show a healthy (key-bearing) profile, never an orphaned
    // connection link — otherwise the home composer stays LLM-blocked.
    const profileRow = page.getByTestId("profile-row").filter({
      hasText: PROFILE_NAME,
    });
    await expect(
      profileRow.getByTestId("profile-broken-connection-badge"),
      "profile must not report a broken connection",
    ).toHaveCount(0, { timeout: 15_000 });

    // Activate via the row's actions menu. A freshly created (sole) profile
    // may already be active, in which case "Set active" is disabled — accept
    // the badge either way.
    await resilientClick(page, profileRow.getByTestId("profile-menu-trigger"));
    await expect(page.getByTestId("profile-actions-menu")).toBeVisible();
    const setActiveItem = page.getByTestId("profile-set-active");
    if (await setActiveItem.isEnabled().catch(() => false)) {
      await dismissTelemetryDialog(page);
      await resilientClick(page, setActiveItem);
    }
    await expect(
      profileRow.getByTestId("profile-active-badge"),
      "profile should be (or become) active",
    ).toBeVisible({ timeout: 60_000 });
    console.log(`[${elapsed()}] profile active`);

    // ------------------------------------------------------------------
    // 4. Start a conversation from the home launcher and prove the agent
    //    talks. (The header thread-picker's launch entry proved unresponsive
    //    here; the home launcher is the primary real-person path: type a
    //    message, hit Send, land in the new conversation.)
    // ------------------------------------------------------------------
    await page.goto(`${appBase}/conversations`, {
      waitUntil: "domcontentloaded",
    });
    await dismissTelemetryDialog(page);
    await expect(page.getByTestId("chat-input")).toBeVisible({
      timeout: 60_000,
    });
    // The composer offers the activated profile.
    await expect(
      page.getByRole("button", { name: PROFILE_NAME }).first(),
      "composer should offer the activated profile",
    ).toBeVisible({ timeout: 30_000 });

    // One short turn, minimal spend: single terminal call + short reply.
    // Single line on purpose: keyboard Enter submits the composer, so a
    // multiline message would fire a premature partial send mid-typing.
    const userMessage =
      `Use the terminal tool exactly once to run this command: echo ${BASH_TOKEN} ` +
      `and then reply with exactly this token and finish: ${REPLY_TOKEN}. Nothing else.`;
    // Type like a real person: keyboard input keeps React state in sync
    // (direct DOM text injection leaves the composer empty and Send disabled).
    // Late overlays or re-renders can steal focus mid-typing, so verify the
    // text actually landed and redo the focus+type cycle until it sticks.
    const chatInput = page.getByTestId("chat-input");
    await expect
      .poll(
        async () => {
          const current = await page.evaluate(
            () =>
              document.querySelector('[data-testid="chat-input"]')
                ?.textContent ?? "",
          );
          if (current.includes(BASH_TOKEN)) return true;
          await resilientClick(page, chatInput);
          await page.keyboard.press("ControlOrMeta+a");
          await chatInput.pressSequentially(userMessage, { delay: 10 });
          return false;
        },
        { timeout: 90_000 },
      )
      .toBe(true);
    // Gate: with text present, home must converge to "ready" (linked profile
    // recognized → Send enabled), not "blocked" (banner). Queries refetch on
    // navigation, so poll for either stable state. A failure here reports the
    // stuck state instead of a bare timeout later.
    await expect
      .poll(
        async () => {
          if (
            await page
              .getByTestId("home-llm-not-configured-banner")
              .count()
          ) {
            return "blocked";
          }
          if (
            await page
              .getByTestId("submit-button")
              .isEnabled()
              .catch(() => false)
          ) {
            return "ready";
          }
          return "waiting";
        },
        { timeout: 120_000 },
      )
      .toBe("ready");
    await resilientClick(page, page.getByTestId("submit-button"));
    const sentAt = Date.now();
    console.log(`[${elapsed()}] message sent`);

    // Sending from the home launcher creates the conversation and navigates.
    // Creation boots a fresh sandbox server-side, so allow a generous budget.
    await expect(page).toHaveURL(/\/conversations\/.+/, {
      timeout: 240_000,
    });
    await expect(page.getByTestId("chat-input")).toBeVisible({
      timeout: 60_000,
    });
    const conversationId =
      page.url().match(/\/conversations\/([^/?#]+)/)?.[1] ?? "";
    expect(conversationId, "conversation id readable from URL").toBeTruthy();

    // Primary assertions are UI-only. The user bubble itself contains both
    // tokens (we asked for them), so strip user messages before asserting.
    const nonUserBodyText = (token: string) =>
      page.evaluate((expected) => {
        const body = document.body.cloneNode(true);
        if (!(body instanceof HTMLElement)) return false;
        body
          .querySelectorAll('[data-testid="user-message"]')
          .forEach((node) => node.remove());
        return body.textContent?.includes(expected) ?? false;
      }, token);

    await expect
      .poll(() => nonUserBodyText(BASH_TOKEN).catch(() => false), {
        // First turn includes sandbox/agent-server boot; allow up to 5 min.
        timeout: 5 * 60 * 1000,
      })
      .toBe(true);
    console.log(
      `[${elapsed()}] trivial command output observed in UI (+${(
        (Date.now() - sentAt) /
        1000
      ).toFixed(1)}s agent turn)`,
    );

    await expect
      .poll(() => nonUserBodyText(REPLY_TOKEN).catch(() => false), {
        timeout: 2 * 60 * 1000,
      })
      .toBe(true);
    console.log(`[${elapsed()}] chat reply observed in UI`);

    // Fallback assertion only: confirm a successful terminal observation via
    // the events API (the UI assertions above are the real proof).
    await expect
      .poll(
        async () => {
          try {
            const response = await request.get(
              `${BACKEND_URL}/api/conversations/${encodeURIComponent(
                conversationId,
              )}/events/search`,
              {
                headers: { "X-Session-API-Key": SESSION_KEY },
                params: { limit: "100", sort_order: "TIMESTAMP_DESC" },
              },
            );
            if (!response.ok()) return false;
            const body = (await response.json()) as {
              items?: Array<Record<string, unknown>>;
            };
            return (
              body.items?.some((item) =>
                JSON.stringify(item).includes(BASH_TOKEN),
              ) ?? false
            );
          } catch {
            return false;
          }
        },
        { timeout: 60_000 },
      )
      .toBe(true);
    console.log(`[${elapsed()}] events API corroborates command output`);

    // Cleanup: delete the conversation if the UI offers it (best effort;
    // test-scoped volumes are wiped by `compose down -v` regardless).
    try {
      const deleteResponse = await request.delete(
        `${BACKEND_URL}/api/conversations/${encodeURIComponent(
          conversationId,
        )}`,
        { headers: { "X-Session-API-Key": SESSION_KEY } },
      );
      if (deleteResponse.ok() || deleteResponse.status() === 404) {
        console.log(`[${elapsed()}] conversation cleaned up`);
      }
    } catch {
      // Non-fatal: volumes are destroyed on teardown.
    }
  });
});
