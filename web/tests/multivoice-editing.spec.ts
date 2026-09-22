import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const text = '😀 Bah! said Scrooge. Humbug!';
const xml =
  '<segment id="speech-a"><speaker ref="scrooge">😀 Bah!</speaker><narrator> said Scrooge. </narrator><speaker ref="scrooge"><em>irritable</em>Humbug!</speaker></segment>';
const dictionary = {
  revision: 1,
  characters: [
    {
      id: 'scrooge',
      display_name: 'Scrooge',
      aliases: [],
      voice_category: 'male',
      notes: '',
      locked: true,
      status: 'accepted',
      origin: 'manual'
    }
  ],
  cast: {
    narrator: { voice: 'narrator-reference' },
    characters: { scrooge: { voice: 'scrooge-reference' } },
    categories: {},
    source_speakers: {}
  }
};
const spans = [
  {
    start: 0,
    end: 6,
    speaker_id: 'scrooge',
    speaker_name: 'Scrooge',
    role: 'speaker',
    delivery: {},
    voice: 'scrooge-reference',
    voice_source: 'character'
  },
  {
    start: 6,
    end: 21,
    speaker_id: null,
    speaker_name: 'Narrator',
    role: 'narrator',
    delivery: {},
    voice: 'narrator-reference',
    voice_source: 'narrator'
  },
  {
    start: 21,
    end: 28,
    speaker_id: 'scrooge',
    speaker_name: 'Scrooge',
    role: 'speaker',
    delivery: { emotion: 'irritable' },
    voice: 'scrooge-reference',
    voice_source: 'character'
  }
];
const preview = {
  revision_id: 'plan-a',
  segment_id: 'speech-a',
  source: 'current_plan',
  text,
  service: 'audio_cpp',
  model: 'qwen3_tts_1_7b_base_q8_0',
  casting_enabled: true,
  performance_enabled: false,
  spans,
  parts: spans.map((span) => ({
    ...span,
    instructions: '',
    report: [
      {
        status: 'unsupported',
        message: 'This model does not support delivery instructions.'
      }
    ]
  })),
  events: []
};

async function login(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(
    page.getByRole('heading', { name: 'What shall we make?' })
  ).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const response = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      name: `Multivoice editing ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(response.ok()).toBeTruthy();
  return (await response.json()).id as string;
}

async function setup(
  page: Page,
  options: { plain?: boolean; optimized?: string; conflict?: boolean } = {}
) {
  const id = await login(page);
  const bodies: {
    previews: Record<string, unknown>[];
    applies: Record<string, unknown>[];
    compiles: Record<string, unknown>[];
  } = { previews: [], applies: [], compiles: [] };
  const item = {
    id: 'speech-a',
    ordinal: 0,
    revision: 2,
    text,
    optimized_text: options.optimized ?? null,
    status: 'ready',
    node_kind: 'text',
    paragraph_break_after: false,
    marked: false,
    removed: false,
    source_segment_ids: [],
    takes: [],
    ...(options.plain ? {} : { speech_plan: { speech_xml: xml } })
  };
  await page.route(`**/api/v1/sessions/${id}/generation-runs`, (route) =>
    route.fulfill({ json: { items: [] } })
  );
  await page.route(`**/api/v1/sessions/${id}/generation-segments?*`, (route) =>
    route.fulfill({
      json: {
        items: [item],
        total: 1,
        next_cursor: null,
        plan_revision_id: 'plan-a'
      }
    })
  );
  await page.route(`**/api/v1/sessions/${id}/generation-controls`, (route) =>
    route.fulfill({ json: dictionary })
  );
  await page.route('**/api/v1/voice-catalog?*', (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: 'managed-voice',
            name: 'Fresh British voice',
            reference: { kind: 'managed', voice_id: 'managed-voice' },
            compatibility: [
              {
                service_id: 'audio_cpp',
                model: preview.model,
                ready: true,
                voice: 'published-reference'
              }
            ]
          }
        ],
        total: 1,
        next_cursor: null
      }
    })
  );
  await page.route(`**/api/v1/sessions/${id}/settings/tts`, (route) =>
    route.fulfill({
      json: {
        section: 'tts',
        revision: 1,
        effective: {
          service: 'audio_cpp',
          model: preview.model,
          voice: 'narrator-reference',
          casting_enabled: true,
          performance_enabled: false
        },
        overrides: {},
        inherited: {}
      }
    })
  );
  await page.route(`**/api/v1/sessions/${id}/speech-plan/preview`, (route) => {
    expect(route.request().headers()['content-type']).toBe('application/json');
    bodies.compiles.push(route.request().postDataJSON());
    return route.fulfill({ json: preview });
  });
  await page.route(
    `**/api/v1/sessions/${id}/speech-plan/selection-preview`,
    (route) => {
      expect(route.request().headers()['content-type']).toBe(
        'application/json'
      );
      bodies.previews.push(route.request().postDataJSON());
      return route.fulfill({
        json: { preview_revision: 'a'.repeat(64), locked: true, preview }
      });
    }
  );
  await page.route(
    `**/api/v1/sessions/${id}/speech-plan/selection`,
    (route) => {
      expect(route.request().headers()['content-type']).toBe(
        'application/json'
      );
      bodies.applies.push(route.request().postDataJSON());
      return options.conflict
        ? route.fulfill({
            status: 409,
            json: {
              error: {
                code: 'revision_conflict',
                message:
                  'The speech settings changed. Preview the selection again.'
              }
            }
          })
        : route.fulfill({
            json: { id: 'manual-plan', status: 'adopted', version: 1 }
          });
    }
  );
  await page.goto(`/sessions/${id}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  return { id, bodies };
}

async function selectPhrase(page: Page, start: number, end: number) {
  const root = page.locator('[data-speech-annotations="speech-a"]');
  await expect(root).toBeVisible();
  await root.evaluate(
    (node, offsets) => {
      // Resolve positions directly across text nodes, including surrogate pairs.
      function locate(target: number) {
        const scan = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
        let seen = 0,
          part;
        while ((part = scan.nextNode())) {
          const letters = Array.from(part.textContent ?? '');
          if (target <= seen + letters.length)
            return {
              node: part,
              offset: letters.slice(0, target - seen).join('').length
            };
          seen += letters.length;
        }
        throw new Error('Selection offset is outside speech');
      }
      const begin = locate(offsets.start),
        finish = locate(offsets.end);
      const range = document.createRange();
      range.setStart(begin.node, begin.offset);
      range.setEnd(finish.node, finish.offset);
      window.getSelection()?.removeAllRanges();
      window.getSelection()?.addRange(range);
      node.dispatchEvent(
        new PointerEvent('pointerup', { bubbles: true, pointerType: 'mouse' })
      );
    },
    { start, end }
  );
  return page.getByRole('dialog', {
    name: 'Edit selected speech',
    exact: true
  });
}

test('speaker inspection preserves exact text and reports unsupported acting directions', async ({
  page
}, info) => {
  const { bodies } = await setup(page);
  const root = page.locator('[data-speech-annotations="speech-a"]');
  await expect(root).toHaveText(text);
  await root.getByRole('button', { name: /phrase 2/ }).click();
  const inspector = page.getByRole('dialog', {
    name: 'Voices and delivery for segment 1'
  });
  await expect(inspector).toContainText('Narrator');
  await expect(inspector).toContainText('narrator-reference');
  await expect(inspector).toContainText('Scrooge → Narrator → Scrooge');
  await expect(inspector).toContainText(
    'does not support delivery instructions'
  );
  expect(bodies.compiles[0]).toEqual({
    revision_id: 'plan-a',
    segment_id: 'speech-a'
  });
  await inspector.screenshot({
    path: `../tmp/speech-inspector-${info.project.name}.png`
  });
  await page.keyboard.press('Escape');
  await expect(inspector).toBeHidden();
  await expect(root).toHaveText(text);
});

test('a Unicode selection across speakers requires preview and deliberate unlock before apply', async ({
  page
}, info) => {
  const { bodies } = await setup(page);
  const editor = await selectPhrase(page, 0, 21);
  await expect(editor).toBeVisible();
  await expect(editor.locator('blockquote')).toHaveText(
    Array.from(text).slice(0, 21).join('')
  );
  await editor
    .getByRole('combobox', { name: 'Speaker', exact: true })
    .selectOption('scrooge');
  await editor
    .getByRole('combobox', { name: 'Voice', exact: true })
    .selectOption({ label: 'Fresh British voice' });
  await editor
    .getByLabel('Delivery instruction', { exact: true })
    .fill('Quietly, with dry amusement.');
  await editor
    .getByRole('combobox', { name: 'Pace', exact: true })
    .selectOption('slower');
  await editor.getByText('More delivery controls', { exact: true }).click();
  await editor
    .getByRole('combobox', { name: 'Cadence', exact: true })
    .selectOption('contrast');
  await editor
    .getByRole('combobox', { name: 'Emphasis', exact: true })
    .selectOption('light');
  await editor
    .getByLabel('Enable delivery instructions for future audio')
    .check();
  await editor.getByRole('button', { name: 'Preview change' }).click();
  await expect(editor).toContainText('Review before applying');
  expect(bodies.previews[0]).toMatchObject({
    revision_id: 'plan-a',
    segment_id: 'speech-a',
    expected_segment_revision: 2,
    start: 0,
    end: 21,
    speaker: 'character',
    character_id: 'scrooge',
    voice: 'published-reference',
    delivery: {
      instruction: 'Quietly, with dry amusement.',
      pace: 'slower',
      cadence: 'contrast',
      emphasis: 'light'
    },
    enable_performance: true
  });
  const apply = editor.getByRole('button', { name: 'Apply reviewed change' });
  await expect(apply).toBeDisabled();
  expect(bodies.applies).toHaveLength(0);
  await editor.screenshot({
    path: `../tmp/speech-selection-review-${info.project.name}.png`
  });
  await editor
    .getByLabel('Unlock and replace the protected phrase settings')
    .check();
  await apply.click();
  await expect(editor).toBeHidden();
  expect(bodies.applies[0]).toMatchObject({
    ...bodies.previews[0],
    unlock_locked: true,
    expected_preview_revision: 'a'.repeat(64)
  });
  await expect(page.locator('[data-speech-annotations="speech-a"]')).toHaveText(
    text
  );
});

test('selection review stays open on a revision conflict and fits a phone', async ({
  page
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page, { conflict: true });
  const editor = await selectPhrase(page, 0, 6);
  await editor
    .getByRole('combobox', { name: 'Speaker', exact: true })
    .selectOption('narrator');
  await editor.getByRole('button', { name: 'Preview change' }).click();
  await editor
    .getByLabel('Unlock and replace the protected phrase settings')
    .check();
  await editor.getByRole('button', { name: 'Apply reviewed change' }).click();
  await expect(editor.getByRole('alert')).toContainText('changed');
  const box = (await editor.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(390);
  expect(box.height).toBeLessThanOrEqual(844);
  await editor.screenshot({
    path: `../tmp/speech-selection-phone-${info.project.name}.png`
  });
  const accessibility = await new AxeBuilder({ page })
    .include('[aria-label="Edit selected speech"]')
    .analyze();
  expect(accessibility.violations).toEqual([]);
  await editor.getByRole('button', { name: 'Back to edit' }).click();
  await expect(
    editor.getByRole('combobox', { name: 'Speaker', exact: true })
  ).toHaveValue('narrator');
  await page.keyboard.press('Escape');
  await expect(editor).toBeHidden();
});

test('keyboard selection works for plain speech and restores focus', async ({
  page
}) => {
  await setup(page, { plain: true });
  const input = page.getByRole('textbox', {
    name: 'Script text for segment 1',
    exact: true
  });
  await input.focus();
  await page.keyboard.press('Control+Home');
  await page.keyboard.down('Shift');
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('ArrowRight');
  await page.keyboard.up('Shift');
  const editor = page.getByRole('dialog', {
    name: 'Edit selected speech',
    exact: true
  });
  await expect(editor).toBeVisible();
  await expect(editor.locator('blockquote')).toContainText('😀');
  await page.keyboard.press('Escape');
  await expect(input).toBeFocused();
});

test('audiobook voice mode is an atomic choice and exposes model-aware narration settings', async ({
  page
}, info) => {
  const id = await login(page);
  await page.goto(`/sessions/${id}`);
  const card = page.getByRole('region', { name: 'Audiobook voices' });
  await expect(card).toBeVisible();
  await card.getByRole('radio', { name: /Multiple voices/ }).check();
  await card.getByTestId('cast-help-summary').click();
  await expect(
    card.getByRole('list', { name: 'Multiple-voice audiobook steps' })
  ).toBeVisible();
  await expect(card).toContainText('Identify speakers');
  const response = await page.request.get(
    `/api/v1/sessions/${id}/audiobook-setup`
  );
  expect(response.ok()).toBeTruthy();
  expect(await response.json()).toMatchObject({
    mode: 'multi_voice',
    casting_enabled: true,
    annotation_mode: 'speakers',
    configured: true
  });
  await card.screenshot({
    path: `../tmp/audiobook-casting-${info.project.name}.png`
  });
  await card.getByRole('button', { name: 'Narration settings' }).click();
  await expect(
    page.getByText('Model-aware', { exact: false }).first()
  ).toBeVisible();
});
