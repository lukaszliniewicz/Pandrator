import { expect, type Page } from '@playwright/test';

export async function mockExportJobs(
  page: Page,
  sessionId: string,
  loadItems: () => object[] | Promise<object[]>
) {
  await page.route('**/api/v1/jobs?**', async (route) => {
    const query = new URL(route.request().url()).searchParams;
    if (query.get('session_id') !== sessionId) return route.fallback();
    expect(query.get('limit')).toBe('8');
    const kind = query.get('kind');
    expect(['export.create', 'export.variant']).toContain(kind);
    const items = await loadItems();
    return route.fulfill({
      json: {
        items: items.filter((item) => 'kind' in item && item.kind === kind)
      }
    });
  });
}
