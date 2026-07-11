import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Open workspace' }).click();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

test('wizard creates a guided subtitle workspace and preserves setup return', async ({ page }) => {
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'Where would you like to begin?' })).toBeVisible();
  await page.getByRole('button', { name: /Create subtitles/ }).last().click();
  await page.getByLabel('Session name').fill('Playwright subtitles');
  await page.getByRole('button', { name: 'Later', exact: true }).click();
  await page.getByRole('button', { name: 'Create session' }).click();
  await expect(page.getByRole('heading', { name: 'Playwright subtitles' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Transcribe' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Tour' })).toBeVisible();
});

test('provider defaults and restartable tours are keyboard reachable', async ({ page }) => {
  await signIn(page);
  await page.getByRole('button', { name: 'Close wizard' }).click();
  await page.getByRole('button', { name: 'Providers', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'LLM models' })).toBeVisible();
  await page.getByRole('button', { name: 'Tour' }).click();
  await expect(page.getByRole('heading', { name: 'One record per canonical model' })).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(page.locator(':focus')).toBeVisible();
});

test('theme and setup dock remain available after navigation', async ({ page }) => {
  await signIn(page);
  await page.getByRole('button', { name: 'Close wizard' }).click();
  await page.getByRole('button', { name: 'Close setup checklist' }).click();
  await expect(page.getByRole('button', { name: 'Return to setup' })).toBeVisible();
  await page.getByRole('button', { name: /Dark mode|Light mode/ }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});

test('editorial workspace visual baseline', async ({ page }) => {
  await signIn(page);
  await page.getByRole('button', { name: 'Close wizard' }).click();
  await page.getByRole('button', { name: 'Close setup checklist' }).click();
  await expect(page).toHaveScreenshot('workspace.png', {
    fullPage: true,
    animations: 'disabled',
    maxDiffPixelRatio: 0.12
  });
});
