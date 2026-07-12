import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

test('wizard creates a guided subtitle workspace and preserves setup return', async ({ page }) => {
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'What shall we make?' })).toBeVisible();
  await page.getByRole('button', { name: /Create subtitles/ }).first().click();
  await page.getByRole('dialog').getByRole('button', { name: /Create subtitles/ }).click();
  await page.getByRole('button', { name: 'Add later' }).click();
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByRole('button', { name: 'Review', exact: true }).click();
  await page.getByLabel('Session name').fill('Playwright subtitles');
  await page.getByRole('button', { name: 'Create workspace' }).click();
  await expect(page.getByRole('heading', { name: 'Playwright subtitles' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Transcribe' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Tour' })).toBeVisible();
});

test('provider defaults and restartable tours are keyboard reachable', async ({ page }) => {
  await signIn(page);
  await page.getByRole('link', { name: 'Providers & services' }).click();
  await expect(page.getByRole('heading', { name: 'LLM models' })).toBeVisible();
  await page.getByRole('button', { name: 'Tour' }).click();
  await expect(page.getByRole('heading', { name: 'One record per canonical model' })).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(page.locator(':focus')).toBeVisible();
});

test('theme and setup dock remain available after navigation', async ({ page }) => {
  await signIn(page);
  await page.getByRole('link', { name: 'Review setup' }).click();
  await expect(page.getByText('Return to setup', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Continue setup' }).click();
  await expect(page.getByRole('heading', { name: 'Prepare your studio' })).toBeVisible();
  await page.getByRole('button', { name: 'Close setup checklist' }).click();
  await page.getByRole('button', { name: /Dark mode|Light mode/ }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});

test('editorial workspace visual baseline', async ({ page }) => {
  await signIn(page);
  await expect(page).toHaveScreenshot('workspace.png', {
    fullPage: true,
    animations: 'disabled',
    maxDiffPixelRatio: 0.12
  });
});

test('voice recording can be previewed, normalized, saved, and played', async ({ page, browserName }) => {
  test.skip(browserName !== 'chromium', 'Chromium provides a deterministic fake microphone for this media integration test.');
  await signIn(page);
  await page.getByRole('link', { name: 'Voices' }).click();
  await page.getByLabel('New voice name').fill('Browser recorder');
  await page.getByRole('button', { name: 'Add voice' }).click();
  await expect(page.getByRole('heading', { name: 'Browser recorder' })).toBeVisible();

  await page.getByRole('button', { name: 'Enable microphone' }).click();
  await expect(page.getByRole('button', { name: 'Record', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Record', exact: true }).click();
  await page.waitForTimeout(1_000);
  await page.getByRole('button', { name: /Stop ·/ }).click();
  await expect(page.getByRole('button', { name: 'Play recording' })).toBeVisible();

  await page.getByRole('button', { name: 'Play recording' }).click();
  await expect(page.getByRole('button', { name: 'Stop recording playback' })).toBeVisible();
  await page.getByRole('button', { name: 'Stop recording playback' }).click();
  await page.getByRole('button', { name: 'Save sample' }).click();

  await expect(page.getByRole('button', { name: 'Play sample' })).toBeVisible({ timeout: 20_000 });
  await page.getByRole('button', { name: 'Play sample' }).click();
  await expect(page.getByRole('button', { name: 'Stop sample playback' })).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
});
