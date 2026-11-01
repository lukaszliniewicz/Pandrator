/** Account isolation for browser-only caches. Authority always stays on the server. */
let identity = 'anonymous';
const resets = new Set<() => void>();

export function setBrowserIdentity(
  instanceId: string,
  userId: string,
  revision: number
) {
  const next = JSON.stringify([instanceId, userId, revision]);
  if (next === identity) return;
  identity = next;
  for (const reset of resets) reset();
}

export function onBrowserIdentityReset(reset: () => void) {
  resets.add(reset);
  return () => resets.delete(reset);
}

export function browserStorageKey(
  name: string,
  resourceId = '',
  workspaceId = ''
) {
  return `pandrator:account:${identity}:${JSON.stringify([workspaceId, name, resourceId])}`;
}
