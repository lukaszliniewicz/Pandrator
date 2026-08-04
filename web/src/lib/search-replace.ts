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
  return Boolean(value && /[\p{L}\p{M}\p{N}_]/u.test(value));
}

function characterBefore(text: string, index: number) {
  if (index <= 0) return undefined;
  const trailing = text.charCodeAt(index - 1);
  const start =
    trailing >= 0xdc00 && trailing <= 0xdfff ? index - 2 : index - 1;
  return text.slice(Math.max(0, start), index);
}

function characterAt(text: string, index: number) {
  const codePoint = text.codePointAt(index);
  return codePoint === undefined ? undefined : String.fromCodePoint(codePoint);
}

function escapeRegularExpression(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

export function findTextMatches(
  texts: string[],
  query: string,
  options: TextSearchOptions = {}
): TextSearchMatch[] {
  if (!query) return [];
  const pattern = new RegExp(
    escapeRegularExpression(query),
    options.matchCase ? 'gu' : 'giu'
  );
  const matches: TextSearchMatch[] = [];

  texts.forEach((text, itemIndex) => {
    for (const match of text.matchAll(pattern)) {
      const start = match.index;
      const matchedText = match[0];
      const end = start + matchedText.length;
      const matchedCharacters = Array.from(matchedText);
      const requiresLeadingBoundary = isWordCharacter(matchedCharacters[0]);
      const requiresTrailingBoundary = isWordCharacter(
        matchedCharacters[matchedCharacters.length - 1]
      );
      const wholeWordMatch =
        !options.wholeWord ||
        ((!requiresLeadingBoundary ||
          !isWordCharacter(characterBefore(text, start))) &&
          (!requiresTrailingBoundary ||
            !isWordCharacter(characterAt(text, end))));
      if (wholeWordMatch) matches.push({ itemIndex, start, end });
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

  return [...grouped.entries()]
    .map(([index, itemMatches]) => {
      let text = texts[index] ?? '';
      for (const match of itemMatches
        .slice()
        .sort((left, right) => right.start - left.start)) {
        text = `${text.slice(0, match.start)}${replacement}${text.slice(match.end)}`;
      }
      return { index, text, matchCount: itemMatches.length };
    })
    .sort((left, right) => left.index - right.index);
}
