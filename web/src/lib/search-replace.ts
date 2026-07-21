export type TextSearchOptions = {
  matchCase?: boolean;
  wholeWord?: boolean;
};

export type TextSearchMatch = {
  itemIndex: number;
  start: number;
  end: number;
};

export type TextReplacement = {
  index: number;
  text: string;
  matchCount: number;
};

function isWordCharacter(value: string | undefined) {
  return Boolean(value && /[\p{L}\p{N}_]/u.test(value));
}

export function findTextMatches(texts: string[], query: string, options: TextSearchOptions = {}): TextSearchMatch[] {
  if (!query) return [];
  const needle = options.matchCase ? query : query.toLocaleLowerCase();
  const matches: TextSearchMatch[] = [];

  texts.forEach((text, itemIndex) => {
    const haystack = options.matchCase ? text : text.toLocaleLowerCase();
    let offset = 0;
    while (offset <= haystack.length - needle.length) {
      const start = haystack.indexOf(needle, offset);
      if (start < 0) break;
      const end = start + needle.length;
      const wholeWordMatch = !options.wholeWord
        || (!isWordCharacter(text[start - 1]) && !isWordCharacter(text[end]));
      if (wholeWordMatch) matches.push({ itemIndex, start, end });
      offset = Math.max(end, start + 1);
    }
  });

  return matches;
}

export function replacementsForMatches(
  texts: string[],
  matches: TextSearchMatch[],
  replacement: string
): TextReplacement[] {
  const grouped = new Map<number, TextSearchMatch[]>();
  for (const match of matches) {
    const existing = grouped.get(match.itemIndex) ?? [];
    existing.push(match);
    grouped.set(match.itemIndex, existing);
  }

  return [...grouped.entries()].map(([index, itemMatches]) => {
    let text = texts[index] ?? '';
    for (const match of itemMatches.slice().sort((left, right) => right.start - left.start)) {
      text = `${text.slice(0, match.start)}${replacement}${text.slice(match.end)}`;
    }
    return { index, text, matchCount: itemMatches.length };
  }).sort((left, right) => left.index - right.index);
}
