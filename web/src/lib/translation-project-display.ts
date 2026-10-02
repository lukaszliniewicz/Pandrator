import type { TranslationProjectBranch } from './api-models';
import { languageLabel } from './language-registry';

export function translationLanguageName(code: string) {
  return languageLabel(code);
}

export function translationBranchStatus(branch: TranslationProjectBranch) {
  if (branch.trashed_at) return 'In trash';
  if (branch.translation_status === 'running') return 'Translation in progress';
  if (branch.translation_status === 'completed') return 'Subtitles translated';
  if (branch.translation_status === 'stale')
    return 'Translation needs updating';
  return 'Ready to translate';
}
