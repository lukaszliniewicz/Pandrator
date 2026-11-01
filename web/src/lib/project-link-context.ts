/** Per-tab selector and CSRF only. Authentication stays in an HttpOnly cookie. */
export type ProjectLinkContext = {
  id: string;
  csrf: string;
  mode: 'manage_project' | 'review_translation';
  projectId: string;
  rootSessionId: string;
  label: string;
};

const KEY = 'pandrator-project-link-context';
let current: ProjectLinkContext | null | undefined;
let channel: BroadcastChannel | null = null;

function connectTabs() {
  if (
    channel ||
    typeof BroadcastChannel === 'undefined' ||
    typeof window === 'undefined'
  )
    return;
  channel = new BroadcastChannel('pandrator-project-link-context');
  channel.onmessage = (message) => {
    const next = message.data as ProjectLinkContext;
    if (
      !current ||
      !next ||
      next.id !== current.id ||
      next.mode !== current.mode ||
      next.projectId !== current.projectId ||
      next.rootSessionId !== current.rootSessionId ||
      typeof next.csrf !== 'string' ||
      next.csrf.length > 256
    )
      return;
    current = { ...current, csrf: next.csrf };
    try {
      sessionStorage.setItem(KEY, JSON.stringify(current));
    } catch {
      /* The tab still works. */
    }
  };
}

export function activeProjectLink(): ProjectLinkContext | null {
  if (current !== undefined) return current;
  if (typeof window === 'undefined') return null;
  try {
    const value = JSON.parse(sessionStorage.getItem(KEY) ?? 'null');
    current =
      value &&
      /^[\da-f-]{36}$/i.test(value.id) &&
      ['manage_project', 'review_translation'].includes(value.mode) &&
      typeof value.csrf === 'string'
        ? value
        : null;
  } catch {
    current = null;
  }
  if (current) connectTabs();
  return current ?? null;
}

export function selectProjectLink(value: ProjectLinkContext) {
  current = value;
  connectTabs();
  channel?.postMessage(value);
  try {
    sessionStorage.setItem(KEY, JSON.stringify(value));
  } catch {
    /* This tab still works. */
  }
}

export function clearProjectLink() {
  current = null;
  channel?.close();
  channel = null;
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    /* No persisted selector. */
  }
}

export function projectLinkHeaders(headers: Headers, mutation: boolean) {
  const selected = activeProjectLink();
  if (!selected) return;
  headers.set('X-Pandrator-Project-Link', selected.id);
  if (mutation) headers.set('X-CSRF-Token', selected.csrf);
}

/** Project visitors receive a finite project resource projection, not workspace catalogues. */
export function projectLinkApiPath(value: string): string {
  const selected = activeProjectLink();
  if (!selected || typeof location === 'undefined') return value;
  const url = new URL(value, location.origin);
  const catalogue = /^\/api\/v1\/services\/(tts|stt)$/.exec(url.pathname);
  if (!catalogue) return value;
  const sessionId =
    /^\/sessions\/([^/]+)/.exec(location.pathname)?.[1] ??
    selected.rootSessionId;
  url.pathname = `/api/v1/public-links/${selected.id}/services/${catalogue[1]}`;
  url.searchParams.delete('workspace_id');
  url.searchParams.set('session_id', sessionId);
  return `${url.pathname}${url.search}`;
}

/** Native media, downloads and SSE cannot attach a custom request header. */
export function projectLinkUrl(value: string): string {
  const selected = activeProjectLink();
  if (!selected || !value || typeof location === 'undefined') return value;
  const url = new URL(value, location.origin);
  if (url.origin !== location.origin || !url.pathname.startsWith('/api/v1/'))
    return value;
  url.searchParams.set('project_link_id', selected.id);
  return `${url.pathname}${url.search}${url.hash}`;
}
