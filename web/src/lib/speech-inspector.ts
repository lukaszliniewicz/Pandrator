import type { SpeechPreview, SpeechSelection } from './speech-annotations';
import { controlFields, type ControlField } from './speech-markup-editor';
import type { SpeechCapabilities } from './speech-support';

export type SpeechDelivery = Record<ControlField, string> & {
  emotion_intensity?: number | null;
};
export type SpeechDeliveryPatch = Partial<
  Record<ControlField, string | null>
> & { emotion_intensity?: number | null };
export type SpeechSelectionContext = {
  service: string;
  model: string;
  revision_id: string;
  segment_id: string;
  segment_revision: number;
  selection: { start: number; end: number; text: string };
  locked: boolean;
  editable: boolean;
  edit_blocked_reason: string | null;
  speech_xml: string | null;
  sound_plan_revision?: number;
  sound_cue_count?: number;
  unrepresented_sound_cue_count?: number;
  sound_cues_truncated?: boolean;
  markup_reference: {
    element_tags: string[];
    element_attributes: Record<string, string[]>;
    control_tags: Record<string, string>;
    delivery_schema: {
      properties: Record<string, { enum?: string[]; maxLength?: number }>;
    };
    vocal_event_schema: {
      properties: Record<
        string,
        { enum?: string[]; minimum?: number; maximum?: number }
      >;
    };
    limits: Record<string, number>;
  };
  current: {
    speaker: 'narrator' | 'dialogue' | 'character' | 'mixed';
    character_id: string | null;
    voice_category: string | null;
    voice: string | null;
    delivery: Record<ControlField, string | null> & {
      emotion_intensity?: number | null;
    };
    mixed_fields: string[];
  };
  inherited: {
    general_direction: string;
    characters: Array<{
      id: string;
      display_name: string;
      delivery: Partial<SpeechDelivery>;
    }>;
  };
  current_flags: {
    casting_enabled: boolean;
    performance_enabled: boolean;
    allow_vocalizations?: boolean;
  };
  capabilities: SpeechCapabilities;
  preview: SpeechPreview | null;
  preview_error?: string | null;
};
export type SpeechInspectorTarget = SpeechSelection & {
  revisionId: string;
  runId?: string;
  focus?:
    | 'speaker'
    | 'delivery'
    | 'advanced'
    | 'instruction'
    | 'emotion'
    | 'cue'
    | 'xml';
  presentation?: 'inline' | 'selection' | 'block-instruction';
};
export type SpeechInspectorController = {
  requestClose: (next: () => void) => void;
  focusSection: (section: SpeechInspectorTarget['focus']) => void;
};

export function emptyDelivery(): SpeechDelivery {
  return { instruction: '', emotion: '', pace: '', cadence: '', emphasis: '' };
}

/** Send only fields the user changed. Empty input explicitly clears that field. */
export function deliveryPatch(
  current: SpeechDeliveryPatch,
  changed: Partial<SpeechDelivery>
): SpeechDeliveryPatch {
  const result: SpeechDeliveryPatch = {};
  for (const field of controlFields.filter((field) => field in changed)) {
    const value = (changed[field] ?? '').trim();
    if (current[field] === null || value !== (current[field] ?? '').trim())
      result[field] = value || null;
  }
  if (
    'emotion_intensity' in changed &&
    (changed.emotion_intensity ?? null) !== (current.emotion_intensity ?? null)
  )
    result.emotion_intensity = changed.emotion_intensity ?? null;
  if (
    result.emotion === null &&
    ('emotion_intensity' in current || 'emotion_intensity' in changed)
  )
    result.emotion_intensity = null;
  return result;
}

/** The phrase editor shows its contribution, separately from root directions. */
export function localDelivery(
  effective: SpeechDeliveryPatch,
  root: Partial<SpeechDelivery>
) {
  const result = { ...effective };
  for (const field of controlFields.filter((field) => field in effective)) {
    const inherited = (root[field] ?? '').trim();
    const current = effective[field];
    if (current === null) continue; // Mixed selections stay mixed.
    if ((current ?? '').trim() === inherited) result[field] = '';
    else if (
      field === 'instruction' &&
      inherited &&
      current?.startsWith(`${inherited}\n`)
    )
      result[field] = current.slice(inherited.length + 1);
  }
  if (effective.emotion_intensity === root.emotion_intensity)
    delete result.emotion_intensity;
  return result;
}

/** The existing range API accepts effective values. Clearing a local value
 * restores the root direction rather than removing it from these words. */
export function inheritDeliveryPatch(
  root: Partial<SpeechDelivery>,
  patch: SpeechDeliveryPatch
) {
  return Object.fromEntries(
    Object.entries(patch).map(([name, value]) => {
      if (name === 'emotion_intensity')
        return [
          name,
          patch.emotion === null
            ? (root.emotion_intensity ?? null)
            : (value ?? root.emotion_intensity ?? null)
        ];
      const field = name as ControlField;
      const inherited = (root[field] ?? '').trim();
      return [
        field,
        field === 'instruction'
          ? [inherited, value].filter(Boolean).join('\n') || null
          : value || inherited || null
      ];
    })
  );
}

export function inspectorTargetKey(target: SpeechInspectorTarget) {
  return `${target.revisionId}:${target.item.id}:${target.start}:${target.end}:${target.runId ?? ''}:${target.presentation ?? 'selection'}`;
}

export function currentSpeakerValue(context: SpeechSelectionContext) {
  return context.current.speaker === 'mixed'
    ? '__keep__'
    : context.current.speaker === 'character'
      ? context.current.character_id || '__keep__'
      : `__${context.current.speaker}__`;
}
