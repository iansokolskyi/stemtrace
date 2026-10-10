import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright configuration for stemtrace UI E2E tests.
 *
 * Two modes:
 *   1. Mock mode (default): Tests run with mocked API responses
 *      - No Docker required
 *      - Tests control their own data
 *      - Fast and isolated
 *      - Uses Vite dev server on port 5173
 *
 *   2. Real mode: Tests run against real Docker services
 *      - Requires: docker compose -f docker-compose.e2e.yml up -d --wait
 *      - Tests actual integration
 *      - Set E2E_MODE=real to enable
 *
 * Run tests:
 *   npm test                    # Mock mode (default)
 *   E2E_PORT=5174 npm test      # Mock mode on another port
 *   E2E_MODE=real npm test      # Real mode (requires Docker)
 *   npx playwright test --ui    # Interactive mode
 */

const isRealMode = process.env.E2E_MODE === 'real'

// In mock mode, use Vite dev server (5173, or E2E_PORT when another checkout
// already serves 5173: the run would otherwise reuse that checkout's server)
// In real mode, use Docker server (8000 or custom)
const devPort = Number(process.env.E2E_PORT || 5173)
const devURL = `http://localhost:${devPort}`
const baseURL = isRealMode ? process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:8000' : devURL

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: [['html', { open: 'never' }], ['list']],

  use: {
    baseURL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  // In mock mode, start Vite dev server
  // In real mode, we expect Docker services to be running
  webServer: isRealMode
    ? undefined
    : {
        // --strictPort: fail instead of silently moving to the next free port
        command: `npm run dev -- --port ${devPort} --strictPort`,
        url: devURL,
        reuseExistingServer: !process.env.CI,
        timeout: 30000,
      },
})
