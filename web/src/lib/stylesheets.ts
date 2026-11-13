// Links that failed or never settled are not waited for again.
const abandoned = new WeakSet<HTMLLinkElement>();

/**
 * Resolve once every stylesheet link in the document has loaded.
 *
 * SvelteKit warms each route module before loading it for rendering, and
 * Vite's preload helper waits for a stylesheet only on the first request.
 * The second request, whose result is rendered, can therefore resolve while
 * the route's CSS is still arriving: the new page paints unstyled and then
 * reflows under the pointer. Awaiting this in `onNavigate` keeps the swap
 * atomic. A failed or slow sheet ends the wait and is skipped afterwards.
 */
export function stylesheetsSettled(timeoutMs = 4_000): Promise<void> {
  const pending = Array.from(
    document.querySelectorAll<HTMLLinkElement>('link[rel="stylesheet"]')
  ).filter((link) => !link.sheet && !abandoned.has(link));
  if (!pending.length) return Promise.resolve();
  return new Promise((resolve) => {
    const waiting = new Set(pending);
    const finish = () => {
      window.clearTimeout(timer);
      for (const link of waiting) abandoned.add(link);
      resolve();
    };
    const timer = window.setTimeout(finish, timeoutMs);
    for (const link of pending) {
      const settle = (event: Event) => {
        if (event.type === 'error') abandoned.add(link);
        waiting.delete(link);
        if (!waiting.size) finish();
      };
      link.addEventListener('load', settle, { once: true });
      link.addEventListener('error', settle, { once: true });
    }
  });
}
