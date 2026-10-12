import type { Page } from '@playwright/test';

export async function showGenerationTextLayer(
  page: Page,
  layer: 'display' | 'speech'
) {
  await page
    .getByRole('button', { name: 'Display options', exact: true })
    .click();
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
