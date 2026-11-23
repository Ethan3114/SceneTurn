import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/browser',
  outputDir: '../.intermediate/frontend-browser',
  timeout: 60000,
  reporter: 'line',
  use: { baseURL: process.env.OPENFMV_TEST_BASE_URL ?? 'http://127.0.0.1:3210', browserName: 'chromium', headless: true },
});
