import { expect, type Page } from '@playwright/test';

export async function showGenerationTextLayer(
  page: Page,
  layer: 'display' | 'speech'
) {
  const options = page.getByRole('button', {
    name: 'Display options',
    exact: true
  });
  await options.click();
  // Fail here, not after the test timeout, if the click did not open it.
  await expect(options).toHaveAttribute('aria-expanded', 'true');
  await page
    .getByRole('button', {
      name:
        layer === 'display'
          ? 'Script / subtitle text'
          : 'Speech text · optimized where available',
      exact: true
    })
    .click();
}

export async function editGenerationWords(page: Page, ordinal: number) {
  await page
    .getByRole('button', {
      name: `Edit words for segment ${ordinal}`,
      exact: true
    })
    .click();
}
