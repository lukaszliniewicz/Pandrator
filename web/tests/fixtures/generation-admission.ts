import type { Page, Route } from '@playwright/test';

type RunFixtureHandler = (route: Route) => Promise<void> | void;

/** Supply run-list fixtures and the durable admission/job/run handshake. */
export async function mockGenerationRuns(
  page: Page,
  sessionId: string,
  handler: RunFixtureHandler
) {
  const jobPrefix = `generation-fixture-${crypto.randomUUID()}`;
  const jobs = new Map<string, string>();
  const runs = new Map<string, Record<string, unknown>>();
  const fixtureRoute = (route: Route, admission: boolean) =>
    new Proxy(route, {
      get(target, property) {
        if (property === 'fulfill')
          return async (options: Parameters<Route['fulfill']>[0]) => {
            if ((options?.status ?? 200) >= 400) return target.fulfill(options);
            const value: unknown =
              options?.json ?? JSON.parse(String(options?.body ?? 'null'));
            if (!admission) {
              if (
                value &&
                typeof value === 'object' &&
                'items' in value &&
                Array.isArray(value.items)
              )
                for (const item of value.items) {
                  if (
                    item &&
                    typeof item === 'object' &&
                    'id' in item &&
                    typeof item.id === 'string'
                  )
                    runs.set(item.id, item as Record<string, unknown>);
                }
              return target.fulfill(options);
            }
            if (
              !value ||
              typeof value !== 'object' ||
              !('id' in value) ||
              typeof value.id !== 'string'
            )
              throw new Error(
                'A generation fixture must return a run with an id.'
              );
            const jobId = `${jobPrefix}-${jobs.size + 1}`;
            jobs.set(jobId, value.id);
            runs.set(value.id, value as Record<string, unknown>);
            await target.fulfill({ status: 202, json: { job_id: jobId } });
          };
        const member: unknown = Reflect.get(target, property, target);
        return typeof member === 'function' ? member.bind(target) : member;
      }
    });
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-runs*`,
    (route) => handler(fixtureRoute(route, false))
  );
  await page.route(`**/api/v1/jobs/${jobPrefix}-*`, async (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-1)!;
    await route.fulfill({
      json: {
        id,
        status: 'succeeded',
        result_json: { generation_run_id: jobs.get(id) }
      }
    });
  });
  await page.route('**/api/v1/generation-runs/*', async (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-1)!;
    const run = runs.get(id);
    if (!run) return route.fallback();
    await route.fulfill({ json: run });
  });
  await page.route(
    `**/api/v1/sessions/${sessionId}/generation-admissions`,
    async (route) => {
      await handler(fixtureRoute(route, true));
    }
  );
}
