import { errorMessage } from './errors';
import { managerApi } from './admin-api';

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

const MANAGER_OPERATION_STORAGE_KEY = 'pandrator-manager-operation';
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
    try {
      const status = knownStatus ?? (await managerApi.status<ManagerStatus>());
      this.status = status;
      if (!status.available) {
        this.remember(null);
        this.error = '';
        return;
      }

      const storedId =
        typeof localStorage === 'undefined'
          ? null
          : localStorage.getItem(MANAGER_OPERATION_STORAGE_KEY);
      const operationId =
        status.status?.active_operation_id ??
        (this.operation && !MANAGER_TERMINAL_STATES.has(this.operation.state)
          ? this.operation.id
          : storedId);

      if (operationId) {
        this.remember(
          await managerApi.operation<ManagerOperation>(operationId)
        );
      } else if (
        !this.operation ||
        !MANAGER_TERMINAL_STATES.has(this.operation.state)
      ) {
        this.remember(null);
      }
      this.error = '';
    } catch (caught) {
      this.error = errorMessage(caught);
    }
  }

  private remember(operation: ManagerOperation | null) {
    this.operation = operation;
    if (typeof localStorage !== 'undefined') {
      if (operation && !MANAGER_TERMINAL_STATES.has(operation.state)) {
        localStorage.setItem(MANAGER_OPERATION_STORAGE_KEY, operation.id);
      } else {
        localStorage.removeItem(MANAGER_OPERATION_STORAGE_KEY);
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
