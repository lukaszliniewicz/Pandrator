import { expect, test } from '@playwright/test';

test('the shell uses the web-sized Pandrator mark and matching favicon', async ({
  page
}) => {
  await page.goto('/');

  await expect(page.locator('link[rel="icon"]')).toHaveAttribute(
    'href',
    /\/favicon-32\.png$/
  );
  const logo = page.getByRole('img', { name: 'Pandrator' });
  await expect(logo).toHaveAttribute('src', /\/pandrator-logo\.webp$/);
  await expect(logo).toHaveAttribute('width', '128');
  await expect(logo).toHaveAttribute('height', '128');

  const logoResponse = await page.request.get('/pandrator-logo.webp');
  expect(logoResponse.ok()).toBeTruthy();
  expect(logoResponse.headers()['content-type']).toContain('image/webp');
  expect((await logoResponse.body()).byteLength).toBeLessThan(20_000);
});
