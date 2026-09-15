import { expect, test, type Page } from '@playwright/test';
import {
  passagePieces,
  type PassageBoundary,
  type PassageLayer
} from '../src/lib/passage-structure';
import type { GenerationSegment } from '../src/lib/api-models';

const left = '😀 He described a local religious';
const right = 'controversy in public.';
const text = `${left} ${right}`;
const boundary: PassageBoundary = {
  id: `pb-${'a'.repeat(24)}`,
  offset: Array.from(left).length,
  display_offset: Array.from(left).length,
  speech_offset: Array.from(left).length,
  left_reference: 173,
  right_reference: 174,
  left_end_ms: 762180,
  right_start_ms: 764340,
  gap_ms: 2160,
  left_window: [759780, 762180],
  right_window: [764340, 769060],
  natural: false,
  boundary_kind: null,
  warning:
    'This timed boundary is inside an unfinished phrase. Splitting here may produce unnatural speech.',
  split_allowed: true,
  split_blocked_reason: null
};
const layer: PassageLayer = {
  status: 'mapped',
  text,
  message: null,
  boundaries: [boundary]
};

function segment(): GenerationSegment {
  return {
    id: 'passage-segment',
    ordinal: 53,
    revision: 1,
    text,
    status: 'ready',
    node_kind: 'subtitle_cue',
    paragraph_break_after: false,
    marked: false,
    removed: false,
    source_segment_ids: [173, 174],
    optimized_text: null,
    takes: [],
    passage_structure: {
      schema_version: 1,
      layers: {
        display: structuredClone(layer),
        speech: structuredClone(layer)
      }
    }
  };
}

test('Unicode offsets retain exact text and fail closed on stale or invalid ranges', () => {
  const pieces = passagePieces(text, layer)!;
  expect(pieces[0].text).toBe(left);
  expect(pieces.map((piece) => piece.text).join('')).toBe(text);
  expect(passagePieces('Edited ' + text, layer)).toBeNull();
  expect(
    passagePieces(text, { ...layer, boundaries: [{ ...boundary, offset: 0 }] })
  ).toBeNull();
  expect(
    passagePieces(text, { ...layer, boundaries: [boundary, boundary] })
  ).toBeNull();
});

async function setup(page: Page, item = segment()) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  const auth = await (await page.request.get('/api/v1/auth/status')).json();
  const created = await page.request.post('/api/v1/sessions', {
    headers: { 'X-CSRF-Token': auth.csrf_token },
    data: {
      name: `Passage markers ${crypto.randomUUID()}`,
      workflow_kind: 'audiobook'
    }
  });
  expect(created.ok()).toBeTruthy();
  const id = (await created.json()).id as string;
  let splitBody: Record<string, unknown> | undefined;
  let revision = 'plan-a';
  await page.route(`**/api/v1/sessions/${id}/generation-runs`, (route) =>
    route.fulfill({ json: { items: [] } })
  );
  await page.route(`**/api/v1/sessions/${id}/generation-segments?*`, (route) =>
    route.fulfill({
      json: {
        items: [item],
        total: 1,
        next_cursor: null,
        plan_revision_id: revision
      }
    })
  );
  await page.route(
    `**/api/v1/sessions/${id}/generation-plan/topology`,
    async (route) => {
      splitBody = route.request().postDataJSON();
      revision = 'plan-b';
      await route.fulfill({
        status: 201,
        json: {
          plan_revision_id: revision,
          segment_ids: ['new-left', 'new-right']
        }
      });
    }
  );
  await page.goto(`/sessions/${id}`);
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  return { getSplit: () => splitBody, sessionId: id };
}

test('table dots are not text; an unfinished-phrase split needs deliberate confirmation', async ({
  page
}, info) => {
  const state = await setup(page);
  const passage = page.locator('[data-passage-segment="passage-segment"]');
  await expect(passage).toBeVisible();
  expect(await passage.textContent()).toBe(text);
  const dot = passage.getByRole('button', { name: /Passages 173 \/ 174/ });
  await dot.focus();
  await page.keyboard.press('Enter');
  await page.getByRole('menuitem', { name: 'Preview', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Inspect passage boundary' });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('2.16 s source gap');
  await expect(dialog).toContainText(left);
  await expect(
    dialog.getByRole('button', { name: 'Split here', exact: true })
  ).toBeDisabled();
  await dialog.screenshot({
    path: `../tmp/passage-preview-${info.project.name}.png`
  });
  await dialog.getByLabel('Split this unfinished phrase deliberately.').check();
  await dialog.getByRole('button', { name: 'Split here', exact: true }).click();
  await expect.poll(state.getSplit).toMatchObject({
    passage_boundary_id: boundary.id,
    cursor: Array.from(left).length,
    text_layer: 'display',
    expected_revision_id: 'plan-a'
  });
  await expect(dialog).not.toBeVisible();
});

test('reading view shares markers; Escape restores focus and native editing never includes a dot', async ({
  page
}, info) => {
  await setup(page);
  await page
    .getByRole('button', { name: 'Edit text for segment 54', exact: true })
    .click();
  await expect(
    page.getByRole('textbox', {
      name: 'Script text for segment 54',
      exact: true
    })
  ).toHaveValue(text);
  await page
    .getByRole('button', { name: 'Return to passage view', exact: true })
    .click();
  await page
    .getByRole('button', { name: 'Display options', exact: true })
    .click();
  await page.getByRole('button', { name: 'Reading view', exact: true }).click();
  const dot = page.getByRole('button', { name: /Passages 173 \/ 174/ });
  await expect(dot).toHaveCount(1);
  await dot.focus();
  await page.keyboard.press('Enter');
  await page.getByRole('menuitem', { name: 'Preview', exact: true }).click();
  await expect(
    page.getByRole('dialog', { name: 'Inspect passage boundary' })
  ).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(
    page.getByRole('dialog', { name: 'Inspect passage boundary' })
  ).not.toBeVisible();
  await expect(dot).toBeFocused();
  await page.screenshot({
    path: `../tmp/passage-reading-${info.project.name}.png`
  });
});

test('stale text mappings have no clickable anchors', async ({ page }) => {
  const item = segment();
  item.text = 'Changed ' + item.text;
  await setup(page, item);
  await expect(
    page.getByRole('button', { name: /Passages 173 \/ 174/ })
  ).toHaveCount(0);
  await expect(
    page.getByText(
      'Text changed; passage markers are hidden until the mapping is verified.',
      { exact: true }
    )
  ).toBeVisible();
});

test('hover offers direct natural split without changing text or opening a modal', async ({
  page
}, info) => {
  const item = segment();
  for (const mapping of Object.values(item.passage_structure!.layers)) {
    mapping.boundaries[0].natural = true;
    mapping.boundaries[0].warning = null;
  }
  const state = await setup(page, item);
  const dot = page.getByRole('button', { name: /Passages 173 \/ 174/ });
  await dot.hover();
  const menu = page.getByRole('menu', { name: 'Passage boundary actions' });
  await expect(menu).toBeVisible();
  expect(state.getSplit()).toBeUndefined();
  expect(
    await page.locator('[data-passage-segment="passage-segment"]').textContent()
  ).toBe(text);
  await menu.screenshot({
    path: `../tmp/passage-menu-${info.project.name}.png`
  });
  await menu.getByRole('menuitem', { name: 'Split here', exact: true }).click();
  await expect.poll(state.getSplit).toMatchObject({
    passage_boundary_id: boundary.id,
    expected_revision_id: 'plan-a'
  });
  await expect(
    page.getByRole('dialog', { name: 'Inspect passage boundary' })
  ).not.toBeVisible();
});

test('a single passage stays left-aligned and directly editable without an extra note', async ({
  page
}) => {
  const item = segment();
  for (const mapping of Object.values(item.passage_structure!.layers))
    mapping.boundaries = [];
  await setup(page, item);
  const editor = page.getByRole('textbox', {
    name: 'Script text for segment 54',
    exact: true
  });
  await expect(editor).toBeVisible();
  await expect(editor).toHaveValue(text);
  expect(['left', 'start']).toContain(
    await editor.evaluate((node) => getComputedStyle(node).textAlign)
  );
  await expect(
    page.getByText('One timed passage; no internal timing anchors.', {
      exact: true
    })
  ).toHaveCount(0);
  await expect(
    page.getByRole('button', { name: 'Edit text for segment 54', exact: true })
  ).toHaveCount(0);
});

test('passage markers default on and remember a hidden preference after reload', async ({
  page
}) => {
  const state = await setup(page);
  const dot = page.getByRole('button', { name: /Passages 173 \/ 174/ });
  await expect(dot).toBeVisible();
  await page
    .getByRole('button', { name: 'Display options', exact: true })
    .click();
  await page
    .getByRole('button', { name: 'Hide passage boundaries', exact: true })
    .click();
  await expect(dot).toHaveCount(0);
  await page.reload();
  await page.getByRole('button', { name: 'Generation', exact: true }).click();
  await expect(dot).toHaveCount(0);
  await page
    .getByRole('button', { name: 'Display options', exact: true })
    .click();
  await page
    .getByRole('button', { name: 'Show passage boundaries', exact: true })
    .click();
  await expect(dot).toBeVisible();
  expect(state.getSplit()).toBeUndefined();
});

test('blocked boundaries retain preview but cannot split and Escape returns focus', async ({
  page
}) => {
  const item = segment();
  for (const mapping of Object.values(item.passage_structure!.layers)) {
    mapping.boundaries[0].natural = true;
    mapping.boundaries[0].split_allowed = false;
    mapping.boundaries[0].split_blocked_reason = 'The timing windows overlap.';
  }
  const state = await setup(page, item);
  const dot = page.getByRole('button', { name: /Passages 173 \/ 174/ });
  await dot.focus();
  await page.keyboard.press('ArrowDown');
  const menu = page.getByRole('menu', { name: 'Passage boundary actions' });
  await expect(menu).toBeVisible();
  await expect(
    menu.getByRole('menuitem', { name: 'Split here', exact: true })
  ).toBeDisabled();
  await expect(menu).toContainText('The timing windows overlap.');
  await page.keyboard.press('Escape');
  await expect(menu).not.toBeVisible();
  await expect(dot).toBeFocused();
  expect(state.getSplit()).toBeUndefined();
});
