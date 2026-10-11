export type SpeechWordEdit = {
  start: number;
  end: number;
  inputType?: string;
  data?: string;
};

/** Selection offsets use code points; textarea selections use UTF-16 units. */
export function applySpeechWordEdit(text: string, edit: SpeechWordEdit) {
  const letters = Array.from(text);
  let start = Math.max(0, Math.min(edit.start, letters.length));
  let end = Math.max(start, Math.min(edit.end, letters.length));
  let inserted = '';
  if (edit.inputType?.startsWith('insert')) inserted = edit.data ?? '';
  else if (edit.inputType?.startsWith('delete') && start === end) {
    // Backspace/Delete remove a whole grapheme, including combined letters/emoji.
    const boundaries = [0];
    let offset = 0;
    for (const part of new Intl.Segmenter(undefined, {
      granularity: 'grapheme'
    }).segment(text)) {
      offset += Array.from(part.segment).length;
      boundaries.push(offset);
    }
    if (edit.inputType === 'deleteContentBackward')
      start = boundaries.findLast((offset) => offset < start) ?? 0;
    else if (edit.inputType === 'deleteContentForward')
      end = boundaries.find((offset) => offset > end) ?? letters.length;
  }
  const changed = Boolean(edit.inputType);
  const value = changed
    ? letters.slice(0, start).join('') + inserted + letters.slice(end).join('')
    : text;
  const caret = letters.slice(0, start).join('').length + inserted.length;
  return {
    value,
    start: changed ? caret : letters.slice(0, start).join('').length,
    end: changed ? caret : letters.slice(0, end).join('').length
  };
}
