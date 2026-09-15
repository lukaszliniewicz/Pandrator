import { apiJson } from './api';
import { notifySessionFlowChange } from './session-flow';

export type RepairBatch = {
  id: string;
  base_revision_id: string;
  result_revision_id: string;
  attempt_count: number;
  applied_count: number;
  rejected_count: number;
  status: string;
  can_undo: boolean;
  undo_disabled_reason: string | null;
  expected_revision_id: string;
  expected_state_hash: string | null;
};
export type RepairCheckpoint = {
  id: string;
  revision_number: number;
  repair_status: string | null;
  repair_reason: string | null;
  source_block_ordinal: number | null;
};
export type RepairBatchDetail = {
  repair_batch: RepairBatch;
  active_revision_id: string | null;
  items: RepairCheckpoint[];
  next_before_revision_number: number | null;
};

function batchPath(sessionId: string, batchId: string) {
  return `/api/v1/sessions/${encodeURIComponent(sessionId)}/generation-plan/repair-batches/${encodeURIComponent(batchId)}`;
}

export function loadRepairBatch(
  sessionId: string,
  batchId: string,
  before?: number | null
) {
  const query = new URLSearchParams({ limit: '20' });
  if (before != null) query.set('before_revision_number', String(before));
  return apiJson<RepairBatchDetail>(
    `${batchPath(sessionId, batchId)}?${query}`
  );
}

export async function undoRepairBatch(sessionId: string, batch: RepairBatch) {
  if (!batch.can_undo || !batch.expected_state_hash)
    throw new Error(
      batch.undo_disabled_reason || 'A verified repair snapshot is required.'
    );
  const result = await apiJson<{ plan_revision_id: string }>(
    `${batchPath(sessionId, batch.id)}/undo`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expected_revision_id: batch.expected_revision_id,
        expected_state_hash: batch.expected_state_hash
      })
    }
  );
  notifySessionFlowChange(sessionId);
  return result;
}
