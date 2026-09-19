export const directionTags = [
  'ins',
  'em',
  'pace',
  'cadence',
  'emphasis'
] as const;
export const controlFields = [
  'instruction',
  'emotion',
  'pace',
  'cadence',
  'emphasis'
] as const;
export type ControlField = (typeof controlFields)[number];

export function readMarkup(xml: string) {
  if (/<!DOCTYPE|<!ENTITY/i.test(xml))
    throw new Error('XML declarations and entities are not supported.');
  const doc = new DOMParser().parseFromString(xml, 'application/xml');
  if (
    doc.querySelector('parsererror') ||
    doc.documentElement.tagName !== 'segment'
  ) {
    throw new Error(
      'Use one complete <segment> with matching opening and closing tags.'
    );
  }
  return doc;
}

export function markupControls(
  xml: string
): Partial<Record<ControlField, string>> {
  if (!xml) return {};
  try {
    const root = readMarkup(xml).documentElement;
    return Object.fromEntries(
      controlFields.map((field, index) => [
        field,
        Array.from(root.children).find(
          (node) => node.tagName === directionTags[index]
        )?.textContent ?? ''
      ])
    );
  } catch {
    return {};
  }
}

export function setMarkupControl(
  xml: string,
  field: ControlField,
  value: string
) {
  const doc = readMarkup(xml);
  const root = doc.documentElement;
  const tag = directionTags[controlFields.indexOf(field)];
  for (const node of Array.from(root.children))
    if (node.tagName === tag) node.remove();
  if (value) {
    const node = doc.createElement(tag);
    node.textContent = value;
    root.insertBefore(node, root.firstChild);
  }
  return new XMLSerializer().serializeToString(root);
}

export function clearMarkupDirections(xml: string) {
  const doc = readMarkup(xml);
  for (const node of doc.querySelectorAll(
    [...directionTags, 'event'].join(',')
  ))
    node.remove();
  return new XMLSerializer().serializeToString(doc.documentElement);
}

export function markupSummary(xml: string): string[] {
  if (!xml) return [];
  try {
    const root = readMarkup(xml).documentElement;
    return Array.from(
      root.querySelectorAll(
        'dialogue,speaker,narrator,span,event,ins,em,pace,cadence,emphasis'
      )
    )
      .filter(
        (node) =>
          !directionTags.includes(
            node.tagName as (typeof directionTags)[number]
          ) || node.parentElement !== root
      )
      .map((node) => {
        if (node.tagName === 'speaker')
          return `Speaker: ${node.getAttribute('ref') || node.getAttribute('n') || node.getAttribute('g') || 'unassigned'}${node.getAttribute('voice') ? ` · voice ${node.getAttribute('voice')}` : ''}`;
        if (node.tagName === 'event')
          return `Vocal event: ${node.getAttribute('kind')}${node.getAttribute('duration_ms') ? ` · ${node.getAttribute('duration_ms')} ms hint` : ''}`;
        if (
          directionTags.includes(node.tagName as (typeof directionTags)[number])
        )
          return `${node.parentElement?.tagName} ${node.tagName}: ${node.textContent}`;
        return node.tagName === 'dialogue'
          ? 'Dialogue turn'
          : node.tagName === 'narrator'
            ? 'Narrator'
            : 'Directed phrase';
      });
  } catch {
    return [];
  }
}

/** Wrap a selected range of spoken text; metadata never contributes offsets. */
export function markSpokenRange(
  xml: string,
  start: number,
  end: number,
  speaker: string,
  category: string
) {
  if (end <= start) throw new Error('Select the spoken words to mark first.');
  const doc = readMarkup(xml);
  const nodes: { node: Text; start: number; end: number }[] = [];
  let offset = 0;
  function walk(node: Node) {
    if (
      node.nodeType === 1 &&
      [...directionTags, 'event'].includes(
        (node as Element).tagName as (typeof directionTags)[number]
      )
    )
      return;
    if (node.nodeType === 3) {
      const size = node.textContent?.length ?? 0;
      nodes.push({ node: node as Text, start: offset, end: offset + size });
      offset += size;
    } else for (const child of Array.from(node.childNodes)) walk(child);
  }
  walk(doc.documentElement);
  const first = nodes.find((item) => start >= item.start && start < item.end);
  const last = nodes.find((item) => end > item.start && end <= item.end);
  if (!first || !last)
    throw new Error(
      'The selected words no longer match this XML. Review the markup first.'
    );
  const range = doc.createRange();
  range.setStart(first.node, start - first.start);
  range.setEnd(last.node, end - last.start);
  if (
    first.node.parentElement?.closest('speaker,narrator') ||
    last.node.parentElement?.closest('speaker,narrator')
  ) {
    throw new Error(
      'This selection already has a speaker. Edit its assignment in XML or select unassigned words.'
    );
  }
  const wrapper = doc.createElement(
    speaker === '__narrator' ? 'narrator' : 'dialogue'
  );
  try {
    range.surroundContents(wrapper);
  } catch {
    throw new Error(
      'Select words within one existing region; this selection crosses a markup boundary.'
    );
  }
  if (speaker !== '__narrator' && (speaker || category !== 'unspecified')) {
    const voice = doc.createElement('speaker');
    if (speaker) voice.setAttribute('ref', speaker);
    else voice.setAttribute('g', category);
    while (wrapper.firstChild) voice.appendChild(wrapper.firstChild);
    wrapper.appendChild(voice);
  }
  return new XMLSerializer().serializeToString(doc.documentElement);
}
