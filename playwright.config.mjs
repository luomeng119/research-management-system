import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './app/tests/e2e',
  timeout: 30_000,
  expect: { timeout: 8_000 },
  fullyParallel: false,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:8877',
    viewport: { width: 1366, height: 768 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    ...devices['Desktop Chrome'],
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  webServer: {
    command: 'PYTHONPATH=. .venv/bin/python app/tests/e2e/server.py',
    url: 'http://127.0.0.1:8877/healthz',
    reuseExistingServer: false,
    timeout: 30_000,
  },
  outputDir: 'build/playwright-results',
});
