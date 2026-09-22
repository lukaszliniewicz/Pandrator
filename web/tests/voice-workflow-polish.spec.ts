import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

async function login(page: Page) {
  await page.goto('/voices');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(
    page.getByRole('heading', { name: 'Voice library', exact: true })
  ).toBeVisible();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  return { 'X-CSRF-Token': auth.csrf_token };
}

const profile = {
  schema_version: 1,
  pitch: 'low',
  perceived_age: 'older',
  textures: ['warm'],
  delivery_presets: ['storytelling'],
  use_cases: ['character_dialogue'],
  languages: [
    {
      language: 'en',
      accent: 'Scottish',
      evidence: { source: 'user', status: 'described' }
    }
  ],
  tags: ['winter'],
  evidence: {}
};
const service = {
  id: 'audio_cpp',
  name: 'audio.cpp',
  adapter: 'audio_cpp',
  available: true,
  models: ['qwen3_tts_1_7b_voicedesign_q8_0', 'clone-model'],
  model_catalog: [],
  supports_voice_cloning: true
};

function catalogVoice(id: string, name: string) {
  return {
    key: `managed:${id}`,
    reference: { kind: 'managed', voice_id: id },
    kind: 'managed',
    id,
    name,
    description: 'Warm Scottish character narration.',
    voice_category: 'male',
    profile,
    revision: 1,
    origin: 'designed',
    collections: [],
    sample_count: 1,
    preview_artifact_id: `sample-${id}`,
    compatibility: [
      {
        service_id: 'audio_cpp',
        model: 'clone-model',
        status: 'ready',
        ready: true,
        voice: id,
        supported_languages: ['en'],
        modes: {
          cloning: true,
          design: false,
          reference_with_instructions: true
        }
      }
    ]
  };
}

async function mockVoices(page: Page) {
  const voices = [
    catalogVoice('scrooge', 'Scrooge'),
    catalogVoice('narrator', 'Narrator')
  ];
  const wav = Buffer.alloc(16044);
  wav.write('RIFF');
  wav.writeUInt32LE(wav.length - 8, 4);
  wav.write('WAVEfmt ', 8);
  wav.writeUInt32LE(16, 16);
  wav.writeUInt16LE(1, 20);
  wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(8000, 24);
  wav.writeUInt32LE(16000, 28);
  wav.writeUInt16LE(2, 32);
  wav.writeUInt16LE(16, 34);
  wav.write('data', 36);
  wav.writeUInt32LE(16000, 40);
  await page.route('**/api/v1/artifacts/*/content', (route) =>
    route.fulfill({ contentType: 'audio/wav', body: wav })
  );
  await page.route('**/api/v1/services/tts*', (route) =>
    route.fulfill({ json: { services: [service] } })
  );
  await page.route('**/api/v1/voice-catalog?**', (route) => {
    const params = new URL(route.request().url()).searchParams;
    const query = params.get('query')?.toLowerCase() ?? '';
    const items = voices.filter((voice) =>
      `${voice.id} ${voice.name}`.toLowerCase().includes(query)
    );
    return route.fulfill({
      json: {
        items,
        total: items.length,
        next_cursor: null,
        facets: { language: { en: 2 } },
        taxonomy: {
          perceived_age: ['older'],
          delivery_presets: ['storytelling']
        }
      }
    });
  });
  return voices;
}

test('designer protects the brief and keeps actions reachable on a phone', async ({
  page
}) => {
  await mockVoices(page);
  await login(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Design voice', exact: true }).click();
  const designer = page.getByRole('dialog', {
    name: 'Design a reusable voice'
  });
  await designer
    .getByLabel('Voice name', { exact: true })
    .fill('Scottish Scrooge');
  await designer
    .getByLabel('Voice description', { exact: false })
    .fill('Older male, dry Scottish accent, low and gravelly.');
  await designer
    .getByLabel('Sample text', { exact: false })
    .fill('Bah, humbug!');
  await page.keyboard.press('Escape');
  const guard = page.getByRole('dialog', { name: 'Keep your voice design?' });
  await expect(guard).toBeVisible();
  const keepBox = await guard
    .getByRole('button', { name: 'Keep editing' })
    .boundingBox();
  const discardBox = await guard
    .getByRole('button', { name: 'Discard design' })
    .boundingBox();
  expect(keepBox!.height).toBeGreaterThanOrEqual(44);
  expect(keepBox!.height).toBe(discardBox!.height);
  expect(keepBox!.y).toBe(discardBox!.y);
  expect((await guard.boundingBox())!.height).toBeLessThan(300);
  await page.setViewportSize({ width: 300, height: 649 });
  const narrowButtons = await guard
    .getByRole('button')
    .evaluateAll((buttons) =>
      buttons.map((button) => button.getBoundingClientRect().height)
    );
  expect(narrowButtons[0]).toBe(narrowButtons[1]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth
    )
  ).toBeTruthy();
  await page.setViewportSize({ width: 390, height: 844 });
  await guard.getByRole('button', { name: 'Keep editing' }).click();
  await expect(designer.getByLabel('Voice name', { exact: true })).toHaveValue(
    'Scottish Scrooge'
  );
  const next = designer.getByRole('button', { name: 'Continue to auditions' });
  const box = await next.boundingBox();
  expect(box!.y + box!.height).toBeLessThanOrEqual(844);
  await next.click();
  await expect(
    designer.getByRole('button', { name: 'Generate 3 candidates' })
  ).toBeVisible();
  await expect(
    designer.getByRole('button', { name: 'Save selected voice' })
  ).toBeDisabled();
  await designer.getByRole('button', { name: '1. Voice brief' }).click();
  await expect(
    designer.getByLabel('Sample text', { exact: false })
  ).toHaveValue('Bah, humbug!');
  await page.keyboard.press('Escape');
  await guard.getByRole('button', { name: 'Discard design' }).click();
  await expect(designer).toBeHidden();
  await page.getByRole('button', { name: 'Design voice', exact: true }).click();
  await expect(designer.getByLabel('Voice name', { exact: true })).toHaveValue(
    ''
  );
});

test('canceling voice design keeps the brief and completed candidates', async ({
  page
}) => {
  await mockVoices(page);
  await login(page);
  let submitted = 0;
  let canceled = false;
  await page.route('**/api/v1/services/tts/audio_cpp/preview', (route) => {
    submitted++;
    return route.fulfill({
      json: { id: `design-${submitted}`, status: 'queued' }
    });
  });
  await page.route('**/api/v1/jobs/design-1', (route) =>
    route.fulfill({
      json: {
        id: 'design-1',
        status: 'succeeded',
        result_json: { artifact_id: 'design-result-1' }
      }
    })
  );
  await page.route('**/api/v1/jobs/design-2', (route) =>
    route.fulfill({
      json: {
        id: 'design-2',
        status: canceled ? 'canceled' : 'running',
        progress_detail: 'Generating second candidate'
      }
    })
  );
  await page.route('**/api/v1/jobs/design-2/cancel', (route) => {
    canceled = true;
    return route.fulfill({ json: { id: 'design-2', status: 'canceled' } });
  });
  await page.getByRole('button', { name: 'Design voice', exact: true }).click();
  const designer = page.getByRole('dialog', {
    name: 'Design a reusable voice'
  });
  await designer
    .getByLabel('Voice name', { exact: true })
    .fill('Scottish Scrooge');
  await designer
    .getByLabel('Voice description', { exact: false })
    .fill('Older, dry Scottish accent, low and gravelly.');
  await designer.getByRole('button', { name: 'Continue to auditions' }).click();
  await designer.getByRole('button', { name: 'Generate 3 candidates' }).click();
  await expect(
    designer.getByText('Generating second candidate', { exact: true }).first()
  ).toBeVisible();
  await designer
    .getByRole('button', { name: 'Cancel generation', exact: true })
    .click();
  await expect(
    designer.getByRole('button', { name: 'Save selected voice', exact: true })
  ).toBeEnabled();
  await expect(
    designer.getByRole('radio', { name: /Candidate 1/ })
  ).toBeChecked();
  await expect(
    page.getByRole('dialog', { name: 'Keep your voice design?' })
  ).toBeHidden();
  expect(submitted).toBe(2);
  expect(canceled).toBe(true);
  await expect(
    designer.getByText('Audition stopped. 1 candidate ready to save.', {
      exact: true
    })
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: '/tmp/pandrator-polish-designer-canceled.png'
  });
  await designer.getByRole('button', { name: '1. Voice brief' }).click();
  await expect(designer.getByLabel('Voice name', { exact: true })).toHaveValue(
    'Scottish Scrooge'
  );
});

test('source scope, filters and model variants remain explicit', async ({
  page
}) => {
  await login(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole('button', { name: 'Saved voices', exact: true })
  ).toHaveAttribute('aria-pressed', 'true');
  await page
    .getByRole('button', { name: 'Provider catalog', exact: true })
    .click();
  await expect(
    page.getByRole('button', { name: 'Filters (1)', exact: true })
  ).toBeVisible();
  await page.getByLabel('Search voices', { exact: true }).fill('Achernar');
  await expect(
    page.getByRole('button', { name: 'Achernar', exact: true })
  ).toHaveCount(1);
  const model = page.getByRole('combobox', { name: /^Model for Achernar/ });
  await expect(model.locator('option')).toHaveCount(6);
  await model.selectOption({ index: 1 });
  await page.getByRole('button', { name: 'Details', exact: true }).click();
  await expect(
    page.getByRole('heading', { name: 'Achernar', exact: true })
  ).toBeVisible();
  await expect(page.getByText(/gemini-2.5-pro-tts/).first()).toBeVisible();
  await page
    .getByRole('button', { name: 'Back to voices', exact: true })
    .click();
  await page.getByRole('button', { name: 'Filters (1)', exact: true }).click();
  await page
    .getByRole('dialog', { name: 'Voice filters' })
    .getByRole('button', { name: 'Clear filters', exact: true })
    .click();
  await page
    .getByRole('button', { name: 'Close filters', exact: true })
    .click();
  await expect(
    page.getByRole('button', { name: 'Saved voices', exact: true })
  ).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByLabel('Search voices', { exact: true })).toHaveValue(
    ''
  );
  await expect(
    page.getByRole('button', { name: 'Filters', exact: true })
  ).toBeVisible();
  await expect(page.getByRole('main')).toHaveCount(1);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth
    )
  ).toBeTruthy();
});

test('comparison has its own renderer, reference playback, and per-voice results', async ({
  page
}) => {
  await mockVoices(page);
  await login(page);
  await page
    .getByRole('checkbox', { name: 'Compare Scrooge', exact: true })
    .check();
  await page
    .getByRole('checkbox', { name: 'Compare Narrator', exact: true })
    .check();
  await page
    .getByRole('button', { name: 'Compare 2 voices', exact: true })
    .click();
  const comparison = page.getByRole('dialog', {
    name: 'Compare voices',
    exact: true
  });
  await expect(
    comparison.getByText('Existing reference · original recording')
  ).toHaveCount(2);
  await comparison.getByLabel('Audition service').selectOption('audio_cpp');
  await comparison.getByLabel('Audition model').selectOption('clone-model');
  await expect(
    comparison.getByRole('button', { name: 'Generate 2 auditions' })
  ).toBeEnabled();
  let calls = 0;
  await page.route(
    '**/api/v1/services/tts/audio_cpp/preview',
    async (route) => {
      const body = route.request().postDataJSON();
      expect(body.model).toBe('clone-model');
      calls++;
      if (body.voice === 'scrooge')
        return route.fulfill({
          status: 503,
          json: {
            error: {
              code: 'voice_unavailable',
              message: 'Scrooge preview failed'
            }
          }
        });
      return route.fulfill({
        json: { id: 'audition-success', status: 'queued' }
      });
    }
  );
  await page.route('**/api/v1/jobs/audition-success', (route) =>
    route.fulfill({
      json: {
        id: 'audition-success',
        status: 'succeeded',
        result_json: { artifact_id: 'audition-result' }
      }
    })
  );
  await comparison
    .getByRole('button', { name: 'Generate 2 auditions' })
    .click();
  await expect(
    comparison.getByRole('alert').filter({ hasText: 'Scrooge preview failed' })
  ).toBeVisible();
  await expect(
    comparison.getByText('Shared passage audition', { exact: true })
  ).toBeVisible();
  expect(calls).toBe(2);
  await comparison
    .getByRole('button', { name: 'Close comparison', exact: true })
    .click();
  await page.getByText('Renderer compatibility', { exact: true }).click();
  await expect(
    page.getByRole('combobox', { name: 'Service', exact: true })
  ).toHaveValue('');
  await page
    .getByRole('button', { name: 'Compare 2 voices', exact: true })
    .click();
  await expect(
    comparison.getByText('Shared passage audition', { exact: true })
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth
    )
  ).toBeTruthy();
  await page.screenshot({ path: '/tmp/pandrator-polish-comparison-390.png' });
});

test('collections support rename and bulk add and remove without deleting voices', async ({
  page
}) => {
  const headers = await login(page);
  const suffix = crypto.randomUUID().slice(0, 8);
  const name = `Carol voice ${suffix}`;
  const voice = await (
    await page.request.post('/api/v1/voices', {
      headers,
      data: { name, profile }
    })
  ).json();
  await page.getByLabel('Search voices').fill(name);
  await expect(page.getByRole('button', { name, exact: true })).toBeVisible();
  await page
    .getByRole('button', { name: 'Create collection', exact: true })
    .click();
  await page
    .getByLabel('Collection name', { exact: true })
    .fill(`Carol ${suffix}`);
  await page
    .locator('form')
    .getByRole('button', { name: 'Create collection', exact: true })
    .click();
  await page
    .getByRole('button', { name: 'Rename collection', exact: true })
    .click();
  await page
    .getByLabel('Collection name', { exact: true })
    .fill(`Christmas Carol ${suffix}`);
  await page
    .getByRole('button', { name: 'Save collection name', exact: true })
    .click();
  await expect(
    page.getByRole('combobox', { name: 'Collection', exact: true })
  ).toContainText(`Christmas Carol ${suffix} (0)`);
  await page
    .getByRole('combobox', { name: 'Collection', exact: true })
    .selectOption('');
  await page
    .getByRole('button', { name: 'Organize voices', exact: true })
    .click();
  await page
    .getByLabel(`Select ${name} for collection`, { exact: true })
    .check();
  await page
    .getByLabel('Destination collection')
    .selectOption({ label: `Christmas Carol ${suffix}` });
  await page.getByRole('button', { name: 'Add selected', exact: true }).click();
  await expect(
    page.getByRole('combobox', { name: 'Collection', exact: true })
  ).toContainText(`Christmas Carol ${suffix} (1)`);
  await page
    .getByLabel(`Select ${name} for collection`, { exact: true })
    .check();
  await page
    .getByRole('button', { name: 'Remove selected', exact: true })
    .click();
  await expect(
    page.getByRole('combobox', { name: 'Collection', exact: true })
  ).toContainText(`Christmas Carol ${suffix} (0)`);
  const library = await (await page.request.get('/api/v1/voices')).json();
  expect(
    library.items.some((item: { id: string }) => item.id === voice.id)
  ).toBe(true);
});

test('voice page exposes a compact cast, inheritance and a navigation guard', async ({
  page
}) => {
  const headers = await login(page);
  const session = await (
    await page.request.post('/api/v1/sessions', {
      headers,
      data: {
        name: `Christmas Carol UI review ${crypto.randomUUID().slice(0, 8)}`,
        workflow_kind: 'audiobook'
      }
    })
  ).json();
  const controls = await (
    await page.request.get(`/api/v1/sessions/${session.id}/generation-controls`)
  ).json();
  const result = await page.request.put(
    `/api/v1/sessions/${session.id}/generation-controls`,
    {
      headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() },
      data: {
        expected_revision: controls.revision,
        characters: [
          {
            id: 'scrooge',
            display_name: 'Scrooge',
            voice_category: 'male',
            aliases: ['Ebenezer'],
            notes: '',
            locked: false,
            status: 'accepted',
            origin: 'manual'
          }
        ],
        cast: {
          narrator: { voice: 'Northern narrator' },
          categories: {},
          characters: {},
          source_speakers: {}
        }
      }
    }
  );
  expect(result.ok(), await result.text()).toBeTruthy();
  await page.goto(`/sessions/${session.id}/voice`);
  const cast = page.locator('#characters-cast');
  await expect(
    cast.getByRole('heading', { name: 'Scrooge', exact: true })
  ).toBeVisible();
  await expect(
    cast.getByText('Inherited · Narrator', { exact: true }).first()
  ).toBeVisible();
  await expect(cast.getByLabel('Name', { exact: true })).toBeHidden();
  await cast
    .getByText('Edit identity, aliases & protection', { exact: true })
    .click();
  await cast.getByLabel('Name', { exact: true }).fill('Ebenezer Scrooge');
  await page.getByRole('link', { name: 'Voices', exact: true }).click();
  const guard = page.getByRole('dialog', { name: 'Keep your cast changes?' });
  await expect(guard).toBeVisible();
  await guard.getByRole('button', { name: 'Keep editing' }).click();
  await expect(cast.getByLabel('Name', { exact: true })).toHaveValue(
    'Ebenezer Scrooge'
  );
  await cast
    .getByRole('button', { name: 'Save characters and cast', exact: true })
    .click();
  await expect(cast.getByText(/Characters and cast saved/)).toBeVisible();
  const saved = await (
    await page.request.get(`/api/v1/sessions/${session.id}/generation-controls`)
  ).json();
  expect(saved.characters[0].display_name).toBe('Ebenezer Scrooge');
  expect(
    (
      await new AxeBuilder({ page })
        .include('#characters-cast')
        .withTags(['wcag2a', 'wcag2aa'])
        .analyze()
    ).violations
  ).toEqual([]);
  await cast
    .getByText('Edit identity, aliases & protection', { exact: true })
    .click();
  for (const width of [390, 800, 1440]) {
    await page.setViewportSize({ width, height: 960 });
    await cast.screenshot({ path: `/tmp/pandrator-polish-cast-${width}.png` });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth
      )
    ).toBeTruthy();
  }
});

test('reference selection reveals the tools without the library list above them', async ({
  page
}) => {
  const headers = await login(page);
  const name = `Carol reference ${crypto.randomUUID().slice(0, 8)}`;
  const result = await page.request.post('/api/v1/voices', {
    headers,
    data: { name, language: 'en' }
  });
  expect(result.ok(), await result.text()).toBeTruthy();
  await page.setViewportSize({ width: 390, height: 844 });
  await page
    .getByRole('button', { name: 'Add reference', exact: true })
    .click();
  await page
    .getByText('Add a sample to an existing voice', { exact: true })
    .click();
  await page
    .getByRole('searchbox', { name: 'Find a reference voice', exact: true })
    .fill(name);
  await page
    .getByRole('button', { name: `${name} English`, exact: true })
    .click();
  await expect(
    page.getByRole('searchbox', { name: 'Find a reference voice', exact: true })
  ).toBeHidden();
  await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Choose another voice', exact: true })
  ).toBeVisible();
  await expect(
    page.getByRole('button', { name: 'Upload sample', exact: true }).first()
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth
    )
  ).toBeTruthy();
});

test('phone generation controls and output actions stay inside their cards', async ({
  page
}) => {
  const headers = await login(page);
  const session = await (
    await page.request.post('/api/v1/sessions', {
      headers,
      data: {
        name: `A Christmas Carol — chapter one ${crypto.randomUUID().slice(0, 8)}`,
        workflow_kind: 'audiobook'
      }
    })
  ).json();
  const filename =
    'A Christmas Carol — chapter one — Scottish Scrooge and the complete ensemble.wav';
  await page.route(
    `**/api/v1/sessions/${session.id}/generation-segments?**`,
    (route) =>
      route.fulfill({
        json: {
          items: [],
          total: 132,
          next_cursor: null,
          plan_revision_id: 'review-plan'
        }
      })
  );
  await page.route('**/api/v1/artifacts?**', (route) => {
    if (!new URL(route.request().url()).searchParams.has('output_only'))
      return route.fallback();
    return route.fulfill({
      json: {
        items: [
          {
            id: 'review-output',
            session_id: session.id,
            role: 'export',
            kind: 'audio',
            mime_type: 'audio/wav',
            path: `/test-outputs/${filename}`,
            size_bytes: 8350928,
            created_at: '2026-09-20T12:00:00Z',
            metadata_json: {}
          }
        ]
      }
    });
  });
  await page.goto(`/sessions/${session.id}/output`);
  const drawer = page.locator('[data-generation-layout="collapsed"]');
  await expect(
    drawer.getByText('132 segments · Active mix', { exact: true })
  ).toBeVisible();
  const output = page.locator('article').filter({
    has: page.getByRole('button', {
      name: `Preview ${filename}`,
      exact: true
    })
  });
  for (const width of [360, 390, 430]) {
    await page.setViewportSize({ width, height: 844 });
    await expect(drawer).toBeVisible();
    const bounds = await drawer.boundingBox();
    for (const name of ['Generation', 'Play playlist', 'Generate audio…']) {
      const button = await drawer
        .getByRole('button', { name, exact: true })
        .boundingBox();
      expect(button!.height).toBeGreaterThanOrEqual(44);
      expect(button!.y).toBeGreaterThanOrEqual(bounds!.y);
      expect(button!.y + button!.height).toBeLessThanOrEqual(
        bounds!.y + bounds!.height
      );
      expect(button!.x + button!.width).toBeLessThanOrEqual(width);
    }
    await output.scrollIntoViewIfNeeded();
    const title = await output.locator('strong').boundingBox();
    expect(title!.width).toBeGreaterThan(width - 170);
    for (const label of [
      `Preview ${filename}`,
      `Copy absolute path for ${filename}`,
      `Remove export ${filename}`
    ]) {
      const button = await output
        .getByRole('button', { name: label, exact: true })
        .boundingBox();
      expect(button!.height).toBeGreaterThanOrEqual(44);
      expect(button!.y).toBeGreaterThan(title!.y + title!.height);
    }
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth
      )
    ).toBeTruthy();
    await page.screenshot({
      path: `/tmp/pandrator-polish-output-${width}.png`,
      fullPage: true
    });
  }
});

test('collapsed navigation retains accessible control names', async ({
  page
}) => {
  await login(page);
  await page
    .getByRole('button', { name: 'Collapse sidebar', exact: true })
    .click();
  const sidebar = page.getByRole('complementary');
  await expect(
    sidebar.getByRole('button', { name: 'Expand sidebar', exact: true })
  ).toBeVisible();
  await expect(
    sidebar.getByRole('button', { name: /^(Light|Dark) mode$/ })
  ).toBeVisible();
  await expect(
    sidebar.getByRole('button', { name: 'Sign out', exact: true })
  ).toBeVisible();
  expect(
    (
      await new AxeBuilder({ page })
        .include('aside.app-sidebar')
        .withTags(['wcag2a', 'wcag2aa'])
        .analyze()
    ).violations
  ).toEqual([]);
});
