import { expect, type Page } from '@playwright/test';
import type { VoiceRecord } from '../../src/lib/api-models';

export function gate() {
  let release!: () => void;
  const ready = new Promise<void>((resolve) => (release = resolve));
  return { ready, release };
}

export async function selectVoice(page: Page, name: string) {
  const button = page.getByRole('button', { name });
  if (!(await button.isVisible())) {
    const selector = page.getByRole('button', {
      name: 'Choose another voice',
      exact: true
    });
    if (await selector.isVisible()) await selector.click();
    await page
      .locator('summary')
      .filter({ hasText: 'Add a sample to an existing voice' })
      .click();
  }
  await button.click();
}

export async function voiceReferenceFixture(page: Page, reviewed = true) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(
    page.getByRole('button', { name: 'New session', exact: true })
  ).toBeVisible();
  const closeTour = page.getByRole('button', { name: 'Close tour' });
  if (await closeTour.isVisible()) await closeTour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const voices = [];
  for (const prefix of ['Draft voice A', 'Draft voice B']) {
    const response = await page.request.post('/api/v1/voices', {
      headers: { 'X-CSRF-Token': auth.csrf_token },
      data: { name: `${prefix} ${crypto.randomUUID()}`, language: 'en' }
    });
    expect(response.ok()).toBeTruthy();
    voices.push((await response.json()) as VoiceRecord);
  }
  const [a, b] = voices;
  let samples = [
    {
      id: 'first',
      artifact_id: 'first-audio',
      transcript: 'First saved',
      transcript_reviewed: reviewed,
      available: true
    },
    {
      id: 'second',
      artifact_id: 'second-audio',
      transcript: 'Second saved',
      transcript_reviewed: reviewed,
      available: true
    }
  ];
  await page.route('**/api/v1/services/tts**', (route) =>
    route.fulfill({ json: { default_service: '', services: [] } })
  );
  await page.route(`**/api/v1/voices/${a.id}/samples`, (route) =>
    route.fulfill({ json: { items: samples } })
  );
  await page.route(`**/api/v1/voices/${b.id}/samples`, (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: 'other',
            artifact_id: 'other-audio',
            transcript: 'Other voice saved',
            transcript_reviewed: true,
            available: true
          }
        ]
      }
    })
  );
  await page.route('**/api/v1/capabilities', (route) =>
    route.fulfill({
      json: {
        stt: {
          crispasr: true,
          models: { parakeet: { installed: true, supported_languages: ['en'] } }
        },
        ffmpeg: { available: true }
      }
    })
  );
  await page.goto('/voices');
  await page
    .getByRole('button', { name: 'Add reference', exact: true })
    .click();
  await selectVoice(page, a.name);
  await expect(
    page.locator(
      'textarea[placeholder="Transcript will remain unsaved until you review it."]'
    )
  ).toHaveCount(2);
  const rows = page
    .locator('article')
    .filter({ has: page.getByRole('button', { name: 'Delete voice sample' }) });
  await rows.nth(1).locator('summary').click();
  const second = rows.nth(1).locator('textarea');
  return {
    a,
    b,
    rows,
    second,
    samples,
    setSamples: (items: typeof samples) => (samples = items)
  };
}
