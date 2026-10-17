import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { editGenerationWords } from './fixtures/generation-editing';

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
    page.getByRole('heading', { name: 'Create a project' })
  ).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const response = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      interface_mode: 'advanced',
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
    contexts: Record<string, unknown>[];
    previews: Record<string, unknown>[];
    applies: Record<string, unknown>[];
    compiles: Record<string, unknown>[];
  } = { contexts: [], previews: [], applies: [], compiles: [] };
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
  await page.route(`**/api/v1/sessions/${id}/generation-runs*`, (route) =>
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
  const service = {
    id: 'audio_cpp',
    name: 'audio.cpp',
    online: true,
    available: true,
    models: [preview.model],
    default_model: preview.model,
    model_catalog: [
      { id: preview.model, family: 'qwen3_tts', voice_mode: 'cloning' }
    ]
  };
  await page.route(
    /\/api\/v1\/services\/tts(?:\/audio_cpp)?(?:\?.*)?$/,
    (route) =>
      route.fulfill({
        json: {
          default_service: 'audio_cpp',
          services: [service],
          service,
          selected_models: [preview.model]
        }
      })
  );
  await page.route('**/api/v1/voices', (route) =>
    route.fulfill({ json: { items: [] } })
  );
  await page.route(
    `**/api/v1/sessions/${id}/speech-plan/selection-context`,
    (route) => {
      const request = route.request().postDataJSON();
      bodies.contexts.push(request);
      return route.fulfill({
        json: {
          revision_id: 'plan-a',
          segment_id: 'speech-a',
          segment_revision: 2,
          selection: {
            start: request.start,
            end: request.end,
            text: Array.from(options.optimized ?? text)
              .slice(request.start, request.end)
              .join('')
          },
          service: preview.service,
          model: preview.model,
          editable: true,
          locked: true,
          edit_blocked_reason: null,
          speech_xml: options.plain ? null : xml,
          current: {
            speaker: 'mixed',
            character_id: null,
            voice: null,
            voice_category: null,
            delivery: {},
            mixed_fields: ['speaker']
          },
          inherited: { general_direction: '', characters: [] },
          current_flags: {
            casting_enabled: true,
            performance_enabled: false,
            allow_vocalizations: true
          },
          capabilities: { instructions: 'unsupported' },
          preview,
          markup_reference: {
            element_tags: ['segment', 'speaker', 'narrator', 'ins'],
            element_attributes: {},
            control_tags: { instruction: 'ins' },
            delivery_schema: { properties: {} },
            vocal_event_schema: { properties: {} },
            limits: {}
          }
        }
      });
    }
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
  await page
    .getByRole('region', { name: 'Voices', exact: true })
    .getByRole('radio', { name: /Multiple voices/ })
    .check();
  await page.goto(`/sessions/${id}/generation`);
  await expect(
    page.getByRole('link', { name: 'Multiple voices', exact: true })
  ).toBeVisible();
  return { id, bodies };
}

async function selectPhrase(
  page: Page,
  start: number,
  end: number,
  action = 'Speaker & voice'
) {
  const root = page.locator('[data-speech-editing="speech-a"]');
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
  await page.getByRole('button', { name: action, exact: true }).click();
  return page.getByRole('dialog', {
    name: action === 'Speaker & voice' ? 'Speaker and voice' : 'Delivery',
    exact: true
  });
}

test('speaker inspection preserves exact text and reports unsupported acting directions', async ({
  page
}, info) => {
  const { bodies } = await setup(page);
  const root = page.locator('[data-speech-editing="speech-a"]');
  await expect(root).toHaveText(text);
  await expect(root.locator('.hint-text')).toHaveCount(3);
  const underlineStyles = await root
    .locator('.hint-text')
    .evaluateAll((nodes) =>
      nodes.map((node) => ({
        color: getComputedStyle(node).getPropertyValue('--speaker-color'),
        direction: getComputedStyle(node).textDecorationStyle,
        width: getComputedStyle(node).textDecorationThickness
      }))
    );
  expect(underlineStyles[0].color).not.toBe(underlineStyles[1].color);
  expect(underlineStyles[0].color).toBe(underlineStyles[2].color);
  expect(parseFloat(underlineStyles[0].width)).toBeGreaterThanOrEqual(2);
  expect(underlineStyles[2].direction).toBe('double');
  await expect(page.getByText('Show waveform', { exact: true })).toHaveCount(0);
  const optionsButton = page.getByRole('button', {
    name: 'Options for segment 1',
    exact: true
  });
  await optionsButton.click();
  const options = page.getByRole('dialog', {
    name: 'Options for segment 1',
    exact: true
  });
  await expect(
    options.getByRole('combobox', { name: 'Segment role', exact: true })
  ).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(options).toBeHidden();
  await expect(optionsButton).toBeFocused();
  await root.locator('.hint-text').nth(1).focus();
  await page.keyboard.press('Enter');
  const inspector = page.getByRole('dialog', {
    name: 'Speaker and voice',
    exact: true
  });
  await expect(inspector).toContainText('Narrator');
  await expect(inspector).toContainText('narrator-reference');
  await page.keyboard.press('Escape');
  await inspector
    .getByText('Effective instructions and voice sequence', { exact: true })
    .click();
  await expect(inspector).toContainText(
    'does not support delivery instructions'
  );
  expect(bodies.contexts[0]).toMatchObject({
    revision_id: 'plan-a',
    segment_id: 'speech-a',
    expected_segment_revision: 2,
    start: 6,
    end: 21
  });
  await inspector.screenshot({
    path: `../tmp/speech-inspector-${info.project.name}.png`
  });
  await inspector
    .getByRole('button', { name: 'Close speaker and voice editor' })
    .click();
  await expect(inspector).toBeHidden();
  await expect(root).toHaveText(text);
});

test('a Unicode selection across speakers previews an assignment before explicit save', async ({
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
    .fill('Scrooge');
  await editor.getByRole('option', { name: 'Scrooge', exact: true }).click();
  await editor
    .getByText('Override voice for these words (optional)', { exact: true })
    .click();
  await editor
    .getByRole('combobox', { name: 'Voice override', exact: true })
    .fill('Fresh British voice');
  await editor
    .getByRole('option', { name: 'Fresh British voice', exact: true })
    .click();
  await editor.getByRole('button', { name: 'Preview', exact: true }).click();
  await expect(editor).toContainText('Review change');
  expect(bodies.previews[0]).toMatchObject({
    revision_id: 'plan-a',
    segment_id: 'speech-a',
    expected_segment_revision: 2,
    start: 0,
    end: 21,
    speaker: 'character',
    character_id: 'scrooge',
    voice: 'published-reference',
    enable_performance: false
  });
  expect(bodies.applies).toHaveLength(0);
  await editor.screenshot({
    path: info.outputPath('speech-selection-review.png')
  });
  await editor
    .getByRole('button', { name: 'Save assignment', exact: true })
    .click();
  await expect(editor).toContainText('Saved for future audio.');
  expect(bodies.applies[0]).toMatchObject({
    ...bodies.previews[0],
    unlock_locked: true,
    expected_preview_revision: 'a'.repeat(64)
  });
  await editor
    .getByRole('button', { name: 'Close speaker and voice editor' })
    .click();
  await expect(editor).toBeHidden();
  await expect(page.locator('[data-speech-editing="speech-a"]')).toHaveText(
    text
  );
});

test('Unicode delivery edits retain precise boundaries and optional model directions', async ({
  page
}) => {
  const { bodies } = await setup(page);
  const editor = await selectPhrase(page, 0, 21, 'More delivery controls');
  await expect(editor).toBeVisible();
  await editor
    .getByLabel('Delivery instruction', { exact: true })
    .fill('Quietly, with dry amusement.');
  await editor.getByRole('combobox', { name: /^Pace/ }).selectOption('slower');
  await editor
    .getByRole('combobox', { name: /^Cadence/ })
    .selectOption('contrast');
  await editor
    .getByRole('combobox', { name: /^Emphasis/ })
    .selectOption('light');
  await editor.getByLabel('Use delivery directions for future audio').check();
  await editor.getByRole('button', { name: 'Preview', exact: true }).click();
  await expect(editor).toContainText('Review change');
  expect(bodies.previews[0]).toMatchObject({
    revision_id: 'plan-a',
    segment_id: 'speech-a',
    expected_segment_revision: 2,
    start: 0,
    end: 21,
    speaker: 'unchanged',
    delivery: {
      instruction: 'Quietly, with dry amusement.',
      pace: 'slower',
      cadence: 'contrast',
      emphasis: 'light'
    },
    enable_performance: true
  });
  expect(bodies.applies).toHaveLength(0);
  await editor.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(editor).toContainText('Saved for future audio.');
  expect(bodies.applies[0]).toMatchObject({
    ...bodies.previews[0],
    unlock_locked: true,
    expected_preview_revision: 'a'.repeat(64)
  });
});

test('selection review stays open on a revision conflict and fits a phone', async ({
  page
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page, { conflict: true });
  const editor = await selectPhrase(page, 0, 6);
  await editor
    .getByRole('combobox', { name: 'Speaker', exact: true })
    .fill('Narrator');
  await editor.getByRole('option', { name: 'Narrator', exact: true }).click();
  await editor.getByRole('button', { name: 'Preview', exact: true }).click();
  await expect(editor).toContainText('Review change');
  await editor
    .getByRole('button', { name: 'Save assignment', exact: true })
    .click();
  await expect(editor.getByRole('alert')).toContainText('changed');
  const box = (await editor.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(390);
  expect(box.height).toBeLessThanOrEqual(844);
  await editor.screenshot({
    path: info.outputPath('speech-selection-phone.png')
  });
  const accessibility = await new AxeBuilder({ page })
    .include('.speech-inspector')
    .analyze();
  expect(accessibility.violations).toEqual([]);
  await expect(
    editor.getByRole('combobox', { name: 'Speaker', exact: true })
  ).toHaveValue('Narrator');
  await editor
    .getByRole('button', { name: 'Close speaker and voice editor' })
    .click();
  const guard = page.getByRole('dialog', { name: 'Keep your speech changes?' });
  await expect(guard).toBeVisible();
  await guard.getByRole('button', { name: 'Discard and continue' }).click();
  await expect(editor).toBeHidden();
});

test('keyboard selection works for plain speech and restores focus', async ({
  page
}) => {
  await setup(page, { plain: true });
  await editGenerationWords(page, 1);
  const input = page.getByRole('textbox', {
    name: 'Spoken override for segment 1',
    exact: true
  });
  await input.focus();
  await page.keyboard.press('Control+Home');
  await page.keyboard.down('Shift');
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('ArrowRight');
  await page.keyboard.up('Shift');
  await page
    .getByRole('button', { name: 'Speaker & voice', exact: true })
    .click();
  const editor = page.getByRole('dialog', {
    name: 'Speaker and voice',
    exact: true
  });
  await expect(editor).toBeVisible();
  await expect(editor.locator('blockquote')).toContainText('😀');
  // Escape first closes the autocomplete, then the enclosing editor.
  await page.keyboard.press('Escape');
  await expect(editor).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(editor).toBeHidden();
  await expect(input).toBeFocused();
});

test('audiobook voice mode is an atomic choice and exposes model-aware narration settings', async ({
  page
}, info) => {
  const id = await login(page);
  await page.goto(`/sessions/${id}`);
  const card = page.getByRole('region', { name: 'Voices', exact: true });
  await expect(card).toBeVisible();
  const textBeforeMode = await (
    await page.request.get(`/api/v1/sessions/${id}/settings/text`)
  ).json();
  const modeSaved = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/sessions/${id}/voice-setup`) &&
      response.request().method() === 'PATCH'
  );
  await card.getByRole('radio', { name: /Multiple voices/ }).check();
  expect((await modeSaved).ok()).toBeTruthy();
  await card.getByTestId('cast-help-summary').click();
  await expect(
    card.getByRole('list', { name: 'Multiple-voice steps' })
  ).toBeVisible();
  await expect(card).toContainText('Identify speakers');
  const response = await page.request.get(`/api/v1/sessions/${id}/voice-setup`);
  expect(response.ok()).toBeTruthy();
  expect(await response.json()).toMatchObject({
    mode: 'multi_voice',
    casting_enabled: true,
    legacy_voice_overrides: false
  });
  expect(
    await (
      await page.request.get(`/api/v1/sessions/${id}/settings/text`)
    ).json()
  ).toEqual(textBeforeMode);
  await card.screenshot({
    path: `../tmp/audiobook-casting-${info.project.name}.png`
  });
  await page
    .getByRole('button', { name: 'Narration settings', exact: true })
    .click();
  await expect(
    page.getByText('Model-aware', { exact: false }).first()
  ).toBeVisible();
});
