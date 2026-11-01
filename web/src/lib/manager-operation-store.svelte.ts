import { errorMessage } from './errors';
import { managerApi } from './admin-api';
import { browserStorageKey, onBrowserIdentityReset } from './browser-identity';

export type ManagerOperation = {
  id: string;
  state: string;
  progress: number;
  current_task_id?: string | null;
  error_message?: string | null;
};

type ManagerStatus = {
  available: boolean;
  configured?: boolean;
  error?: { code: string; message: string };
  status?: {
    configuration_revision?: number;
    active_operation_id?: string | null;
  };
};

export const MANAGER_TERMINAL_STATES = new Set([
  'succeeded',
  'failed',
  'cancelled',
  'recovery_required'
]);

type OperationListener = (operation: ManagerOperation | null) => void;

class ManagerOperationStore {
  status = $state<ManagerStatus | null>(null);
  operation = $state<ManagerOperation | null>(null);
  error = $state('');

  private listeners = new Set<OperationListener>();
  private consumers = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private inFlight: Promise<void> | null = null;
  private stopped = true;
  private lastNotification = '';
  private identityEpoch = 0;

  constructor() {
    onBrowserIdentityReset(() => {
      this.identityEpoch += 1;
      this.status = null;
      this.operation = null;
      this.error = '';
      this.lastNotification = '';
    });
  }

  connect(listener?: OperationListener) {
    this.consumers += 1;
    if (listener) this.listeners.add(listener);
    if (this.consumers === 1) {
      this.stopped = false;
      void this.poll();
    } else if (listener) {
      listener(this.operation);
    }

    return () => {
      if (listener) this.listeners.delete(listener);
      this.consumers = Math.max(0, this.consumers - 1);
      if (this.consumers === 0) {
        this.stopped = true;
        if (this.timer) clearTimeout(this.timer);
        this.timer = null;
      }
    };
  }

  async refresh(knownStatus?: ManagerStatus) {
    if (this.inFlight) return this.inFlight;
    this.inFlight = this.load(knownStatus).finally(() => {
      this.inFlight = null;
    });
    return this.inFlight;
  }

  setOperation(operation: ManagerOperation | null) {
    this.remember(operation);
  }

  async cancel() {
    const operation = this.operation;
    if (!operation || MANAGER_TERMINAL_STATES.has(operation.state)) return;
    try {
      await managerApi.cancel(operation.id);
      await this.refresh();
    } catch (caught) {
      this.error = errorMessage(caught);
    }
  }

  private async load(knownStatus?: ManagerStatus) {
    const identityEpoch = this.identityEpoch;
    try {
      const status = knownStatus ?? (await managerApi.status<ManagerStatus>());
      if (identityEpoch !== this.identityEpoch) return;
      this.status = status;
      if (!status.available) {
        this.remember(null);
        this.error = '';
        return;
      }

      const storedId =
        typeof localStorage === 'undefined'
          ? null
          : localStorage.getItem(browserStorageKey('manager-operation'));
      const operationId =
        status.status?.active_operation_id ??
        (this.operation && !MANAGER_TERMINAL_STATES.has(this.operation.state)
          ? this.operation.id
          : storedId);

      if (operationId) {
        const operation =
          await managerApi.operation<ManagerOperation>(operationId);
        if (identityEpoch !== this.identityEpoch) return;
        this.remember(operation);
      } else if (
        !this.operation ||
        !MANAGER_TERMINAL_STATES.has(this.operation.state)
      ) {
        this.remember(null);
      }
      this.error = '';
    } catch (caught) {
      if (identityEpoch === this.identityEpoch)
        this.error = errorMessage(caught);
    }
  }

  private remember(operation: ManagerOperation | null) {
    this.operation = operation;
    if (typeof localStorage !== 'undefined') {
      if (operation && !MANAGER_TERMINAL_STATES.has(operation.state)) {
        localStorage.setItem(
          browserStorageKey('manager-operation'),
          operation.id
        );
      } else {
        localStorage.removeItem(browserStorageKey('manager-operation'));
      }
    }

    const signature = operation
      ? `${operation.id}:${operation.state}:${operation.progress}:${operation.current_task_id ?? ''}:${operation.error_message ?? ''}`
      : '';
    if (signature === this.lastNotification) return;
    this.lastNotification = signature;
    for (const listener of this.listeners) listener(operation);
  }

  private async poll() {
    if (this.stopped) return;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    await this.refresh();
    if (this.stopped) return;
    const delay =
      this.operation && !MANAGER_TERMINAL_STATES.has(this.operation.state)
        ? 1000
        : 5000;
    this.timer = setTimeout(() => void this.poll(), delay);
  }
}

export const managerOperationStore = new ManagerOperationStore();
