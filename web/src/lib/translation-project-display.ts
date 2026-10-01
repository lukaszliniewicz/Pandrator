import type { TranslationProjectBranch } from './api-models';
import { LANGUAGE_OPTIONS } from './settings-fields';

export function translationLanguageName(code: string) {
  return (
    LANGUAGE_OPTIONS.find(
      (option) => String(option.value).toLowerCase() === code.toLowerCase()
    )?.label ?? code
  );
}

export function translationBranchStatus(branch: TranslationProjectBranch) {
  if (branch.trashed_at) return 'In trash';
  if (branch.translation_status === 'running') return 'Translation in progress';
  if (branch.translation_status === 'completed') return 'Subtitles translated';
  if (branch.translation_status === 'stale')
    return 'Translation needs updating';
  return 'Ready to translate';
}
