import type { GenerationSegment } from './api-models';
import {
  controlFields,
  directionTags,
  readMarkup
} from './speech-markup-editor';

export type SpeechSpan = {
  start: number;
  end: number;
  speaker_id?: string | null;
  speaker_name?: string | null;
  role: 'narrator' | 'dialogue' | 'speaker';
  delivery: Record<string, unknown>;
  voice?: string | null;
  voice_source?: string | null;
  fallback?: boolean;
};

export type SpeechSelection = {
  item: GenerationSegment;
  start: number;
  end: number;
  anchor: HTMLElement;
  rect: { left: number; top: number; bottom: number };
};

export function selectedSpeechRange(
  root: HTMLElement,
  item: GenerationSegment,
  layer: 'display' | 'speech'
): SpeechSelection | null {
  const selection = window.getSelection();
  const spoken = item.optimized_text ?? item.text;
  if (
    !selection ||
    selection.isCollapsed ||
    !selection.rangeCount ||
    (layer === 'display' && spoken !== item.text)
  )
    return null;
  const range = selection.getRangeAt(0);
  if (
    !root.contains(range.startContainer) ||
    !root.contains(range.endContainer)
  )
    return null;
  const prefix = range.cloneRange();
  prefix.selectNodeContents(root);
  prefix.setEnd(range.startContainer, range.startOffset);
  const start = Array.from(prefix.cloneContents().textContent ?? '').length;
  const selected = range.cloneContents().textContent ?? '';
  const end = start + Array.from(selected).length;
  if (
    !selected.trim() ||
    Array.from(spoken).slice(start, end).join('') !== selected
  )
    return null;
  const bounds = range.getBoundingClientRect();
  return {
    item,
    start,
    end,
    anchor: root,
    rect: { left: bounds.left, top: bounds.top, bottom: bounds.bottom }
  };
}

export function textareaSpeechRange(
  node: HTMLTextAreaElement,
  item: GenerationSegment,
  layer: 'display' | 'speech'
): SpeechSelection | null {
  const spoken = item.optimized_text ?? item.text;
  if ((layer === 'display' && spoken !== item.text) || node.value !== spoken)
    return null;
  const start = Array.from(node.value.slice(0, node.selectionStart)).length;
  const end = Array.from(node.value.slice(0, node.selectionEnd)).length;
  if (!Array.from(spoken).slice(start, end).join('').trim()) return null;
  const rect = node.getBoundingClientRect();
  return {
    item,
    start,
    end,
    anchor: node,
    rect: { left: rect.left, top: rect.top, bottom: rect.bottom }
  };
}

export type SpeechPreview = {
  revision_id: string;
  segment_id: string;
  source: 'current_plan' | 'run_snapshot';
  text: string;
  service: string;
  model: string;
  casting_enabled: boolean;
  performance_enabled: boolean;
  spans: SpeechSpan[];
  parts: Array<{
    start: number;
    end: number;
    voice?: string | null;
    voice_source?: string | null;
    fallback?: boolean;
    instructions?: string | null;
    report?: Array<{ status?: string; control?: string; message?: string }>;
  }>;
  events: Array<{ offset: number; kind: string; duration_ms?: number }>;
};

export function deliveryDescription(delivery: Record<string, unknown>) {
  return Object.entries(delivery)
    .filter(
      ([, value]) => value !== null && value !== '' && value !== undefined
    )
    .map(([key, value]) => `${key}: ${String(value)}`)
    .join(' · ');
}

/** Local markup is only a display hint. The compiler resolves actual voices. */
export function annotationSpans(item: GenerationSegment): SpeechSpan[] {
  const text = item.optimized_text ?? item.text;
  const base: SpeechSpan = {
    start: 0,
    end: Array.from(text).length,
    speaker_id: item.speaker,
    role: item.speaker ? 'speaker' : 'narrator',
    delivery: {}
  };
  const xml = item.speech_annotation_xml ?? item.speech_plan?.speech_xml;
  if (typeof xml !== 'string' || !xml || typeof DOMParser === 'undefined')
    return [base];
  try {
    const root = readMarkup(xml).documentElement;
    let transcript = '';
    const spans: SpeechSpan[] = [];
    function walk(node: Node, inherited: SpeechSpan) {
      if (node.nodeType === 3) {
        const value = node.textContent ?? '';
        if (!value) return;
        const start = Array.from(transcript).length;
        transcript += value;
        spans.push({ ...inherited, start, end: Array.from(transcript).length });
        return;
      }
      if (node.nodeType !== 1) return;
      const element = node as Element;
      if ([...directionTags, 'event'].includes(element.tagName as 'ins'))
        return;
      const next = { ...inherited, delivery: { ...inherited.delivery } };
      if (element.tagName === 'narrator') {
        next.role = 'narrator';
        next.speaker_id = null;
      } else if (element.tagName === 'speaker') {
        next.role = 'speaker';
        next.speaker_id =
          element.getAttribute('ref') || element.getAttribute('n');
      } else if (element.tagName === 'dialogue') next.role = 'dialogue';
      for (const child of Array.from(element.children)) {
        const index = directionTags.indexOf(child.tagName as 'ins');
        if (index >= 0)
          next.delivery[controlFields[index]] = child.textContent ?? '';
      }
      for (const child of Array.from(node.childNodes)) walk(child, next);
    }
    walk(root, base);
    // Do not apply offsets from stale markup or to differently optimized text.
    return transcript === text && spans.length ? spans : [base];
  } catch {
    return [base];
  }
}

export function speakerLabel(span: SpeechSpan) {
  return (
    span.speaker_name ||
    span.speaker_id ||
    (span.role === 'dialogue' ? 'Dialogue' : 'Narrator')
  );
}
