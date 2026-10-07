import { expect, test, type Page } from '@playwright/test';
import type { SessionRecord } from '../src/lib/api-models';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
}

async function project(
  page: Page,
  name: string,
  kind = 'voiceover',
  language?: string
) {
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const response = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      name,
      workflow_kind: kind,
      source_language: 'en',
      target_language: language
    }
  });
  expect(response.ok()).toBeTruthy();
  return (await response.json()) as SessionRecord;
}

test('language versions form one project and switch output controls while linking shared sources', async ({
  page
}) => {
  await signIn(page);
  const source = await project(page, 'Library navigation source');
  const branch = await project(
    page,
    'Library navigation Japanese',
    'voiceover',
    'ja'
  );
  const book = await project(page, 'Library navigation audiobook', 'audiobook');
  const membership = {
    id: 'language-project',
    name: 'Library navigation languages',
    source_session_id: source.id
  };
  source.translation_project = { ...membership, role: 'source' };
  branch.translation_project = {
    ...membership,
    role: 'branch',
    target_language: 'ja'
  };
  const items = [source, branch, book];
  await page.route(/\/api\/v1\/sessions(?:\?[^/]*)?$/, (route) =>
    route.fulfill({ json: { items } })
  );
  await page.route('**/api/v1/events/snapshot?**', async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    await route.fulfill({ json: { ...payload, sessions: { items } } });
  });
  for (const item of [source, branch])
    await page.route(`**/api/v1/sessions/${item.id}`, (route) =>
      route.fulfill({ json: item })
    );
  await page.goto('/sessions');
  await expect(
    page.getByRole('heading', { name: 'Projects', exact: true })
  ).toBeVisible();
  await expect(
    page.getByText('One corrected source · 1 language version')
  ).toBeVisible();
  await expect(
    page.getByRole('link', { name: 'Library navigation Japanese', exact: true })
  ).toHaveCount(0);
  await page
    .getByRole('combobox', { name: 'Workflow', exact: true })
    .selectOption('audiobook');
  await expect(
    page.getByRole('link', { name: /Library navigation audiobook/ })
  ).toBeVisible();
  await expect(
    page.getByRole('link', {
      name: 'Library navigation languages',
      exact: true
    })
  ).toHaveCount(0);
  await page.goto(`/sessions/${source.id}/output`);
  await page
    .getByRole('combobox', { name: 'Project language', exact: true })
    .selectOption(branch.id);
  await expect(page).toHaveURL(`/sessions/${branch.id}/output`);
  await expect(
    page.getByText(/uses a pinned copy of the corrected source/)
  ).toBeVisible();
  await expect(page.getByLabel('Project language')).toHaveValue(branch.id);
  const nav = page.getByRole('navigation', { name: 'Project sections' });
  await expect(
    nav.getByRole('link', { name: 'Sources', exact: true })
  ).toHaveAttribute('href', `/sessions/${source.id}/sources`);
  await expect(
    nav.getByRole('link', { name: 'Languages', exact: true })
  ).toHaveAttribute('href', `/sessions/${source.id}/languages`);
  await nav.getByRole('link', { name: 'Sources', exact: true }).click();
  await expect(page).toHaveURL(`/sessions/${source.id}/sources`);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth
    )
  ).toBeTruthy();
});

test('source filters use formats and lazily expose project references', async ({
  page
}) => {
  await signIn(page);
  const stamp = new Date().toISOString();
  const common = {
    state: 'current',
    revision: 1,
    reference_count: 0,
    current_reference_count: 0,
    created_at: stamp,
    updated_at: stamp
  };
  const items = [
    {
      ...common,
      id: 'video',
      display_name: 'Interview.mp4',
      kind: 'mp4',
      mime_type: 'video/mp4',
      size_bytes: 15728640,
      reference_count: 1,
      current_reference_count: 1
    },
    {
      ...common,
      id: 'book',
      display_name: 'Chapter.epub',
      kind: 'epub',
      mime_type: 'application/epub+zip',
      size_bytes: 2048
    },
    {
      ...common,
      id: 'captions',
      display_name: 'Interview.srt',
      kind: 'srt',
      mime_type: 'text/plain',
      size_bytes: 730
    }
  ];
  let referencesRead = 0;
  await page.route('**/api/v1/sources?**', (route) => {
    expect(new URL(route.request().url()).searchParams.get('view')).toBe(
      'compact'
    );
    return route.fulfill({ json: { items } });
  });
  await page.route('**/api/v1/sources/video/references?**', (route) => {
    referencesRead++;
    return route.fulfill({
      json: {
        total: 1,
        offset: 0,
        next_offset: null,
        items: [
          {
            attachment_id: 'attachment',
            session_id: 'interview-project',
            session_name: 'Interview project',
            workflow_kind: 'voiceover',
            source_language: 'en',
            target_language: null,
            status: 'idle',
            role: 'primary',
            is_current: true,
            updated_at: stamp
          }
        ]
      }
    });
  });
  await page.goto('/sources');
  await expect(page.locator('article')).toHaveCount(3);
  expect(referencesRead).toBe(0);
  const video = page.locator('article').filter({ hasText: 'Interview.mp4' });
  await expect(video.getByRole('img', { name: 'Video source' })).toBeVisible();
  await page
    .getByRole('combobox', { name: 'Type', exact: true })
    .selectOption('video');
  await expect(page.locator('article')).toHaveCount(1);
  await video
    .getByRole('button', { name: 'Show projects using Interview.mp4' })
    .click();
  await expect(
    video.getByRole('link', { name: 'Interview project' })
  ).toHaveAttribute('href', '/sessions/interview-project/sources');
  expect(referencesRead).toBe(1);
  await page
    .getByRole('combobox', { name: 'Type', exact: true })
    .selectOption('');
  await page
    .getByRole('combobox', { name: 'Usage', exact: true })
    .selectOption('unused');
  await expect(page.locator('article')).toHaveCount(2);
  await page
    .getByRole('combobox', { name: 'Sort', exact: true })
    .selectOption('size');
  await expect(page.locator('article').first()).toContainText('Chapter.epub');
  await expect(page.locator('article').last()).toContainText('730 B');
});

test('RVC browses compact outputs by default and gives takes readable passage labels', async ({
  page
}) => {
  await signIn(page);
  let fullArtifactReads = 0;
  await page.route('**/api/v1/artifacts?**', (route) => {
    const params = new URL(route.request().url()).searchParams;
    if (params.get('view') !== 'compact') fullArtifactReads++;
    expect(params.get('media_type')).toBe('audio');
    const takes = params.get('output_only') !== 'true';
    const common = {
      session_id: 'carol',
      session_name: 'Christmas Carol',
      kind: 'wav',
      mime_type: 'audio/wav',
      size_bytes: 20,
      state: 'current',
      created_at: new Date().toISOString()
    };
    return route.fulfill({
      json: {
        total: 1,
        offset: 0,
        next_offset: null,
        items: [
          {
            ...common,
            id: takes ? 'internal-take-uuid' : 'output',
            role: takes ? 'generated' : 'export',
            display_name: takes ? 'internal-take-uuid.wav' : 'Stave One.wav',
            ...(takes
              ? {
                  segment_ordinal: 4,
                  speaker: 'Scrooge',
                  segment_text: 'Bah, humbug!'
                }
              : {})
          }
        ]
      }
    });
  });
  await page.goto('/rvc');
  await expect(page.getByLabel('Audio recording')).toContainText(
    'Christmas Carol — Output · Stave One.wav'
  );
  await page.getByLabel('Include individual takes & recordings').check();
  await expect(page.getByLabel('Audio recording')).toContainText(
    'Christmas Carol — Segment 5 · Scrooge — Bah, humbug!'
  );
  await expect(page.getByLabel('Audio recording')).not.toContainText(
    'internal-take-uuid'
  );
  expect(fullArtifactReads).toBe(0);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth
    )
  ).toBeTruthy();
});

test('designed voice filter opens a prefilled redesign without submitting a generation', async ({
  page
}) => {
  await signIn(page);
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const response = await page.request.post('/api/v1/voices', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      name: `Designed narrator ${crypto.randomUUID()}`,
      language: 'en',
      description: 'A warm measured British narrator.'
    }
  });
  const voice = await response.json();
  let submitted = 0;
  await page.route('**/api/v1/voice-catalog?**', (route) =>
    route.fulfill({
      json: {
        items: [
          {
            ...voice,
            key: `managed:${voice.id}`,
            reference: { kind: 'managed', voice_id: voice.id },
            kind: 'managed',
            origin: 'designed',
            voice_category: 'unspecified',
            profile: {
              schema_version: 1,
              pitch: null,
              perceived_age: null,
              textures: [],
              delivery_presets: [],
              use_cases: [],
              languages: [],
              tags: [],
              evidence: {}
            },
            collections: [],
            compatibility: []
          }
        ],
        total: 1,
        next_cursor: null,
        facets: { origin: { designed: 1 }, language: { en: 1 } },
        taxonomy: {},
        collections: []
      }
    })
  );
  await page.route('**/api/v1/voices/design*', (route) => {
    submitted++;
    return route.fulfill({ status: 500, json: {} });
  });
  await page.goto('/voices');
  await page.getByLabel('Voice origin').selectOption('designed');
  await page
    .getByRole('button', { name: `Redesign ${voice.name}`, exact: true })
    .click();
  await expect(
    page.getByRole('combobox', { name: 'Save to', exact: true })
  ).toHaveValue(voice.id);
  await expect(
    page.getByRole('textbox', { name: /^Voice description/ })
  ).toHaveValue('A warm measured British narrator.');
  expect(submitted).toBe(0);
});

test('model language autocomplete accepts names and reports a real planning target', async ({
  page
}) => {
  await signIn(page);
  await page.goto('/models');
  await expect(
    page.locator('#model-catalogue-languages option')
  ).not.toHaveCount(0);
  await page.getByLabel('Language', { exact: true }).fill('English');
  const result = page.waitForResponse(
    (response) =>
      response.url().includes('/services/models/catalogue?') &&
      new URL(response.url()).searchParams.get('language') === 'en'
  );
  await page
    .getByRole('button', { name: 'Apply filters', exact: true })
    .click();
  expect((await result).ok()).toBeTruthy();
  await expect(
    page.getByText('Default audiobook target:', { exact: false }).first()
  ).toBeVisible();
  await page.getByLabel('Language', { exact: true }).fill('No such language');
  await page
    .getByRole('button', { name: 'Apply filters', exact: true })
    .click();
  await expect(page.getByRole('alert')).toContainText(
    'Choose a language supported'
  );
});

test('generation shell is visible while its segment request is pending', async ({
  page
}) => {
  await signIn(page);
  const item = await project(page, 'Drawer loading feedback', 'audiobook');
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(
    `**/api/v1/sessions/${item.id}/generation-segments?**`,
    async (route) => {
      await gate;
      await route.fulfill({
        json: { items: [], total: 0, next_cursor: null, plan_revision_id: null }
      });
    }
  );
  await page.goto(`/sessions/${item.id}`);
  try {
    await expect(
      page.getByText('Loading speech plan…', { exact: true })
    ).toBeVisible();
    await expect(
      page.getByRole('button', { name: 'Generation', exact: true })
    ).toBeDisabled();
  } finally {
    release();
  }
  await expect(
    page.getByText('Loading speech plan…', { exact: true })
  ).toHaveCount(0);
});
