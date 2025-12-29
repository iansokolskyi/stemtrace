import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright configuration for stemtrace UI E2E tests.
 *
 * Run tests:
 *   npx playwright test
 *   npx playwright test --ui  # Interactive mode
 *
 * Prerequisites:
 *   docker compose -f docker-compose.e2e.yml up -d --wait
 */
export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: [['html', { open: 'never' }], ['list']],

  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:8000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  // Don't start a web server - we expect docker-compose to be running
  // webServer: {
  //   command: 'docker compose -f docker-compose.e2e.yml up',
  //   url: 'http://localhost:8000/api/health',
  //   reuseExistingServer: true,
  // },
})
