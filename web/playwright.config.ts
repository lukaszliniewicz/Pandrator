import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  // Fresh Windows pages can spend more than eight seconds loading the static
  // bundle before hydration begins. Keep a bounded platform budget, not retries.
  expect: { timeout: process.platform === 'win32' ? 20_000 : 8_000 },
  reporter: [
    ['list'],
    ['html', { open: 'never', outputFolder: 'playwright-report' }]
  ],
  snapshotPathTemplate:
    '{testDir}/__screenshots__/{testFilePath}/{arg}-{projectName}{ext}',
  use: {
    baseURL: 'http://127.0.0.1:8098',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure'
  },
  webServer: {
    command: 'python ../scripts/run_web_e2e_server.py',
    url: 'http://127.0.0.1:8098/api/v1/health',
    reuseExistingServer: !process.env.CI,
    timeout: 45_000
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        // Use an installed browser for local checks without downloading a bundle.
        channel: process.env.PANDRATOR_E2E_CHROME_CHANNEL,
        permissions: ['microphone'],
        launchOptions: {
          args: [
            '--use-fake-ui-for-media-stream',
            '--use-fake-device-for-media-stream',
            '--autoplay-policy=no-user-gesture-required'
          ]
        }
      }
    },
    {
      name: 'firefox',
      use: {
        ...devices['Desktop Firefox'],
        launchOptions: {
          firefoxUserPrefs: {
            'media.navigator.streams.fake': true,
            'media.navigator.permission.disabled': true
          }
        }
      }
    }
  ]
});
