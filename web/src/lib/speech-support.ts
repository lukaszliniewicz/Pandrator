import type { ControlField } from './speech-markup-editor';

/** This describes Pandrator's transport, not a promise about the resulting audio. */
export type SpeechCapabilities = {
  model?: string;
  backend?: string;
  status?: string;
  dialect?: string;
  instructions?: string;
  instruction_kind?:
    'none' | 'delivery' | 'voice_description' | 'voice_and_delivery';
  instruction_transport?: string;
  requires_instruction?: boolean;
  voice_design?: boolean;
  voice_design_workflow?: 'none' | 'resource_creation' | 'request_description';
  uses_reference?: boolean;
  instruction_scope?: string[];
  native_instruction_scope?: string[];
  phrase_instruction_transport?: string;
  phrase_instruction_reliability?: string;
  recommended_instruction_scope?: string;
  emotion?: {
    mode?: string;
    tags?: string[];
    transport?: string;
    intensity?: {
      min: number;
      max: number;
      default: number;
      transport: string;
      scope: string;
      label: string;
      semantics: string;
    };
  };
  event_tags?: Record<string, string>;
  event_format?: string;
  event_pause?: { format?: string; max_duration_ms?: number | null };
  semantic_context?: string;
  semantic_context_transport?: string;
  voice_mode?: string;
  multi_voice?: { pandrator?: string; native?: string };
  notes?: string[];
};

export type SpeechSupport = {
  status: 'sent' | 'hint' | 'unsupported' | 'unknown' | 'disabled';
  label: string;
  description: string;
};

const unknown: SpeechSupport = {
  status: 'unknown',
  label: 'Unverified',
  description:
    'No verified transport for this control on this model and API route. It remains saved.'
};
const unsupported: SpeechSupport = {
  status: 'unsupported',
  label: 'Not sent',
  description:
    'This model and API route do not receive this control. It remains saved for another model.'
};
const disabled: SpeechSupport = {
  status: 'disabled',
  label: 'Off',
  description:
    'Delivery directions are saved, but delivery is disabled for generation.'
};

export function deliverySupport(
  capabilities: SpeechCapabilities | undefined,
  field: ControlField,
  value = '',
  scope: 'segment' | 'phrase' | 'unresolved' = 'segment',
  enabled = true
): SpeechSupport {
  if (!enabled) return disabled;
  if (!capabilities || capabilities.status === 'unknown') return unknown;
  if (
    field === 'emotion' &&
    ['enum', 'text_field'].includes(capabilities.emotion?.mode ?? '')
  ) {
    if (
      capabilities.emotion?.mode === 'enum' &&
      value &&
      !capabilities.emotion?.tags?.includes(value.trim().toLowerCase())
    )
      return {
        ...unsupported,
        description: `This route accepts ${capabilities.emotion?.tags?.join(', ') || 'a fixed emotion list'}. Other emotions remain saved.`
      };
    if (scope === 'unresolved')
      return {
        status: 'unknown',
        label: 'Request only',
        description:
          'This route has a whole-request emotion setting. Preview checks whether these words form a complete voice request.'
      };
    if (scope === 'phrase')
      return {
        ...unsupported,
        description:
          'This route has a request-wide emotion setting. Phrase emotions are retained but not sent.'
      };
    return {
      status: 'sent',
      label:
        capabilities.emotion?.mode === 'text_field'
          ? 'Emotion description'
          : 'Emotion setting',
      description: 'Sent as the provider’s request-wide emotion setting.'
    };
  }
  if (!capabilities.instructions || capabilities.instructions === 'none')
    return unsupported;
  if (capabilities.instruction_kind === 'voice_description') {
    if (scope === 'unresolved')
      return {
        status: 'unknown',
        label: 'Voice request only',
        description:
          'This channel describes the whole generated voice. Preview checks whether the selection is a complete voice request.'
      };
    if (scope === 'phrase')
      return {
        ...unsupported,
        description:
          'This model accepts a description for the whole voice, not a direction for these words. The phrase direction remains saved.'
      };
    return {
      status: 'hint',
      label: 'Voice description hint',
      description:
        'Included in the whole-request voice description. It can affect voice identity and style; it is not a separate delivery control.'
    };
  }
  const phraseTransport =
    capabilities.phrase_instruction_transport ??
    (capabilities.instructions === 'field'
      ? 'quoted_hint'
      : capabilities.instruction_scope?.includes('span')
        ? 'inline_hint'
        : 'none');
  if (scope === 'unresolved' && phraseTransport === 'none')
    return {
      status: 'unknown',
      label: 'Request only',
      description:
        'This direction requires a whole voice request. Preview checks the resolved voice boundaries.'
    };
  if (scope === 'phrase' && phraseTransport === 'none')
    return {
      ...unsupported,
      description:
        'This route accepts a direction for the whole request. It cannot receive a direction for only these words.'
    };
  if (scope !== 'segment')
    return {
      status: 'hint',
      label:
        phraseTransport === 'quoted_hint'
          ? 'Quoted phrase hint'
          : 'Best effort',
      description:
        phraseTransport === 'quoted_hint'
          ? `Sent as a quoted phrase hint in the request-wide ${instructionTransportName(capabilities)}. It has no hard phrase boundary and may affect surrounding words. Internal XML is not sent to the provider.`
          : 'Inserted as an inline cue. It may be ignored or continue beyond the marked words; reset cues are not closing tags. Whole-segment directions are recommended.'
    };
  return {
    status: 'sent',
    label:
      capabilities.instructions === 'field'
        ? 'Whole-request style'
        : capabilities.instructions === 'inline'
          ? 'Whole-segment cue'
          : 'Prompt direction',
    description:
      field === 'instruction'
        ? `Sent through ${instructionTransportName(capabilities)} for each voice request in this segment. Delivery is interpreted by the model; spoken words stay separate.`
        : 'Translated into a natural-language delivery direction. This is separate from a numeric speed or audio effect setting.'
  };
}

export function instructionTransportName(
  capabilities: SpeechCapabilities | undefined
) {
  if (capabilities?.instruction_transport)
    return capabilities.instruction_transport;
  if (capabilities?.dialect === 'gemini38')
    return capabilities.backend?.includes('vertex')
      ? 'speechMetadata.style'
      : 'speech_metadata.style';
  return capabilities?.instructions === 'field'
    ? 'the instruction field'
    : 'the model prompt';
}

export function eventSupport(
  capabilities: SpeechCapabilities | undefined,
  kind: string,
  vocalizations = true,
  enabled = true
): SpeechSupport {
  if (!enabled) return disabled;
  if (kind !== 'pause' && !vocalizations)
    return {
      ...disabled,
      description:
        'This event is saved, but vocalizations are disabled for generation.'
    };
  if (!capabilities || capabilities.status === 'unknown') return unknown;
  if (!capabilities.event_tags?.[kind]) return unsupported;
  if (kind === 'pause' && capabilities.event_pause?.format === 'ssml_break')
    return {
      status: 'sent',
      label: 'SSML break',
      description: `Sent as a provider break, up to ${(capabilities.event_pause.max_duration_ms ?? 3000) / 1000} seconds. Longer requested pauses are shortened. Assembly timing is unchanged.`
    };
  return {
    status: 'hint',
    label: 'Audio cue',
    description:
      kind === 'pause'
        ? 'Sent as a model-specific pause cue. Duration is a soft hint, not inserted silence.'
        : `Sent using this model’s ${capabilities.event_tags[kind]} cue. The model interprets the result.`
  };
}
