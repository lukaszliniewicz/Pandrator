import { expect, test, type Page } from '@playwright/test';

async function displayFilter(page: Page) {
  const filter = page.getByRole('combobox', { name: 'Segments to display' });
  if (!(await filter.isVisible()))
    await page
      .getByRole('button', { name: 'Display options', exact: true })
      .click();
  await expect(filter).toBeVisible();
  return filter;
}

async function fixture(page: Page, total = 2000) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const csrf = (await (await page.request.get('/api/v1/auth/status')).json())
    .csrf_token;
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': csrf },
    data: {
      name: `Generation workspace ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook',
      interface_mode: 'advanced'
    }
  });
  expect(created.ok()).toBeTruthy();
  const id = (await created.json()).id as string;
  const base = `/api/v1/sessions/${id}`;
  const segmentRequests: URL[] = [];
  const runRequests: URL[] = [];
  const errors: Error[] = [];
  page.on('pageerror', (error) => errors.push(error));
  const text = new Map<number, string>();
  const speech = new Map<number, string>();
  const annotations = new Map<number, string>();
  const revisions = new Map<number, number>();
  const segment = (ordinal: number, full = false) => ({
    id: `workspace-segment-${ordinal}`,
    ordinal,
    node_kind: 'paragraph',
    // One long paragraph is deliberate: it must not defeat DOM bounds.
    paragraph_break_after: false,
    text:
      text.get(ordinal) ??
      `Workspace segment ${ordinal + 1}. A sentence for the scrolling reader.`,
    optimized_text: speech.get(ordinal) ?? null,
    speech_plan: {},
    speech_annotation_xml: annotations.get(ordinal) ?? null,
    marked: ordinal % 2 === 0,
    removed: false,
    status: 'completed',
    revision: revisions.get(ordinal) ?? (text.has(ordinal) ? 2 : 1),
    take_count: ordinal === 0 ? 2 : 1,
    take_history_loaded: full || ordinal !== 0,
    takes: [
      {
        id: `workspace-take-${ordinal}`,
        artifact_id: `workspace-artifact-${ordinal}`,
        generation_run_id: null,
        kind: 'tts',
        status: 'completed',
        is_active: true,
        revision: 1
      },
      ...(full && ordinal === 0
        ? [
            {
              id: 'workspace-old-take',
              artifact_id: 'workspace-old-artifact',
              generation_run_id: null,
              kind: 'tts',
              status: 'completed',
              is_active: false,
              revision: 1
            }
          ]
        : [])
    ]
  });
  await page.route(`**${base}/generation/summary*`, (route) =>
    route.fulfill({
      json: {
        session_id: id,
        plan_revision_id: 'workspace-plan',
        total,
        included_total: total,
        active_run: null,
        assembly: null
      }
    })
  );
  await page.route(`**${base}/generation-runs*`, (route) => {
    runRequests.push(new URL(route.request().url()));
    return route.fulfill({ json: { items: [] } });
  });
  await page.route(`**${base}/generation-segments?*`, (route) => {
    const url = new URL(route.request().url());
    segmentRequests.push(url);
    const around = url.searchParams.get('around_ordinal');
    const start = Number(around ?? url.searchParams.get('cursor') ?? 0);
    const end =
      around == null
        ? Math.min(
            total - 1,
            Number(url.searchParams.get('end_ordinal') ?? total - 1)
          )
        : start;
    const ordinals = Array.from(
      { length: Math.max(0, end - start + 1) },
      (_, index) => start + index
    ).filter((ordinal) => !url.searchParams.has('marked') || ordinal % 2 === 0);
    const selected = ordinals.slice(
      0,
      Number(url.searchParams.get('limit') ?? 100)
    );
    return route.fulfill({
      json: {
        items: selected.map((ordinal) =>
          segment(ordinal, url.searchParams.get('take_history') === 'full')
        ),
        total: url.searchParams.has('marked') ? Math.ceil(total / 2) : total,
        next_cursor:
          selected.length < ordinals.length ? selected.at(-1)! + 1 : null,
        plan_revision_id: 'workspace-plan',
        is_active_revision: true
      }
    });
  });
  return {
    id,
    base,
    segmentRequests,
    runRequests,
    errors,
    text,
    speech,
    annotations,
    revisions,
    segment
  };
}

async function scrollBottom(page: Page) {
  await page.getByTestId('generation-scroll-root').evaluate((node) => {
    node.scrollTop = node.scrollHeight;
  });
}

test('project tabs retain only a summary; scroll appends once and cached rows return without reads', async ({
  page
}, testInfo) => {
  const f = await fixture(page);
  await page.goto(`/sessions/${f.id}`);
  await expect(page.getByTestId('generation-status-bar')).toBeVisible();
  expect(f.segmentRequests).toHaveLength(0);
  expect(f.runRequests).toHaveLength(0);
  await page.getByRole('link', { name: 'Generation', exact: true }).click();
  const table = page.getByTestId('generation-segment-table');
  await expect(table).toHaveAttribute('data-loaded-count', '100');
  expect(f.segmentRequests[0].searchParams.get('take_history')).toBe(
    'selected'
  );
  expect(f.runRequests).toHaveLength(1);
  await scrollBottom(page);
  await expect(table).toHaveAttribute('data-loaded-count', '200');
  expect(
    f.segmentRequests.filter((url) => url.searchParams.get('cursor') === '100')
  ).toHaveLength(1);
  expect(f.runRequests).toHaveLength(1);
  const reads = f.segmentRequests.length;
  await page.getByTestId('generation-scroll-root').evaluate((node) => {
    node.scrollTop = 0;
  });
  const row = page.locator('[data-segment-id="workspace-segment-0"]');
  await expect(row).toBeVisible();
  await expect(row.locator('.speech-editing-text')).toHaveText(
    /Workspace segment 1\./
  );
  expect(f.segmentRequests).toHaveLength(reads);
  expect(await page.locator('tbody tr[data-segment-id]').count()).toBeLessThan(
    60
  );
  await row.getByRole('button', { name: 'Show all 2 recordings' }).click();
  await expect(
    row
      .getByRole('combobox', { name: 'Recording for segment 1' })
      .locator('option')
  ).toHaveCount(2);
  expect(
    f.segmentRequests.filter(
      (url) =>
        url.searchParams.get('around_ordinal') === '0' &&
        url.searchParams.get('take_history') === 'full'
    )
  ).toHaveLength(1);
  await testInfo.attach('generation-workspace-metrics.json', {
    contentType: 'application/json',
    body: JSON.stringify(
      {
        loadedSegments: Number(await table.getAttribute('data-loaded-count')),
        renderedRows: await page.locator('tbody tr[data-segment-id]').count(),
        domNodes: await page.locator('*').count(),
        runHistoryReadsAfterPaging: f.runRequests.length,
        selectedTakeReads: f.segmentRequests.filter(
          (url) => url.searchParams.get('take_history') === 'selected'
        ).length,
        fullTakeHistoryReads: f.segmentRequests.filter(
          (url) => url.searchParams.get('take_history') === 'full'
        ).length
      },
      null,
      2
    )
  });
  await page.screenshot({
    path: testInfo.outputPath('generation-workspace.png')
  });
  await page.getByRole('link', { name: 'Overview', exact: true }).click();
  await expect(page.getByTestId('generation-workspace')).toHaveCount(0);
  await expect(page.getByTestId('generation-status-bar')).toBeVisible();
  const after = f.segmentRequests.length;
  await page.waitForTimeout(700); // Covers the detailed invalidation debounce.
  expect(f.segmentRequests).toHaveLength(after);
  expect(f.errors).toEqual([]);
});

test('reading mode stays bounded during automatic paging and restores its view on return', async ({
  page
}) => {
  const f = await fixture(page);
  await page.goto(`/sessions/${f.id}/generation`);
  await expect(page.getByTestId('generation-segment-table')).toHaveAttribute(
    'data-loaded-count',
    '100'
  );
  await page
    .getByRole('button', { name: 'Display options', exact: true })
    .click();
  await page.getByRole('button', { name: 'Reading view', exact: true }).click();
  const reader = page.getByTestId('generation-reading-view');
  await expect(reader).toHaveAttribute('data-virtualized', 'true');
  for (let index = 0; index < 5; index++) {
    const before = Number(await reader.getAttribute('data-loaded-count'));
    await scrollBottom(page);
    await expect
      .poll(async () => Number(await reader.getAttribute('data-loaded-count')))
      .toBeGreaterThan(before);
  }
  expect(Number(await reader.getAttribute('data-rendered-count'))).toBeLessThan(
    150
  );
  await page.getByRole('link', { name: 'Voice & audio', exact: true }).click();
  await expect(reader).toHaveCount(0);
  await page.getByRole('link', { name: 'Generation', exact: true }).click();
  await expect(reader).toBeVisible();
  await expect(reader).toHaveAttribute('data-virtualized', 'true');
  expect(Number(await reader.getAttribute('data-loaded-count'))).toBeLessThan(
    300
  );
  const earlier = page.getByRole('button', { name: 'Load earlier segments' });
  await expect(earlier).toHaveCount(1);
  await page.getByTestId('generation-scroll-root').evaluate((node) => {
    node.scrollTop = 0;
  });
  await expect
    .poll(async () => Number(await reader.getAttribute('data-loaded-count')))
    .toBeGreaterThan(100);
  expect(Number(await reader.getAttribute('data-rendered-count'))).toBeLessThan(
    150
  );
  expect(f.errors).toEqual([]);
});

test('a pending text save settles before navigation and the editor restores filter and selection', async ({
  page
}) => {
  const f = await fixture(page, 40);
  await page.route(
    '**/api/v1/generation-segments/workspace-segment-0',
    async (route) => {
      const value = route.request().postDataJSON().text as string;
      await new Promise((resolve) => setTimeout(resolve, 250));
      f.text.set(0, value);
      await route.fulfill({
        json: { ...f.segment(0), plan_revision_id: 'workspace-plan' }
      });
    }
  );
  await page.goto(`/sessions/${f.id}/generation`);
  const row = page.locator('[data-segment-id="workspace-segment-0"]');
  await expect(row).toBeVisible();
  await page
    .getByRole('button', { name: 'Display options', exact: true })
    .click();
  await page
    .getByRole('button', { name: 'Script / subtitle text', exact: true })
    .click();
  await row.getByRole('checkbox', { name: 'Mark segment 1' }).focus();
  await row.getByRole('button', { name: 'Edit words for segment 1' }).click();
  await row
    .locator('textarea.segment-text')
    .fill('This edit must survive changing tabs.');
  await page.getByRole('link', { name: 'Overview', exact: true }).click();
  await expect(page).toHaveURL(`/sessions/${f.id}`);
  expect(f.text.get(0)).toBe('This edit must survive changing tabs.');
  await page.getByRole('link', { name: 'Generation', exact: true }).click();
  await expect(row.locator('.speech-editing-text')).toHaveText(
    'This edit must survive changing tabs.'
  );
  await (await displayFilter(page)).selectOption('marked');
  await expect(page.getByTestId('generation-segment-table')).toHaveAttribute(
    'data-loaded-count',
    '20'
  );
  await page.getByRole('link', { name: 'Overview', exact: true }).click();
  await page.getByRole('link', { name: 'Generation', exact: true }).click();
  await expect(await displayFilter(page)).toHaveValue('marked');
  expect(f.errors).toEqual([]);
});

async function speechEditingFixture(
  page: Page,
  multiVoice: boolean,
  markup?: string,
  options: {
    capabilities?: Record<string, unknown>;
    deliveryEnabled?: boolean;
    locked?: boolean;
  } = {}
) {
  const f = await fixture(page, 1);
  const wording = 'A😀 calmer spoken line.';
  f.speech.set(0, wording);
  if (markup) f.annotations.set(0, markup);
  const selectionRequests: Array<Record<string, unknown>> = [];
  const bodies: Array<Record<string, unknown>> = [];
  await page.route(`**${f.base}/settings/tts`, (route) =>
    route.fulfill({
      json: {
        section: 'tts',
        revision: 1,
        effective: {
          casting_enabled: multiVoice,
          performance_enabled: options.deliveryEnabled ?? true,
          performance_allow_vocalizations: true
        },
        overrides: {},
        inherited: {}
      }
    })
  );
  await page.route(`**${f.base}/generation-controls`, (route) =>
    route.fulfill({
      json: {
        revision: 1,
        characters: [],
        cast: {
          narrator: {},
          characters: {},
          categories: {},
          source_speakers: {}
        }
      }
    })
  );
  await page.route(`**${f.base}/speech-plan/selection-context`, (route) => {
    const request = route.request().postDataJSON();
    selectionRequests.push(request);
    const currentWording = f.speech.get(0) ?? wording;
    return route.fulfill({
      json: {
        revision_id: 'workspace-plan',
        segment_id: 'workspace-segment-0',
        segment_revision: f.revisions.get(0) ?? 1,
        selection: {
          start: request.start,
          end: request.end,
          text: Array.from(currentWording)
            .slice(request.start, request.end)
            .join('')
        },
        service: 'test',
        model: 'test-model',
        editable: true,
        locked: options.locked ?? false,
        edit_blocked_reason: null,
        speech_xml:
          f.annotations.get(0) ??
          markup ??
          `<segment id="workspace-segment-0">${currentWording}</segment>`,
        current: {
          speaker: 'narrator',
          character_id: null,
          voice: null,
          voice_category: null,
          delivery: {},
          mixed_fields: []
        },
        inherited: { general_direction: '', characters: [] },
        current_flags: {
          casting_enabled: multiVoice,
          performance_enabled: options.deliveryEnabled ?? true,
          allow_vocalizations: true
        },
        capabilities: options.capabilities ?? {},
        preview: null,
        markup_reference: {
          element_tags: ['segment', 'span', 'ins', 'event'],
          element_attributes: { segment: ['id'] },
          control_tags: { instruction: 'ins' },
          delivery_schema: { properties: { instruction: { maxLength: 1200 } } },
          vocal_event_schema: {
            properties: { kind: { enum: ['pause', 'sigh'] } }
          },
          limits: {}
        }
      }
    });
  });
  await page.route(`**${f.base}/speech-plan/selection-preview`, (route) => {
    bodies.push(route.request().postDataJSON());
    return route.fulfill({
      json: {
        preview_revision: 'a'.repeat(64),
        locked: options.locked ?? false,
        preview: { parts: [], report: [] }
      }
    });
  });
  await page.goto(`/sessions/${f.id}/generation?mode=editing`);
  const words = page.locator('[data-speech-editing="workspace-segment-0"]');
  await expect
    .poll(() =>
      words.evaluate((node) => {
        const clone = node.cloneNode(true) as HTMLElement;
        clone
          .querySelectorAll('[data-speech-decoration]')
          .forEach((entry) => entry.remove());
        return clone.textContent;
      })
    )
    .toBe(wording);
  return { ...f, wording, words, bodies, selectionRequests };
}

test('the unified workspace opens speech and its keyboard toolbar follows single-voice mode', async ({
  page
}) => {
  const f = await speechEditingFixture(page, false);
  await expect(page.locator('[data-optimized-speech]')).toHaveText(
    'Optimized speech'
  );
  await expect(
    page
      .getByTestId('generation-segment-table')
      .getByRole('columnheader', { name: 'Audio take' })
  ).toHaveCount(1);
  await expect(
    page.getByRole('group', { name: 'Generation mode', exact: true })
  ).toHaveCount(0);
  await page
    .getByRole('button', { name: 'Block actions for segment 1', exact: true })
    .first()
    .click();
  const toolbar = page.getByRole('toolbar', { name: 'Block speech actions' });
  await expect(
    toolbar.getByRole('button', { name: 'Speaker & voice', exact: true })
  ).toHaveCount(0);
  await page.keyboard.press('Tab');
  await expect(
    toolbar.getByRole('button', { name: 'Instruction', exact: true })
  ).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('Enter');
  await expect(page.locator('[data-delivery-input="emotion"]')).toBeFocused();
  expect(f.errors).toEqual([]);
});

test('multiple voices opens voice autocomplete while XML stays in the block', async ({
  page
}) => {
  const f = await speechEditingFixture(page, true);
  await page
    .getByRole('button', { name: 'Block actions for segment 1', exact: true })
    .first()
    .click();
  await page
    .getByRole('toolbar')
    .getByRole('button', { name: 'Speaker & voice', exact: true })
    .click();
  const voice = page.getByRole('dialog', {
    name: 'Speaker and voice',
    exact: true
  });
  await expect(
    voice.getByRole('combobox', { name: 'Speaker', exact: true })
  ).toBeFocused();
  await expect(
    voice.getByRole('combobox', { name: 'Voice override', exact: true })
  ).not.toBeVisible();
  await voice
    .getByRole('combobox', { name: 'Speaker', exact: true })
    .press('Escape');
  await voice
    .getByText('Override voice for this segment (optional)', { exact: true })
    .click();
  await expect(
    voice.getByRole('combobox', { name: 'Voice override', exact: true })
  ).toBeVisible();
  await voice
    .getByRole('button', { name: 'Close speaker and voice editor' })
    .click();
  await page
    .getByRole('button', { name: 'Edit speech XML for segment 1' })
    .click();
  const xml = page.getByRole('region', { name: 'Speech XML', exact: true });
  await expect(xml.getByLabel('Whole-block speech XML')).toHaveValue(
    `<segment id="workspace-segment-0">${f.wording}</segment>`
  );
  await expect(xml).toHaveJSProperty('popover', null);
  expect(f.errors).toEqual([]);
});

test('the bottom instruction field previews only the root while retaining phrase directions and cues', async ({
  page
}) => {
  const markup =
    '<segment id="workspace-segment-0"><ins>Read warmly</ins>A😀 <span><ins>Emphasize this</ins>calmer</span> spoken line.<event kind="pause" duration_ms="100"/></segment>';
  const f = await speechEditingFixture(page, false, markup);
  await f.words.dblclick();
  await page.getByLabel('Whole-segment instruction for segment 1').click();
  const field = page.locator('[data-delivery-input="instruction"]');
  await expect(field).toBeFocused();
  await expect(field).toHaveValue('Read warmly');
  await field.fill('Read coolly');
  await page
    .getByRole('region', { name: 'Instruction', exact: true })
    .getByRole('button', { name: 'Preview', exact: true })
    .click();
  await expect.poll(() => f.bodies.length).toBe(1);
  expect(f.bodies[0].speech_xml).toBe(
    markup.replace('Read warmly', 'Read coolly')
  );
  expect(f.bodies[0]).toMatchObject({
    start: 0,
    end: Array.from(f.wording).length
  });
  expect(f.bodies[0]).not.toHaveProperty('delivery');
  expect(f.errors).toEqual([]);
});

test('manual instruction saves replace protected directions without an unlock step', async ({
  page
}) => {
  const markup =
    '<segment id="workspace-segment-0"><ins>Read warmly</ins>A😀 calmer spoken line.</segment>';
  const f = await speechEditingFixture(page, false, markup, { locked: true });
  const applied: Array<Record<string, unknown>> = [];
  await page.route(`**${f.base}/speech-plan/selection`, (route) => {
    const body = route.request().postDataJSON();
    applied.push(body);
    f.annotations.set(0, body.speech_xml);
    return route.fulfill({ json: { revision_id: 'workspace-plan' } });
  });
  await f.words.dblclick();
  await page.getByLabel('Whole-segment instruction for segment 1').click();
  const editor = page.getByRole('region', {
    name: 'Instruction',
    exact: true
  });
  const field = editor.locator('[data-delivery-input="instruction"]');
  for (const instruction of ['Read coolly', 'Whisper']) {
    await field.fill(instruction);
    await editor.getByRole('button', { name: 'Save instruction' }).click();
    await expect(field).toBeEnabled();
    await expect
      .poll(() => applied.length)
      .toBe(instruction === 'Whisper' ? 2 : 1);
    expect(applied.at(-1)).toMatchObject({
      speech_xml: markup.replace('Read warmly', instruction),
      expected_segment_revision: 1,
      expected_preview_revision: 'a'.repeat(64),
      unlock_locked: true
    });
    await expect(
      page.getByRole('checkbox', {
        name: 'Unlock protected directions for this edit'
      })
    ).toHaveCount(0);
    await expect(
      editor.getByRole('button', { name: 'Save instruction' })
    ).toBeDisabled();
  }
  expect(f.errors).toEqual([]);
});

test('opening a whole-segment instruction waits for a wording save and uses its new revision', async ({
  page
}) => {
  const f = await speechEditingFixture(page, false);
  let release: (() => void) | undefined;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  const next = 'A😀 revised spoken line.';
  await page.route(
    '**/api/v1/generation-segments/workspace-segment-0',
    async (route) => {
      await pending;
      f.speech.set(0, next);
      f.revisions.set(0, 2);
      await route.fulfill({
        json: { ...f.segment(0), plan_revision_id: 'workspace-plan' }
      });
    }
  );
  await f.words.focus();
  await page.keyboard.press('F2');
  await page.locator('textarea.segment-text').fill(next);
  await page.getByLabel('Whole-segment instruction for segment 1').click();
  expect(f.selectionRequests).toHaveLength(0);
  release?.();
  await expect.poll(() => f.selectionRequests.length).toBe(1);
  expect(f.selectionRequests[0]).toMatchObject({
    expected_segment_revision: 2,
    start: 0,
    end: Array.from(next).length
  });
  await expect(
    page.locator('[data-delivery-input="instruction"]')
  ).toBeFocused();
  expect(f.errors).toEqual([]);
});

test('single click leaves annotated preview read-only until explicit wording editing', async ({
  page
}) => {
  const f = await speechEditingFixture(page, false);
  await f.words.click();
  await expect(
    page.locator('[data-segment-id="workspace-segment-0"]')
  ).toHaveClass(/selected/);
  await expect(f.words).toHaveAttribute('contenteditable', 'false');
  await expect(page.locator('textarea.segment-text')).toHaveCount(0);
  await expect(
    page.getByRole('toolbar', { name: 'Cursor speech actions' })
  ).toHaveCount(0);
  await page.keyboard.type('Z');
  await expect(f.words).toHaveText(f.wording);
  await page.keyboard.press('F2');
  const editor = page.locator('textarea.segment-text');
  await expect(editor).toBeFocused();
  await page.keyboard.press('Home');
  await page.keyboard.type('Z');
  await expect(editor).toHaveValue(`Z${f.wording}`);
  expect(f.errors).toEqual([]);
});

test('a cursor cue keeps its Unicode position without speaker or delivery edits', async ({
  page
}) => {
  const f = await speechEditingFixture(page, false);
  await f.words.focus();
  await page.keyboard.press('F2');
  const editor = page.locator('textarea.segment-text');
  await editor.evaluate((node: HTMLTextAreaElement) => {
    node.focus();
    node.setSelectionRange(3, 3); // A plus the surrogate pair
    node.dispatchEvent(new Event('select', { bubbles: true }));
    node.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
  });
  await page
    .getByRole('toolbar', { name: 'Cursor speech actions' })
    .getByRole('button', { name: 'Vocal cue', exact: true })
    .click();
  await page.locator('[data-vocal-cue]').selectOption('sigh');
  await page
    .getByRole('dialog', { name: 'Vocal cue', exact: true })
    .getByRole('button', { name: 'Preview', exact: true })
    .click();
  await expect.poll(() => f.bodies.length).toBe(1);
  expect(f.bodies[0]).toMatchObject({
    start: 2,
    end: 2,
    vocal_event: { kind: 'sigh', position: 'before' }
  });
  for (const field of ['voice', 'delivery', 'character_id', 'speech_xml'])
    expect(f.bodies[0]).not.toHaveProperty(field);
  expect(f.errors).toEqual([]);
});

test('audio visibility is a display choice and F2 opens the segment instruction field', async ({
  page
}) => {
  const f = await speechEditingFixture(page, false);
  const table = page.getByTestId('generation-segment-table');
  await f.words.focus();
  await page.keyboard.press('F2');
  await expect(page.locator('textarea.segment-text')).toBeFocused();
  await expect(
    page.getByLabel('Whole-segment instruction for segment 1')
  ).toBeVisible();
  await page.getByRole('button', { name: 'Done editing', exact: true }).click();
  const reads = f.segmentRequests.length;
  await page
    .getByRole('button', { name: 'Display options', exact: true })
    .click();
  await page
    .getByRole('button', { name: 'Hide audio columns', exact: true })
    .click();
  await expect(
    table.getByRole('columnheader', { name: 'Audio take' })
  ).toHaveCount(0);
  await expect(f.words).toBeVisible();
  await page
    .getByRole('button', { name: 'Display options', exact: true })
    .click();
  await page
    .getByRole('button', { name: 'Show audio columns', exact: true })
    .click();
  await expect(
    table.getByRole('columnheader', { name: 'Audio take' })
  ).toHaveCount(1);
  expect(f.segmentRequests).toHaveLength(reads);
  expect(f.errors).toEqual([]);
});

test('phrase directions appear on hover without a selection API read or contaminating copied speech', async ({
  page
}) => {
  const f = await speechEditingFixture(
    page,
    false,
    '<segment id="workspace-segment-0">A😀 <span><ins>Emphasize this</ins>calmer</span> spoken line.</segment>'
  );
  await expect(f.words).toHaveText(f.wording);
  const phrase = f.words.getByRole('button', {
    name: /calmer.*Emphasize this/
  });
  await f.words.evaluate((node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    window.getSelection()?.removeAllRanges();
    window.getSelection()?.addRange(range);
  });
  const reads = f.selectionRequests.length;
  await phrase.hover();
  const hint = page.getByRole('dialog', { name: 'Speech annotation details' });
  await expect(hint).toBeVisible();
  await expect(hint).toContainText('Emphasize this');
  expect(f.selectionRequests).toHaveLength(reads);
  const copied = await f.words.evaluate((node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    window.getSelection()?.removeAllRanges();
    window.getSelection()?.addRange(range);
    const data = new DataTransfer();
    node.dispatchEvent(
      new ClipboardEvent('copy', {
        bubbles: true,
        cancelable: true,
        clipboardData: data
      })
    );
    return data.getData('text/plain');
  });
  expect(copied).toBe(f.wording);
  expect(f.errors).toEqual([]);
});

test('Gemini emotion suggestions retain custom text and separate model support from delivery being off', async ({
  page
}) => {
  const f = await speechEditingFixture(page, false, undefined, {
    deliveryEnabled: false,
    capabilities: {
      status: 'documented',
      dialect: 'gemini38',
      instructions: 'field',
      emotion: { mode: 'open_description', tags: [] }
    }
  });
  await f.words.evaluate((node) => {
    const text = document
      .createTreeWalker(node, NodeFilter.SHOW_TEXT, {
        acceptNode: (entry) =>
          entry.textContent === node.textContent
            ? NodeFilter.FILTER_ACCEPT
            : NodeFilter.FILTER_SKIP
      })
      .nextNode();
    if (!text) throw new Error('Missing spoken text');
    const range = document.createRange();
    range.setStart(text, 4);
    range.setEnd(text, 10);
    window.getSelection()?.removeAllRanges();
    window.getSelection()?.addRange(range);
    node.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
  });
  await page
    .getByRole('toolbar')
    .getByRole('button', { name: 'Emotion', exact: true })
    .click();
  const editor = page.getByRole('dialog', { name: 'Emotion', exact: true });
  const choice = editor.getByRole('combobox', { name: 'Emotion', exact: true });
  await choice.selectOption('excited');
  await expect(editor.locator('.emotion-support')).toHaveAttribute(
    'data-support',
    'hint'
  );
  await expect(editor.locator('.emotion-support')).toContainText(
    'Delivery is off'
  );
  await choice.selectOption('__custom__');
  await editor
    .getByLabel('Custom emotion', { exact: true })
    .fill('quietly hopeful, despite exhaustion');
  await editor.getByRole('button', { name: 'Preview', exact: true }).click();
  await expect.poll(() => f.bodies.length).toBe(1);
  expect(f.bodies[0]).toMatchObject({
    start: 3,
    end: 9,
    delivery: { emotion: 'quietly hopeful, despite exhaustion' },
    enable_performance: false
  });
  expect(f.errors).toEqual([]);
});

test('playlist playback keeps compact controls after leaving generation', async ({
  page
}) => {
  const f = await fixture(page, 40);
  await page.goto(`/sessions/${f.id}/generation`);
  await expect(page.getByTestId('generation-segment-table')).toHaveAttribute(
    'data-loaded-count',
    '40'
  );
  await page.evaluate(() => {
    const probe = {
      audios: [] as Array<{
        src: string;
        paused: boolean;
        onended?: () => void;
      }>
    };
    (window as unknown as { playbackProbe: typeof probe }).playbackProbe =
      probe;
    window.Audio = class {
      src: string;
      paused = false;
      onended?: () => void;
      onerror?: () => void;
      constructor(src: string) {
        this.src = src;
        probe.audios.push(this);
      }
      play() {
        this.paused = false;
        return Promise.resolve();
      }
      pause() {
        this.paused = true;
      }
    } as unknown as typeof Audio;
  });
  await page
    .getByRole('button', { name: 'Play playlist', exact: true })
    .click();
  await expect(
    page.getByRole('button', { name: 'Pause playlist', exact: true })
  ).toBeVisible();
  await page.getByRole('link', { name: 'Overview', exact: true }).click();
  await expect(page.getByTestId('generation-workspace')).toHaveCount(0);
  await page
    .getByRole('button', { name: 'Pause playback', exact: true })
    .click();
  expect(
    await page.evaluate(
      () =>
        (
          window as unknown as {
            playbackProbe: { audios: { paused: boolean }[] };
          }
        ).playbackProbe.audios[0].paused
    )
  ).toBe(true);
  await page
    .getByRole('button', { name: 'Resume playback', exact: true })
    .click();
  await page.evaluate(() =>
    (
      window as unknown as {
        playbackProbe: { audios: { onended: () => void }[] };
      }
    ).playbackProbe.audios[0].onended()
  );
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (
            window as unknown as {
              playbackProbe: { audios: { src: string }[] };
            }
          ).playbackProbe.audios.at(-1)?.src
      )
    )
    .toContain('workspace-artifact-1');
  await page
    .getByRole('button', { name: 'Stop playback', exact: true })
    .click();
  expect(
    await page.evaluate(
      () =>
        (
          window as unknown as {
            playbackProbe: { audios: { paused: boolean }[] };
          }
        ).playbackProbe.audios.at(-1)?.paused
    )
  ).toBe(true);
  expect(f.segmentRequests).toHaveLength(1);
  expect(f.errors).toEqual([]);
});

test('a delayed page cannot reappear after changing the segment filter', async ({
  page
}) => {
  const f = await fixture(page, 1000);
  let release: (() => void) | undefined;
  let waiting = false;
  await page.route(
    /\/generation-segments\?.*cursor=100(?:&|$)/,
    async (route) => {
      if (new URL(route.request().url()).searchParams.has('marked'))
        return route.fallback();
      waiting = true;
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      await route
        .fulfill({
          json: {
            items: [{ ...f.segment(100), id: 'late-all-row' }],
            total: 1000,
            next_cursor: 200,
            plan_revision_id: 'workspace-plan'
          }
        })
        .catch(() => undefined);
    }
  );
  await page.goto(`/sessions/${f.id}/generation`);
  await expect(page.getByTestId('generation-segment-table')).toHaveAttribute(
    'data-loaded-count',
    '100'
  );
  await scrollBottom(page);
  await expect.poll(() => waiting).toBe(true);
  await (await displayFilter(page)).selectOption('marked');
  await expect
    .poll(() => f.segmentRequests.some((url) => url.searchParams.has('marked')))
    .toBe(true);
  release?.();
  await expect(await displayFilter(page)).toHaveValue('marked');
  await expect(page.locator('[data-segment-id="late-all-row"]')).toHaveCount(0);
  expect(f.errors).toEqual([]);
});

test('Simple Generation is read-only and switching restores Advanced controls without rewriting the plan', async ({
  page
}) => {
  const f = await speechEditingFixture(page, true);
  const modes = page.getByRole('group', {
    name: 'Project interface',
    exact: true
  });
  const before = await (
    await page.request.get(`${f.base}/generation-segments`)
  ).json();
  await modes.getByRole('button', { name: /^simple$/i }).click();
  await expect(
    modes.getByRole('button', { name: /^simple$/i })
  ).toHaveAttribute('aria-pressed', 'true');
  await expect(
    page.getByRole('button', { name: 'Edit words for segment 1', exact: true })
  ).toHaveCount(0);
  await expect(
    page.getByRole('button', {
      name: 'Edit speech XML for segment 1',
      exact: true
    })
  ).toHaveCount(0);
  await expect(page.getByLabel('Mark segment 1', { exact: true })).toHaveCount(
    0
  );
  await f.words.focus();
  await page.keyboard.press('F2');
  await expect(page.locator('textarea.segment-text')).toHaveCount(0);
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await expect(
    page.getByRole('button', { name: 'Replace all', exact: true })
  ).toHaveCount(0);
  const stored = await (await page.request.get(f.base)).json();
  expect(stored.interface_mode).toBe('simple');
  expect(
    (await (await page.request.get(`${f.base}/generation-segments`)).json())
      .plan_revision_id
  ).toBe(before.plan_revision_id);
  await modes.getByRole('button', { name: /^advanced$/i }).click();
  await expect(
    page.getByRole('button', { name: 'Edit words for segment 1', exact: true })
  ).toBeVisible();
  expect(f.errors).toEqual([]);
});

test('a pending interface switch pauses project input until it finishes', async ({
  page
}) => {
  const f = await speechEditingFixture(page, false);
  await f.words.focus();
  await page.keyboard.press('F2');
  const editor = page.locator('textarea.segment-text');
  const wording = await editor.inputValue();
  let release!: () => void;
  let entered!: () => void;
  const waitForRelease = new Promise<void>((resolve) => (release = resolve));
  const patchEntered = new Promise<void>((resolve) => (entered = resolve));
  await page.route(`**${f.base}`, async (route) => {
    if (route.request().method() === 'PATCH') {
      entered();
      await waitForRelease;
    }
    await route.continue();
  });
  const simple = page
    .getByRole('group', { name: 'Project interface', exact: true })
    .getByRole('button', { name: /^simple$/i });
  await simple.click();
  try {
    await patchEntered;
    await expect(page.locator('.session-content')).toHaveAttribute('inert', '');
    await editor.evaluate((node: HTMLTextAreaElement) => node.focus());
    await expect(editor).not.toBeFocused();
    await page.keyboard.type('Z');
    await expect(editor).toHaveValue(wording);
  } finally {
    release();
  }
  await expect(simple).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.session-content')).not.toHaveAttribute('inert');
  expect(f.errors).toEqual([]);
});

test('switching to Simple does not discard an unfinished instruction editor', async ({
  page
}) => {
  const f = await speechEditingFixture(page, false);
  await f.words.focus();
  await page.keyboard.press('F2');
  await page.getByLabel('Whole-segment instruction for segment 1').click();
  await page
    .locator('[data-delivery-input="instruction"]')
    .fill('An unfinished direction');
  await page
    .getByRole('group', { name: 'Project interface', exact: true })
    .getByRole('button', { name: /^simple$/i })
    .click();
  await expect(page.getByRole('alert')).toContainText(
    'Finish or close the open editor'
  );
  await expect(page.locator('[data-delivery-input="instruction"]')).toHaveValue(
    'An unfinished direction'
  );
  expect((await (await page.request.get(f.base)).json()).interface_mode).toBe(
    'advanced'
  );
  expect(f.errors).toEqual([]);
});
