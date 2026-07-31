import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
}

async function expectNoBlockingViolations(page: Page) {
  const result = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
    .analyze();
  const blocking = result.violations
    .filter((violation) =>
      ['serious', 'critical'].includes(violation.impact ?? '')
    )
    .map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      description: violation.description,
      targets: violation.nodes.map((node) => node.target.join(' '))
    }));
  expect(blocking).toEqual([]);
}

test('core authenticated surfaces have no serious or critical WCAG violations', async ({
  page
}) => {
  test.setTimeout(90_000);
  await signIn(page);
  for (const route of [
    '/',
    '/sessions',
    '/providers',
    '/settings',
    '/pronunciations'
  ]) {
    await page.goto(route);
    await expect(page.locator('main')).toBeVisible();
    await expectNoBlockingViolations(page);
  }
});

test('modal focus enters, remains contained, closes with Escape, and returns to its opener', async ({
  page
}) => {
  await signIn(page);
  const opener = page.getByRole('button', { name: 'New session' });
  await opener.focus();
  await opener.click();

  const dialog = page.getByRole('dialog', {
    name: /What would you like to make/i
  });
  await expect(dialog).toBeVisible();
  await expect
    .poll(() =>
      dialog.evaluate((node) => node.contains(document.activeElement))
    )
    .toBe(true);
  await expectNoBlockingViolations(page);

  const focusable = dialog.locator(
    'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  );
  const first = focusable.first();
  const last = focusable.last();
  await first.focus();
  await page.keyboard.press('Shift+Tab');
  await expect(last).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(first).toBeFocused();

  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
});

test('setup checklist is exposed as a keyboard-operable dialog', async ({
  page
}) => {
  await signIn(page);
  await page.goto('/?setup=1');
  const dialog = page.getByRole('dialog', { name: 'Prepare your studio' });
  await expect(dialog).toBeVisible();
  await expectNoBlockingViolations(page);
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
});
