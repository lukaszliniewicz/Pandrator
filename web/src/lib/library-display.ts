import type { SourceAsset, SessionRecord } from './api-models';

export type SourceCategory =
  'audio' | 'video' | 'text' | 'pdf' | 'epub' | 'subtitles' | 'other';

export const SOURCE_CATEGORIES: { value: SourceCategory; label: string }[] = [
  { value: 'audio', label: 'Audio' },
  { value: 'video', label: 'Video' },
  { value: 'text', label: 'Text' },
  { value: 'pdf', label: 'PDF' },
  { value: 'epub', label: 'EPUB' },
  { value: 'subtitles', label: 'Subtitles' },
  { value: 'other', label: 'Other' }
];

export function sourceCategory(
  source: Pick<SourceAsset, 'kind' | 'mime_type'>
): SourceCategory {
  const kind = source.kind.toLowerCase().replace(/^\./, '');
  if (kind === 'pdf' || source.mime_type === 'application/pdf') return 'pdf';
  if (kind === 'epub' || source.mime_type === 'application/epub+zip')
    return 'epub';
  if (
    ['srt', 'vtt', 'ass', 'ssa', 'sub', 'sbv'].includes(kind) ||
    source.mime_type === 'text/vtt'
  )
    return 'subtitles';
  if (
    source.mime_type?.startsWith('audio/') ||
    [
      'wav',
      'mp3',
      'flac',
      'm4a',
      'ogg',
      'opus',
      'aac',
      'aiff',
      'aif',
      'wma'
    ].includes(kind)
  )
    return 'audio';
  if (
    source.mime_type?.startsWith('video/') ||
    ['mp4', 'mkv', 'mov', 'webm', 'avi', 'm4v', 'mpeg', 'mpg'].includes(kind)
  )
    return 'video';
  if (
    source.mime_type?.startsWith('text/') ||
    ['txt', 'md', 'doc', 'docx', 'rtf', 'html', 'htm'].includes(kind)
  )
    return 'text';
  return 'other';
}

export function formatFileSize(bytes: number) {
  if (!Number.isFinite(bytes) || bytes < 0) return 'Unknown';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

export const WORKFLOW_LABELS: Record<SessionRecord['workflow_kind'], string> = {
  audiobook: 'Audiobook',
  voiceover: 'Voiceover',
  subtitles: 'Subtitles',
  media_edit: 'Recording edit'
};
