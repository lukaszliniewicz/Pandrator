export type ExportProfile = {
  output: Record<string, unknown>;
  audio: Record<string, unknown>;
};

type SubtitleTrack = {
  role: string;
  artifact_id: string;
  content_hash: string | null;
  revision_id: string | null;
};

export function selectedSubtitleTracks(
  context: Record<string, unknown>,
  selection: unknown
): SubtitleTrack[] {
  const tracks = Array.isArray(context.subtitle_tracks)
    ? context.subtitle_tracks.filter((track): track is SubtitleTrack =>
        Boolean(
          track &&
          typeof track === 'object' &&
          typeof track.artifact_id === 'string'
        )
      )
    : [];
  const attachedSource =
    typeof context.source_artifact_id === 'string' &&
    String(context.source_name ?? '')
      .toLowerCase()
      .endsWith('.srt')
      ? {
          role: 'upload',
          artifact_id: context.source_artifact_id,
          content_hash:
            typeof context.source_content_hash === 'string'
              ? context.source_content_hash
              : null,
          revision_id:
            typeof context.source_revision_id === 'string'
              ? context.source_revision_id
              : null
        }
      : undefined;
  const source =
    tracks.find((track) => track.role === 'correction') ??
    tracks.find((track) => track.role === 'media_edit_subtitles') ??
    tracks.find((track) => track.role === 'transcription') ??
    attachedSource;
  const translated = tracks.find((track) => track.role === 'translation');
  const choice = String(selection ?? context.subtitle_selection ?? 'auto');
  const selected = ['auto', 'dual', 'both'].includes(choice)
    ? [source, translated]
    : choice === 'translation'
      ? [translated]
      : [source];
  return selected.filter((track): track is SubtitleTrack => Boolean(track));
}

export function subtitleSelectionIssue(
  context: Record<string, unknown>,
  selection: unknown
): string {
  const available = selectedSubtitleTracks(context, 'auto');
  const source = available.some((track) => track.role !== 'translation');
  const translation = available.some((track) => track.role === 'translation');
  const choice = String(selection ?? context.subtitle_selection ?? 'auto');
  if (!available.length)
    return 'No subtitle document is available. Attach or prepare subtitles before exporting.';
  if (['source', 'dual', 'both'].includes(choice) && !source)
    return 'The source subtitle document is unavailable. Prepare it or choose an available document.';
  if (['translation', 'dual', 'both'].includes(choice) && !translation)
    return 'The translated subtitle document is unavailable. Prepare a translation or choose an available document.';
  return '';
}

/** Keep the visible export contents and the submitted audio/text choice aligned. */
export function exportIntent(
  workflowKind: string | undefined,
  output: Record<string, unknown>,
  hasSourceVideo: boolean,
  hasSourceAudio: boolean
) {
  const exportMode = String(
    output.export_mode ?? (workflowKind === 'subtitles' ? 'subtitles' : 'media')
  );
  const storedAudioMode = String(
    output.audio_mode ?? (hasSourceAudio ? 'mixed' : 'dubbing_only')
  );
  const audioMode =
    storedAudioMode === 'source'
      ? 'preserve'
      : storedAudioMode === 'dubbed'
        ? 'dubbing_only'
        : storedAudioMode;
  const rendersVideo = exportMode === 'media' && hasSourceVideo;
  const subtitleTextSource = String(output.subtitle_text_source ?? 'current');
  const includesSubtitles =
    ['subtitles', 'text'].includes(exportMode) ||
    (rendersVideo && String(output.subtitle_mode ?? 'none') !== 'none');
  return {
    exportMode,
    audioMode,
    rendersVideo,
    includesSubtitles,
    subtitleTextSource,
    usesHistoricalText:
      workflowKind !== 'audiobook' &&
      includesSubtitles &&
      subtitleTextSource === 'generation_run',
    usesGeneratedAudio:
      workflowKind === 'audiobook' ||
      (['media', 'audio'].includes(exportMode) && audioMode !== 'preserve')
  };
}
