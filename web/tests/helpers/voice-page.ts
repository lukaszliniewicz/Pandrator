import { expect, type Page } from '@playwright/test';

// The voice page loads its setup card and each settings panel separately, and
// each grows when its data arrives, moving everything below it. A pointer
// action started before then can land on whatever moved into its place.
export async function waitForVoicePageSettled(page: Page) {
  await expect(page.locator('.settings-panel')).toHaveCount(3);
  await expect(page.locator('.settings-panel[aria-busy="true"]')).toHaveCount(
    0
  );
  await expect(
    page.getByText('Loading voice setup…', { exact: true })
  ).toHaveCount(0);
}

export async function openVoicePage(page: Page, sessionId: string) {
  await page.goto(`/sessions/${sessionId}/voice`);
  await waitForVoicePageSettled(page);
}
