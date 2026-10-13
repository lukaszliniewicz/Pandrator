import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';
import type { ProjectFolderSnapshot } from '../src/lib/project-library';

async function signIn(page: Page) {
  await page.goto('/');
  await page.getByLabel('Owner password').fill('pandrator-e2e');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
  const tour = page.getByRole('button', { name: 'Close tour' });
  if (await tour.isVisible()) await tour.click();
  return {
    'X-CSRF-Token': (
      await (await page.request.get('/api/v1/auth/status')).json()
    ).csrf_token
  };
}

async function multilingualProject(
  page: Page,
  headers: Record<string, string>,
  name: string
) {
  const created = await page.request.post('/api/v1/sessions', {
    headers,
    data: {
      name,
      interface_mode: 'advanced',
      workflow_kind: 'voiceover',
      source_language: 'en',
      target_language: 'de',
      included_stages: ['transcribe', 'translate', 'tts', 'export']
    }
  });
  expect(created.ok(), await created.text()).toBeTruthy();
  const source = await created.json();
  const uploaded = await page.request.post('/api/v1/uploads', {
    headers,
    multipart: {
      session_id: source.id,
      file: {
        name: 'course-session.srt',
        mimeType: 'application/x-subrip',
        buffer: Buffer.from(
          '1\n00:00:00,000 --> 00:00:03,000\nOne shared source for every language.\n'
        )
      }
    }
  });
  expect(uploaded.ok(), await uploaded.text()).toBeTruthy();
  const adopted = await page.request.post(
    `/api/v1/sessions/${source.id}/sources/adopt-subtitles`,
    {
      headers,
      data: {
        source_asset_id: (await uploaded.json()).source_asset_id,
        role: 'primary'
      }
    }
  );
  expect(adopted.ok(), await adopted.text()).toBeTruthy();
  const catalog = await (
    await page.request.get(`/api/v1/sessions/${source.id}/subtitles/catalog`)
  ).json();
  const checkpoint = catalog.items.find(
    (item: { stage: string; state: string }) =>
      item.stage === 'transcription' && item.state === 'current'
  );
  expect(checkpoint).toBeTruthy();
  const fresh = await (
    await page.request.get(`/api/v1/sessions/${source.id}`)
  ).json();
  const aggregate = await page.request.post(
    `/api/v1/sessions/${source.id}/translation-project`,
    {
      headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() },
      data: {
        name,
        expected_revision: fresh.revision,
        checkpoint_artifact_id: checkpoint.artifact_id
      }
    }
  );
  expect(aggregate.ok(), await aggregate.text()).toBeTruthy();
  const project = (await aggregate.json()).project;
  const branch = await page.request.post(
    `/api/v1/translation-projects/${project.id}/branches`,
    {
      headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() },
      data: {
        expected_revision: project.revision,
        targets: [{ target_language: 'ja' }]
      }
    }
  );
  expect(branch.ok(), await branch.text()).toBeTruthy();
  return { source, project: (await branch.json()).project };
}

async function createFolder(page: Page, name: string, parentId = '') {
  await page.getByRole('button', { name: 'New folder', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New folder', exact: true });
  await dialog.getByLabel('Folder name', { exact: true }).fill(name);
  await dialog
    .getByLabel('Parent folder', { exact: true })
    .selectOption(parentId);
  await dialog
    .getByRole('button', { name: 'Create folder', exact: true })
    .click();
  await expect(dialog).toBeHidden();
  const snapshot = (await (
    await page.request.get('/api/v1/project-folders')
  ).json()) as ProjectFolderSnapshot;
  return snapshot.items.find((folder) => folder.name === name)!;
}

test('course folders organize the whole multilingual project, retain independent order, and delete only folder metadata', async ({
  page
}, info) => {
  test.setTimeout(90_000);
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const headers = await signIn(page);
  const stamp = crypto.randomUUID();
  const firstName = `Session one ${stamp}`;
  const secondName = `Session two ${stamp}`;
  const { source, project } = await multilingualProject(
    page,
    headers,
    firstName
  );
  const secondResponse = await page.request.post('/api/v1/sessions', {
    headers,
    data: {
      name: secondName,
      workflow_kind: 'audiobook',
      source_language: 'en'
    }
  });
  expect(secondResponse.ok()).toBeTruthy();
  const second = await secondResponse.json();
  await page.goto('/sessions');
  const course = await createFolder(page, `Consciousness course ${stamp}`);
  const module = await createFolder(page, `Module one ${stamp}`, course.id);
  await page.goto('/sessions');
  await page
    .getByRole('button', { name: `Folders for ${firstName}`, exact: true })
    .click();
  const assignment = page.getByRole('dialog', {
    name: 'Project folders',
    exact: true
  });
  await assignment
    .getByRole('checkbox', { name: course.name, exact: true })
    .check();
  await assignment
    .getByRole('checkbox', {
      name: `${course.name} / ${module.name}`,
      exact: true
    })
    .check();
  const findings = await new AxeBuilder({ page })
    .include('[role="dialog"]')
    .analyze();
  expect(
    findings.violations.filter((item) =>
      ['serious', 'critical'].includes(item.impact ?? '')
    )
  ).toEqual([]);
  await assignment
    .getByRole('button', { name: 'Save folders', exact: true })
    .click();
  await expect(assignment).toBeHidden();
  await page
    .getByRole('button', { name: `Folders for ${secondName}`, exact: true })
    .click();
  await assignment
    .getByRole('checkbox', { name: course.name, exact: true })
    .check();
  await assignment
    .getByRole('button', { name: 'Save folders', exact: true })
    .click();
  await page
    .getByRole('button', { name: `Open folder ${course.name}`, exact: true })
    .click();
  await expect(
    page.locator('article[data-project-id]').first()
  ).toHaveAttribute('data-project-id', source.id);
  await page
    .getByRole('button', { name: `Move ${secondName} up`, exact: true })
    .click();
  await expect(
    page.locator('article[data-project-id]').first()
  ).toHaveAttribute('data-project-id', second.id);
  await page.reload();
  await expect(
    page.locator('article[data-project-id]').first()
  ).toHaveAttribute('data-project-id', second.id);
  await page
    .getByRole('button', {
      name: `Expand languages for ${firstName}`,
      exact: true
    })
    .click();
  const row = page.locator(`[data-project-id="${source.id}"]`);
  await expect(row.getByRole('link', { name: /^German/ })).toBeVisible();
  await row.getByRole('link', { name: /^Japanese/ }).click();
  await expect(page.getByLabel('Project language')).toHaveValue(
    project.branches[0].session_id
  );
  await expect(
    page.getByRole('navigation', { name: 'Project location' })
  ).toContainText(course.name);
  await page
    .getByRole('button', { name: `Rename project ${firstName}` })
    .click();
  const renamed = `Renamed session one ${stamp}`;
  await page.getByLabel('Project name', { exact: true }).fill(renamed);
  await page
    .getByRole('button', { name: 'Save project name', exact: true })
    .click();
  await expect(
    page.getByRole('heading', { name: renamed, exact: true })
  ).toBeVisible();
  const originalSource = await (
    await page.request.get(`/api/v1/sessions/${source.id}`)
  ).json();
  expect(originalSource.name).toBe(firstName);
  await page
    .getByRole('navigation', { name: 'Project location' })
    .getByRole('link', { name: course.name, exact: true })
    .click();
  await expect(
    page.getByRole('link', { name: renamed, exact: true })
  ).toBeVisible();
  await page.getByRole('button', { name: 'Edit folder', exact: true }).click();
  const edit = page.getByRole('dialog', { name: 'Edit folder', exact: true });
  await expect(edit.getByLabel('Parent folder', { exact: true })).toHaveValue(
    ''
  );
  await edit
    .getByRole('button', { name: 'Delete folder', exact: true })
    .click();
  await page
    .getByRole('dialog', { name: 'Delete folder', exact: true })
    .getByRole('button', { name: 'Delete folder', exact: true })
    .click();
  await expect(page.getByRole('dialog')).toBeHidden();
  const remaining = (await (
    await page.request.get('/api/v1/project-folders')
  ).json()) as ProjectFolderSnapshot;
  expect(
    remaining.items.find((folder) => folder.id === module.id)?.parent_id
  ).toBeNull();
  expect(
    remaining.memberships
      .filter((member) => member.project_id === source.id)
      .map((member) => member.folder_id)
  ).toEqual([module.id]);
  expect(
    (
      await page.request.get(
        `/api/v1/sessions/${project.branches[0].session_id}`
      )
    ).ok()
  ).toBeTruthy();
  await page
    .getByRole('button', { name: `Open folder ${module.name}`, exact: true })
    .click();
  await expect(page.locator('article[data-project-id]')).toHaveCount(1);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole('combobox', { name: 'Folder', exact: true })
  ).toHaveValue(module.id);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth
    )
  ).toBeTruthy();
  await page
    .getByRole('combobox', { name: 'Folder', exact: true })
    .selectOption('unfiled');
  await expect(
    page.getByRole('link', { name: secondName, exact: true })
  ).toBeVisible();
  await expect(
    page.getByRole('link', { name: renamed, exact: true })
  ).toHaveCount(0);
  await info.attach('project-folders.json', {
    contentType: 'application/json',
    body: JSON.stringify(remaining)
  });
  expect(errors).toEqual([]);
});
