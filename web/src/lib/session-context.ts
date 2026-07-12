import { getContext } from 'svelte';
import type { SessionRecord } from './api';

export const SESSION_CONTEXT = Symbol('pandrator-session');
export type SessionContext = {
  session: SessionRecord | null;
  outcome: any;
  loading: boolean;
  error: string;
  reload: () => Promise<void>;
  customize: () => void;
};
export const useSessionContext = () => getContext<SessionContext>(SESSION_CONTEXT);
