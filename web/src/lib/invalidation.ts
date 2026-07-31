export const INVALIDATION_RESOURCES = [
  'jobs',
  'sessions',
  'workflow',
  'generation',
  'sources',
  'output',
  'voices',
  'training',
  'capabilities'
] as const;

export type InvalidationResource = (typeof INVALIDATION_RESOURCES)[number];

export type PandratorServerEvent = {
  type: string;
  job_id?: string | null;
  job_kind?: string;
  session_id?: string | null;
  status?: string;
  progress?: number;
  detail?: string | null;
  created_at?: string;
  changed_entities?: InvalidationResource[];
  [key: string]: unknown;
};

export type InvalidationBatch = {
  resources: InvalidationResource[];
  session_ids: string[];
  job_ids: string[];
  events: PandratorServerEvent[];
};

const KNOWN_RESOURCES = new Set<string>(INVALIDATION_RESOURCES);

type InvalidationListener = (batch: InvalidationBatch) => void;

class InvalidationBus {
  private listeners = new Set<InvalidationListener>();

  publish(batch: InvalidationBatch) {
    for (const listener of this.listeners) listener(batch);
  }

  subscribe(listener: InvalidationListener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}

export const invalidationBus = new InvalidationBus();

function resourcesFor(event: PandratorServerEvent): InvalidationResource[] {
  const supplied = Array.isArray(event.changed_entities)
    ? event.changed_entities.filter((item): item is InvalidationResource =>
        KNOWN_RESOURCES.has(item)
      )
    : [];
  return supplied.length ? Array.from(new Set(supplied)) : ['jobs'];
}

export class InvalidationCoordinator {
  private timer?: number;
  private events: PandratorServerEvent[] = [];
  private resources = new Set<InvalidationResource>();
  private sessionIds = new Set<string>();
  private jobIds = new Set<string>();

  constructor(
    private readonly onFlush: (batch: InvalidationBatch) => void,
    private readonly delayMs = 160
  ) {}

  enqueue(event: PandratorServerEvent) {
    this.events.push(event);
    for (const resource of resourcesFor(event)) this.resources.add(resource);
    if (event.session_id) this.sessionIds.add(String(event.session_id));
    if (event.job_id) this.jobIds.add(String(event.job_id));
    if (this.timer === undefined) {
      this.timer = window.setTimeout(() => this.flush(), this.delayMs);
    }
  }

  flush() {
    if (this.timer !== undefined) window.clearTimeout(this.timer);
    this.timer = undefined;
    if (!this.events.length) return;
    const batch: InvalidationBatch = {
      resources: Array.from(this.resources),
      session_ids: Array.from(this.sessionIds),
      job_ids: Array.from(this.jobIds),
      events: this.events
    };
    this.events = [];
    this.resources.clear();
    this.sessionIds.clear();
    this.jobIds.clear();
    this.onFlush(batch);
  }

  dispose() {
    if (this.timer !== undefined) window.clearTimeout(this.timer);
    this.timer = undefined;
    this.events = [];
    this.resources.clear();
    this.sessionIds.clear();
    this.jobIds.clear();
  }
}

export function invalidates(
  batch: InvalidationBatch,
  resource: InvalidationResource,
  sessionId?: string
) {
  if (!batch.resources.includes(resource)) return false;
  if (!sessionId) return true;
  return (
    batch.session_ids.length === 0 || batch.session_ids.includes(sessionId)
  );
}
