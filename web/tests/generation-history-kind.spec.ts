import { expect, test } from '@playwright/test';
import type { GenerationRun } from '../src/lib/api-models';
import {
  generationHistoryStatus,
  historyAriaLabel,
  historyCounts,
  historyDetailsSummary,
  historyHeading,
  historyHelper,
  historyInspectLabel,
  historyVersionOutcome,
  historyVersionReason,
  historyVersionTitle,
  secondPassKind
} from '../src/lib/generation-history';

function baseRun(overrides: Partial<GenerationRun> = {}): GenerationRun {
  return {
    id: 'root',
    session_id: 'session-1',
    plan_revision_id: 'plan-1',
    sequence_number: 1,
    operation: 'generate',
    label: 'Run 8: Test voice',
    status: 'completed',
    progress: 1,
    ...overrides
  } as GenerationRun;
}

function version(
  id: string,
  repair_status: string,
  repair_reason: string | null = null,
  extra: Record<string, unknown> = {}
) {
  return {
    generation_run_id: id,
    plan_revision_id: `plan-${id}`,
    sequence_number: 2,
    status: 'completed',
    repair_status,
    repair_reason,
    ...extra
  };
}

/** Run8 shape: 19 passage_regroup attempts, 9 applied, 10 duration_misfit. */
function run8(): GenerationRun {
  const versions = [
    ...Array.from({ length: 9 }, (_, i) =>
      version(`regroup-${i + 1}`, 'applied', null, {
        reason: 'passage_regroup'
      })
    ),
    ...Array.from({ length: 10 }, (_, i) =>
      version(`regroup-${10 + i}`, 'not_applied', 'duration_misfit', {
        reason: 'passage_regroup'
      })
    )
  ];
  return baseRun({
    id: 'b5db8037-67be-468e-ba6d-98a7827241e5',
    label: 'Run8: Test voice',
    second_pass: 'regroup',
    settings_snapshot: {
      tts: {
        speech_block_generation_mode: 'passage',
        speech_block_regroup_enabled: true
      }
    },
    timing_repair: {
      result_generation_run_id: 'regroup-9',
      result_plan_revision_id: 'plan-regroup-9',
      result_sequence_number: 10,
      applied_count: 9,
      attempt_count: 19,
      status: 'completed',
      kind: 'regroup',
      versions
    }
  });
}

test('regroup complete uses merge wording and attempt-based counts', () => {
  const run = run8();
  expect(secondPassKind(run)).toBe('regroup');
  expect(historyHeading(run)).toBe('9 groups merged');
  expect(historyHelper(run)).toBe(
    '19 groups regenerated: 9 accepted, 10 rejected.'
  );
  expect(historyCounts(run.timing_repair!)).toEqual({
    attempted: 19,
    applied: 9,
    rejected: 10,
    pending: 0
  });
  expect(generationHistoryStatus(run)).toBe('completed · 9 groups merged');
  expect(historyAriaLabel(run)).toBe('Passage regroup history');
  expect(historyDetailsSummary(run)).toBe('Regroup details · 19 groups');
  expect(historyVersionTitle(run, 0)).toBe('Group 1');
  expect(historyInspectLabel(run, 0)).toBe('Inspect group 1 audio');
  expect(historyVersionOutcome(run, 'applied')).toBe('Merge applied');
  expect(historyVersionOutcome(run, 'not_applied')).toBe('Kept previous audio');
  expect(historyVersionReason('duration_misfit')).toContain('timing window');
});

test('regroup running says combining passages', () => {
  const run = run8();
  run.phase = 'repairing_timing';
  run.timing_repair!.status = 'running';
  expect(historyHeading(run)).toBe('Combining passages');
  expect(generationHistoryStatus(run)).toBe('Combining passages');
  expect(historyHelper(run)).toContain(
    'Accepted groups become part of this run'
  );
});

test('all-accepted regroup needs no duration_misfit to read as merge', () => {
  const run = baseRun({
    second_pass: 'regroup',
    timing_repair: {
      result_generation_run_id: 'g3',
      result_plan_revision_id: 'plan-g3',
      result_sequence_number: 4,
      applied_count: 3,
      attempt_count: 3,
      status: 'completed',
      kind: 'regroup',
      versions: [
        version('g1', 'applied', null, { reason: 'passage_regroup' }),
        version('g2', 'applied', null, { reason: 'passage_regroup' }),
        version('g3', 'applied', null, { reason: 'passage_regroup' })
      ]
    }
  });
  expect(secondPassKind(run)).toBe('regroup');
  expect(historyHeading(run)).toBe('3 groups merged');
  expect(historyHelper(run)).toBe(
    '3 groups regenerated: 3 accepted, 0 rejected.'
  );
  expect(generationHistoryStatus(run)).toBe('completed · 3 groups merged');
  expect(historyVersionOutcome(run, 'applied')).toBe('Merge applied');
});

test('initial running regroup with zero accepted still says combining', () => {
  const run = baseRun({
    status: 'running',
    phase: 'repairing_timing',
    second_pass: 'regroup',
    timing_repair: {
      result_generation_run_id: 'root',
      result_plan_revision_id: 'plan-1',
      result_sequence_number: 1,
      applied_count: 0,
      attempt_count: 2,
      status: 'running',
      kind: 'regroup',
      versions: [
        version('g1', 'pending', null, { reason: 'passage_regroup' }),
        version('g2', 'pending', null, { reason: 'passage_regroup' })
      ]
    }
  });
  expect(historyHeading(run)).toBe('Combining passages');
  expect(generationHistoryStatus(run)).toBe('Combining passages');
  expect(historyCounts(run.timing_repair!)).toEqual({
    attempted: 2,
    applied: 0,
    rejected: 0,
    pending: 2
  });
});

test('failed regroup names merge failure and keeps last accepted audio', () => {
  const run = baseRun({
    second_pass: 'regroup',
    timing_repair: {
      result_generation_run_id: 'g1',
      result_plan_revision_id: 'plan-g1',
      result_sequence_number: 2,
      applied_count: 1,
      attempt_count: 2,
      status: 'failed',
      kind: 'regroup',
      versions: [
        version('g1', 'applied', null, { reason: 'passage_regroup' }),
        version('g2', 'failed', 'generation_failed', {
          reason: 'passage_regroup'
        })
      ]
    }
  });
  expect(secondPassKind(run)).toBe('regroup');
  expect(historyVersionOutcome(run, 'failed')).toBe('Merge failed');
  expect(historyHelper(run)).toContain('last accepted audio remains available');
  expect(historyCounts(run.timing_repair!)).toEqual({
    attempted: 2,
    applied: 1,
    rejected: 1,
    pending: 0
  });
});

test('failed legacy repair keeps repair wording', () => {
  const run = baseRun({
    second_pass: 'repair',
    timing_repair: {
      result_generation_run_id: 'r1',
      result_plan_revision_id: 'plan-r1',
      result_sequence_number: 2,
      applied_count: 1,
      attempt_count: 2,
      status: 'failed',
      kind: 'repair',
      versions: [
        version('r1', 'applied', null, { reason: 'early_timing_repair' }),
        version('r2', 'failed', 'generation_failed', {
          reason: 'early_timing_repair'
        })
      ]
    }
  });
  expect(secondPassKind(run)).toBe('repair');
  expect(historyVersionOutcome(run, 'failed')).toBe('Repair failed');
  expect(historyHeading(run)).toBe('1 block split');
});

test('unknown repair_status never counts as rejected', () => {
  const run = baseRun({
    timing_repair: {
      result_generation_run_id: 'root',
      result_plan_revision_id: 'plan-1',
      result_sequence_number: 1,
      applied_count: 0,
      attempt_count: 2,
      status: 'completed',
      versions: [version('a', 'unknown', null), version('b', 'pending', null)]
    }
  });
  expect(historyCounts(run.timing_repair!)).toEqual({
    attempted: 2,
    applied: 0,
    rejected: 0,
    pending: 1
  });
});
test('regroup stopped keeps last accepted audio wording', () => {
  const run = run8();
  run.timing_repair!.status = 'stopped';
  expect(historyHelper(run)).toBe(
    'Regroup stopped. The last accepted audio remains available.'
  );
  const failed = run8();
  failed.timing_repair!.status = 'failed';
  expect(historyHelper(failed)).toContain(
    'last accepted audio remains available'
  );
});

test('pending attempts are not counted as rejected', () => {
  const run = baseRun({
    second_pass: 'regroup',
    timing_repair: {
      result_generation_run_id: 'root',
      result_plan_revision_id: 'plan-1',
      result_sequence_number: 1,
      applied_count: 1,
      attempt_count: 3,
      status: 'running',
      versions: [
        version('a', 'applied', null),
        version('b', 'pending', null),
        version('c', 'not_applied', 'duration_misfit')
      ]
    }
  });
  expect(historyCounts(run.timing_repair!)).toEqual({
    attempted: 3,
    applied: 1,
    rejected: 1,
    pending: 1
  });
  expect(historyHelper(run)).toContain(
    'Accepted groups become part of this run'
  );
});

test('legacy early repair retains split wording', () => {
  const run = baseRun({
    second_pass: 'repair',
    settings_snapshot: {
      tts: {
        speech_block_generation_mode: 'legacy',
        speech_block_early_repair_enabled: true
      }
    },
    timing_repair: {
      result_generation_run_id: 'repair-2',
      result_plan_revision_id: 'plan-repair-2',
      result_sequence_number: 3,
      applied_count: 2,
      attempt_count: 3,
      status: 'completed',
      versions: [
        version('repair-1', 'applied', null, { source_block_ordinal: 0 }),
        version('repair-2', 'applied', null, { source_block_ordinal: 4 }),
        version('repair-3', 'not_applied', 'added_delay', {
          source_block_ordinal: 7
        })
      ]
    }
  });
  expect(secondPassKind(run)).toBe('repair');
  expect(historyHeading(run)).toBe('2 blocks split');
  expect(generationHistoryStatus(run)).toBe('completed · 2 blocks split');
  expect(historyAriaLabel(run)).toBe('Timing repair history');
  expect(historyDetailsSummary(run)).toBe('Repair details · 3 attempts');
  expect(historyVersionTitle(run, 0)).toBe('Repair 1');
  expect(historyInspectLabel(run, 0)).toBe('Inspect repair 1 audio');
  expect(historyVersionOutcome(run, 'applied')).toBe('Split applied');
});

test('outcome strings alone never decide kind (old API without kind)', () => {
  const run = baseRun({
    timing_repair: {
      result_generation_run_id: 'repair-1',
      result_plan_revision_id: 'plan-repair-1',
      result_sequence_number: 2,
      applied_count: 1,
      attempt_count: 2,
      status: 'completed',
      versions: [
        version('repair-1', 'applied', null),
        version('repair-2', 'not_applied', 'added_delay')
      ]
    }
  });
  expect(secondPassKind(run)).toBe('unknown');
  expect(historyAriaLabel(run)).toBe('Second-pass history');
  expect(historyHeading(run)).toBe('1 adjustment accepted');
  expect(historyHeading(run)).not.toContain('split');
  expect(historyHeading(run)).not.toContain('merged');
});

test('top-level kind mixed stays neutral and never claims all merged', () => {
  const run = baseRun({
    timing_repair: {
      result_generation_run_id: 'root',
      result_plan_revision_id: 'plan-1',
      result_sequence_number: 1,
      applied_count: 1,
      attempt_count: 3,
      status: 'completed',
      kind: 'mixed',
      versions: [
        version('a', 'applied', null, { reason: 'passage_regroup' }),
        version('b', 'not_applied', 'duration_misfit', {
          reason: 'passage_regroup'
        }),
        version('c', 'not_applied', 'added_delay', {
          reason: 'early_timing_repair'
        })
      ]
    }
  });
  expect(secondPassKind(run)).toBe('mixed');
  expect(historyHeading(run)).toBe('1 adjustment accepted');
  expect(historyAriaLabel(run)).toBe('Second-pass history');
  expect(historyDetailsSummary(run)).toBe('Second-pass details · 3 attempts');
  expect(historyHeading(run)).not.toContain('merged');
  expect(historyVersionTitle(run, 0)).toBe('Attempt 1');
});

test('unknown kind stays neutral', () => {
  const run = baseRun({
    timing_repair: {
      result_generation_run_id: 'root',
      result_plan_revision_id: 'plan-1',
      result_sequence_number: 1,
      applied_count: 0,
      attempt_count: 2,
      status: 'completed',
      versions: [
        version('a', 'not_applied', 'selection_changed'),
        version('b', 'not_applied', 'selection_changed')
      ]
    }
  });
  expect(secondPassKind(run)).toBe('unknown');
  expect(historyHeading(run)).toBe('No second-pass changes');
  expect(historyHelper(run)).toContain('accepted, 2 rejected');
  expect(generationHistoryStatus(run)).toBe('completed');
});

test('zero attempts: eligible-groups wording only with evidence', () => {
  const withEvidence = baseRun({
    second_pass: 'regroup',
    timing_repair: {
      result_generation_run_id: 'root',
      result_plan_revision_id: 'plan-1',
      result_sequence_number: 1,
      applied_count: 0,
      attempt_count: 0,
      status: 'completed',
      no_eligible_groups: true,
      versions: []
    }
  });
  expect(historyHeading(withEvidence)).toBe('No eligible groups');
  const withoutEvidence = baseRun({
    second_pass: 'regroup',
    timing_repair: {
      result_generation_run_id: 'root',
      result_plan_revision_id: 'plan-1',
      result_sequence_number: 1,
      applied_count: 0,
      attempt_count: 0,
      status: 'completed',
      versions: []
    }
  });
  expect(historyHeading(withoutEvidence)).toBe('No groups regenerated');
});

test('explicit per-version operation reasons decide kind (serializer contract)', () => {
  const regroup = baseRun({
    timing_repair: {
      result_generation_run_id: 'g2',
      result_plan_revision_id: 'p2',
      result_sequence_number: 2,
      applied_count: 2,
      attempt_count: 2,
      status: 'completed',
      versions: [
        version('g1', 'applied', null, { reason: 'passage_regroup' }),
        version('g2', 'applied', null, { reason: 'passage_regroup' })
      ]
    }
  });
  expect(secondPassKind(regroup)).toBe('regroup');
  const mixed = baseRun({
    timing_repair: {
      result_generation_run_id: 'g1',
      result_plan_revision_id: 'p1',
      result_sequence_number: 2,
      applied_count: 1,
      attempt_count: 2,
      status: 'completed',
      versions: [
        version('g1', 'applied', null, { reason: 'passage_regroup' }),
        version('r1', 'applied', null, { reason: 'early_timing_repair' })
      ]
    }
  });
  expect(secondPassKind(mixed)).toBe('mixed');
});

test('two- and three-passage groups keep member counts on versions', () => {
  const run = baseRun({
    second_pass: 'regroup',
    timing_repair: {
      result_generation_run_id: 'g2',
      result_plan_revision_id: 'p2',
      result_sequence_number: 3,
      applied_count: 2,
      attempt_count: 2,
      status: 'completed',
      versions: [
        version('g1', 'applied', null, {
          reason: 'passage_regroup',
          member_count: 2
        }),
        version('g2', 'applied', null, {
          reason: 'passage_regroup',
          member_count: 3
        })
      ]
    }
  });
  expect(secondPassKind(run)).toBe('regroup');
  expect(run.timing_repair!.versions[0].member_count).toBe(2);
  expect(run.timing_repair!.versions[1].member_count).toBe(3);
  expect(historyHeading(run)).toBe('2 groups merged');
});
