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

export function markupControls(xml: string): Partial<
  Record<ControlField, string>
> & {
  emotion_intensity?: number | null;
} {
  if (!xml) return {};
  try {
    const root = readMarkup(xml).documentElement;
    const result = Object.fromEntries(
      controlFields.map((field, index) => [
        field,
        Array.from(root.children).find(
          (node) => node.tagName === directionTags[index]
        )?.textContent ?? ''
      ])
    );
    const emotion = Array.from(root.children).find(
      (node) => node.tagName === 'em'
    );
    const scalar = emotion?.getAttribute('intensity');
    return {
      ...result,
      ...(scalar !== null &&
      scalar !== undefined &&
      Number.isFinite(Number(scalar))
        ? { emotion_intensity: Number(scalar) }
        : {})
    };
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
        'dialogue,speaker,narrator,span,event,sound,ins,em,pace,cadence,emphasis'
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
        if (node.tagName === 'sound')
          return `Sound effect: ${node.getAttribute('placement')} · variant ${node.getAttribute('variant')}`;
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

/** DOM offsets use UTF-16, matching the selection API of the source textarea. */
function spokenNodes(root: Node) {
  const nodes: { node: Text; start: number; end: number }[] = [];
  let offset = 0;
  function walk(node: Node) {
    if (
      node.nodeType === 1 &&
      [...directionTags, 'event', 'sound'].includes((node as Element).tagName)
    )
      return;
    if (node.nodeType === 3) {
      const size = node.textContent?.length ?? 0;
      nodes.push({ node: node as Text, start: offset, end: offset + size });
      offset += size;
    } else for (const child of Array.from(node.childNodes)) walk(child);
  }
  walk(root);
  return nodes;
}

function spokenRange(doc: Document, start: number, end: number) {
  if (start >= end) throw new Error('Select some spoken words first.');
  const nodes = spokenNodes(doc.documentElement);
  const first = nodes.find((item) => start >= item.start && start < item.end);
  const last = nodes.find((item) => end > item.start && end <= item.end);
  if (!first || !last)
    throw new Error(
      'The selected words no longer match this XML. Review the markup first.'
    );
  const range = doc.createRange();
  range.setStart(first.node, start - first.start);
  range.setEnd(last.node, end - last.start);
  return { range, first, last, nodes };
}

/** Wrap unassigned words, or change the assignment of a complete existing turn. */
export function markSpokenRange(
  xml: string,
  start: number,
  end: number,
  speaker: string,
  category: string
) {
  const doc = readMarkup(xml);
  const { range, first, last, nodes } = spokenRange(doc, start, end);
  const existing = first.node.parentElement?.closest(
    'dialogue,narrator,speaker'
  );
  const lastExisting = last.node.parentElement?.closest(
    'dialogue,narrator,speaker'
  );
  const wrapper = doc.createElement(
    speaker === '__narrator' ? 'narrator' : 'dialogue'
  );
  if (existing || lastExisting) {
    const region = existing?.closest('dialogue,narrator') ?? existing;
    const regionNodes = nodes.filter((item) => region?.contains(item.node));
    if (
      !region ||
      !region.contains(last.node) ||
      start !== regionNodes[0]?.start ||
      end !== regionNodes.at(-1)?.end
    )
      throw new Error(
        'Select the complete marked turn to change its speaker, or select unassigned words.'
      );
    // An explicit whole-turn reassignment keeps wording and nested directions.
    while (region.firstChild) wrapper.appendChild(region.firstChild);
    for (const old of Array.from(wrapper.querySelectorAll('speaker')))
      old.replaceWith(...Array.from(old.childNodes));
    region.replaceWith(wrapper);
  } else {
    try {
      range.surroundContents(wrapper);
    } catch {
      throw new Error(
        'Select words within one existing region; this selection crosses a markup boundary.'
      );
    }
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

/** Phrase directions may sit inside an existing speaker or narrator region. */
export function directSpokenRange(
  xml: string,
  start: number,
  end: number,
  instruction: string
) {
  if (!instruction.trim())
    throw new Error('Add a direction for the selected words first.');
  const doc = readMarkup(xml);
  const { range } = spokenRange(doc, start, end);
  const span = doc.createElement('span');
  try {
    range.surroundContents(span);
  } catch {
    throw new Error(
      'Select words within one speaker or phrase; this selection crosses a markup boundary.'
    );
  }
  const direction = doc.createElement('ins');
  direction.textContent = instruction.trim();
  span.insertBefore(direction, span.firstChild);
  return new XMLSerializer().serializeToString(doc.documentElement);
}

/** Safe, renderable text runs; annotation metadata is never spoken text. */
export function annotatedTextRuns(xml: string) {
  if (!xml) return [];
  try {
    const root = readMarkup(xml).documentElement;
    return spokenNodes(root).map(({ node, start, end }) => {
      const parent = node.parentElement;
      const owner = parent?.closest('speaker,narrator');
      const direction =
        parent?.closest('span')?.querySelector(':scope > ins')?.textContent ??
        '';
      return {
        text: node.textContent ?? '',
        start,
        end,
        direction,
        speaker:
          owner?.tagName === 'narrator'
            ? '__narrator'
            : (owner?.getAttribute('ref') ?? owner?.getAttribute('n') ?? ''),
        category: owner?.getAttribute('g') ?? 'unspecified',
        dialogue: Boolean(parent?.closest('dialogue'))
      };
    });
  } catch {
    return [];
  }
}
