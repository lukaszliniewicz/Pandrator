import { getContext } from 'svelte';
import type { LoadState, OutcomePlan, SessionRecord } from './api-models';
import type { WorkflowStore } from './workflow-store.svelte';

export const SESSION_CONTEXT = Symbol('pandrator-session');
export const SOURCE_SESSION_CONTEXT = Symbol('pandrator-source-session');
export type SessionContext = {
  readonly session: SessionRecord | null;
  readonly outcome: OutcomePlan | null;
  readonly status: LoadState;
  readonly loading: boolean;
  readonly error: string;
  readonly workflow: WorkflowStore;
  reload: () => Promise<void>;
  customize: () => void;
};
export const useSessionContext = () =>
  getContext<SessionContext>(SESSION_CONTEXT);
export const useSourceSessionContext = () =>
  getContext<SessionContext>(SOURCE_SESSION_CONTEXT);
