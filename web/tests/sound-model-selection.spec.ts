import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import type {
  SoundCandidate,
  SoundCatalogue,
  SoundGeneration,
  SoundModel
} from '../src/lib/sound-api';

const soundModel: SoundModel = {
  id: 'stable_audio_3_small_sfx_q8_0',
  label: 'Sound effects model',
  family: 'stable_audio',
  cached: true,
  kinds: ['sound'],
  description: 'Sound effects only',
  backends: ['cpu'],
  duration: { minimum: 1, maximum: 30, default: 4 },
  supports_lyrics: false,
  seed_maximum: 4294967295,
  download_bytes: 0,
  available: true,
  parameters: {}
};
const musicModel: SoundModel = {
  ...soundModel,
  id: 'stable_audio_3_small_music_q8_0',
  label: 'Music model',
  kinds: ['music'],
  description: 'Music only',
  duration: { minimum: 1, maximum: 90, default: 12 }
};

async function fixture(
  page: Page,
  {
    musicAvailable = false,
    delayCatalogue = false,
    candidates = []
  }: {
    musicAvailable?: boolean;
    delayCatalogue?: boolean;
    candidates?: SoundCandidate[];
  } = {}
) {
  const errors: string[] = [];
  const requests: SoundGeneration[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(
    page.getByRole('heading', { name: 'Create a project', exact: true })
  ).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const catalogue: SoundCatalogue = {
    runtime: { available: true, version: '0.9.1' },
    models: [soundModel, { ...musicModel, available: musicAvailable }],
    defaults: { model_id: soundModel.id, duration_seconds: 4, threads: 4 }
  };
  let releaseCatalogue = () => {};
  const catalogueReady = delayCatalogue
    ? new Promise<void>((resolve) => {
        releaseCatalogue = resolve;
      })
    : Promise.resolve();
  await page.route('**/api/v1/sounds/catalogue', async (route) => {
    await catalogueReady;
    await route.fulfill({ json: catalogue });
  });
  await page.route('**/api/v1/sounds/candidates?*', (route) =>
    route.fulfill({ json: { items: candidates, next_cursor: null } })
  );
  await page.route('**/api/v1/sounds/candidates/*', (route) =>
    route.fulfill({
      json: candidates.find((item) => route.request().url().endsWith(item.id))
    })
  );
  await page.route('**/api/v1/sounds/generate', (route) => {
    requests.push(route.request().postDataJSON() as SoundGeneration);
    return route.fulfill({
      status: 400,
      json: {
        code: 'fixture_request',
        message: 'Request recorded by the test.'
      }
    });
  });
  await page.goto('/sounds');
  await expect(
    page.getByRole('combobox', { name: 'Create', exact: true })
  ).toBeVisible();
  if (!delayCatalogue)
    await expect(
      page.getByRole('combobox', { name: 'Model', exact: true })
    ).toHaveValue(soundModel.id);
  return { errors, requests, releaseCatalogue };
}

for (const width of [1280, 390]) {
  test(`an unavailable music choice clears sound settings and cannot submit a sound-only model at ${width}px`, async ({
    page
  }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const f = await fixture(page);
    const create = page.getByRole('combobox', { name: 'Create', exact: true });
    const model = page.getByRole('combobox', { name: 'Model', exact: true });
    const prompt = page.getByLabel('Description', { exact: true });
    await create.selectOption('music');
    await prompt.fill('A quiet instrumental piano intro.');
    await expect(model).toHaveValue('');
    await expect(model).toBeDisabled();
    await expect(
      page.getByText('No models are available for music.', { exact: true })
    ).toBeVisible();
    await expect(
      page.getByLabel('Duration (seconds)', { exact: true })
    ).toBeDisabled();
    await expect(
      page.getByRole('button', { name: 'Generate', exact: true })
    ).toBeDisabled();
    await expect(
      page.getByText('Length: 1–30 seconds.', { exact: true })
    ).not.toBeVisible();
    expect(
      await page.evaluate(
        () =>
          document.documentElement.scrollWidth <=
          document.documentElement.clientWidth
      )
    ).toBe(true);
    const accessibility = await new AxeBuilder({ page }).analyze();
    expect(
      accessibility.violations.filter((violation) =>
        ['serious', 'critical'].includes(violation.impact ?? '')
      )
    ).toEqual([]);
    await page.screenshot({
      path: info.outputPath('unavailable-music.png'),
      fullPage: true
    });
    // The submit handler must guard programmatic submissions as well as clicks.
    await page.locator('form').first().dispatchEvent('submit');
    await expect(page.getByRole('alert')).toContainText(
      'Choose an available model and backend'
    );
    expect(f.requests).toEqual([]);

    await create.selectOption('sound');
    await expect(model).toHaveValue(soundModel.id);
    await expect(prompt).toHaveValue('A quiet instrumental piano intro.');
    await expect(
      page.getByLabel('Duration (seconds)', { exact: true })
    ).toHaveValue('4');
    await page.getByRole('button', { name: 'Generate', exact: true }).click();
    await expect.poll(() => f.requests.length).toBe(1);
    expect(f.requests[0]).toMatchObject({
      kind: 'sound',
      model_id: soundModel.id,
      parameters: {}
    });
    expect(f.errors).toEqual([]);
  });
}

test('a late catalogue response preserves the selected kind and chooses its compatible model', async ({
  page
}) => {
  const f = await fixture(page, { musicAvailable: true, delayCatalogue: true });
  try {
    await page
      .getByRole('combobox', { name: 'Create', exact: true })
      .selectOption('music');
    await page
      .getByLabel('Description', { exact: true })
      .fill('A slow piano melody.');
    f.releaseCatalogue();
    await expect(
      page.getByRole('combobox', { name: 'Model', exact: true })
    ).toHaveValue(musicModel.id);
    await expect(
      page.getByLabel('Duration (seconds)', { exact: true })
    ).toHaveValue('12');
    await page.getByRole('button', { name: 'Generate', exact: true }).click();
    await expect.poll(() => f.requests.length).toBe(1);
    expect(f.requests[0]).toMatchObject({
      kind: 'music',
      model_id: musicModel.id,
      duration_seconds: 12
    });
    expect(f.errors).toEqual([]);
  } finally {
    f.releaseCatalogue();
  }
});

test('reusing an incompatible take cannot submit until a compatible model is chosen', async ({
  page
}) => {
  const candidate: SoundCandidate = {
    id: 'incompatible-take',
    status: 'ready',
    job_id: null,
    artifact_id: null,
    item_id: null,
    duration_seconds: 4,
    sample_rate: 44100,
    channels: 1,
    created_at: '2026-10-12T00:00:00Z',
    provenance: {
      model_id: soundModel.id,
      kind: 'music',
      prompt: 'A recorded music description.',
      backend: 'cpu',
      parameters: { guidance_scale: 2 }
    }
  };
  const f = await fixture(page, {
    musicAvailable: true,
    candidates: [candidate]
  });
  await page.getByRole('button', { name: 'Use settings', exact: true }).click();
  const model = page.getByRole('combobox', { name: 'Model', exact: true });
  await expect(
    page.getByRole('combobox', { name: 'Create', exact: true })
  ).toHaveValue('music');
  await expect(model).toHaveValue('');
  await expect(
    page.getByRole('button', { name: 'Generate', exact: true })
  ).toBeDisabled();
  await page.locator('form').first().dispatchEvent('submit');
  await expect(page.getByRole('alert')).toContainText(
    'Choose an available model and backend'
  );
  expect(f.requests).toEqual([]);
  await model.selectOption(musicModel.id);
  await page.getByRole('button', { name: 'Generate', exact: true }).click();
  await expect.poll(() => f.requests.length).toBe(1);
  expect(f.requests[0]).toMatchObject({
    kind: 'music',
    model_id: musicModel.id,
    parameters: {}
  });
  expect(f.errors).toEqual([]);
});
