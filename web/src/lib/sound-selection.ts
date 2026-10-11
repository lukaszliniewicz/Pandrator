import type { SpeechSelection } from './speech-annotations';
import type { SoundAnchor } from './project-sounds-api';

/** Offsets are Unicode code points, matching the server's speech anchors. */
export function soundAnchorForSelection(
  selection: Pick<SpeechSelection, 'item' | 'start' | 'end' | 'kind'>
): SoundAnchor {
  const text = selection.item.optimized_text || selection.item.text;
  const points = Array.from(text);
  if (
    !Number.isInteger(selection.start) ||
    !Number.isInteger(selection.end) ||
    selection.start < 0 ||
    selection.end < selection.start ||
    selection.end > points.length
  )
    throw new Error('The text selection changed. Select the words again.');
  const graphemes = new Set([
    0,
    ...[
      ...new Intl.Segmenter(undefined, {
        granularity: 'grapheme'
      }).segment(text)
    ].map((part) => Array.from(text.slice(0, part.index) + part.segment).length)
  ]);
  if (!graphemes.has(selection.start) || !graphemes.has(selection.end))
    throw new Error('Select complete characters for the sound placement.');
  if (selection.kind === 'segment' || selection.end >= points.length)
    return { kind: 'after_segment', segment_id: selection.item.id };
  if (selection.end === 0)
    return { kind: 'before_segment', segment_id: selection.item.id };
  let start = selection.start,
    end = selection.end;
  if (start === end) {
    const utfCursor = points.slice(0, end).join('').length;
    const words = [
      ...new Intl.Segmenter(undefined, { granularity: 'word' }).segment(text)
    ];
    // An in-word caret anchors after the complete word, never a truncated prefix.
    const word = words
      .reverse()
      .find((part) => part.isWordLike && part.index < utfCursor);
    if (!word)
      throw new Error('Choose a word or the beginning/end of the segment.');
    start = Array.from(text.slice(0, word.index)).length;
    end = start + Array.from(word.segment).length;
  }
  const quote = points.slice(start, end).join('');
  if (!quote.trim() || Array.from(quote).length > 500)
    throw new Error('Select a word or a phrase of up to 500 characters.');
  let occurrence = 0,
    position = 0;
  const utfStart = points.slice(0, start).join('').length;
  while (position <= utfStart) {
    const found = text.indexOf(quote, position);
    if (found < 0 || found > utfStart) break;
    occurrence += 1;
    if (found === utfStart) break;
    position = found + quote.length;
  }
  return {
    kind: 'after_word',
    segment_id: selection.item.id,
    quote,
    occurrence,
    start,
    end
  };
}

export function soundReturnUrl(
  sessionId: string,
  anchor: SoundAnchor,
  itemId?: string
) {
  const query = new URLSearchParams({ sound_anchor: JSON.stringify(anchor) });
  if (itemId) query.set('sound_item', itemId);
  return `/sessions/${encodeURIComponent(sessionId)}/generation?${query}`;
}
