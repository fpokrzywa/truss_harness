import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';

// `pnpm e2e` runs e2e/prepare.ts first (this checkout's database: migrated and seeded).
// Dedicated ports and dist dir, so the suite never collides with `pnpm dev` (3710) or the API (8710).
const API_PORT = 8780;
const WEB_PORT = 3780;
const WEB_ORIGIN = `http://127.0.0.1:${WEB_PORT}`;

// webServer children inherit process.env: the checkout's .devdata/dev.env decides the database.
delete process.env.SHIFDE_DATABASE_URL;
delete process.env.SHIFDE_MIGRATION_DATABASE_URL;
// No page snapshot in a failing test's error-context.md: it records form values, the password too.
process.env.PLAYWRIGHT_NO_COPY_PROMPT = '1';

export default defineConfig({
  testDir: 'e2e',
  testMatch: '**/*.spec.ts',
  // The tests share one seeded database and one account.
  workers: 1,
  fullyParallel: false,
  forbidOnly: true,
  reporter: 'list',
  use: {
    baseURL: WEB_ORIGIN,
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command: 'uv run --frozen shifde-api',
      cwd: path.resolve(import.meta.dirname, '..', 'backend'),
      url: `http://127.0.0.1:${API_PORT}/healthz`,
      env: {
        SHIFDE_ENV: 'development',
        SHIFDE_AUTH_LOCAL_ENABLED: 'true',
        SHIFDE_API_PORT: String(API_PORT),
        SHIFDE_ALLOWED_ORIGINS: WEB_ORIGIN,
        // databricks/snowflake connections reach the in-process fakes (e2e/connections.spec.ts)
        SHIFDE_DEV_FAKE_DATA_PLATFORMS: 'true',
        UV_LINK_MODE: 'copy',
      },
      reuseExistingServer: false,
      timeout: 180_000,
    },
    {
      // Runs the queued connection tests. It has no port of its own, and a `url` of the API that is
      // already up would be refused as in use: it starts once the entry above answers /healthz
      // (webServer entries start in order) and is not waited on (B-2759, B-3251).
      command: 'uv run --frozen shifde-worker',
      cwd: path.resolve(import.meta.dirname, '..', 'backend'),
      env: {
        SHIFDE_ENV: 'development',
        SHIFDE_DEV_FAKE_DATA_PLATFORMS: 'true',
        UV_LINK_MODE: 'copy',
      },
      reuseExistingServer: false,
      timeout: 180_000,
    },
    {
      command: `pnpm exec next dev -p ${WEB_PORT} -H 127.0.0.1`,
      url: `${WEB_ORIGIN}/login`,
      env: {
        SHIFDE_API_ORIGIN: `http://127.0.0.1:${API_PORT}`,
        SHIFDE_NEXT_DIST_DIR: '.next-e2e',
      },
      reuseExistingServer: false,
      timeout: 240_000,
    },
  ],
});
