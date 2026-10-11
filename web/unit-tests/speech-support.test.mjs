import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/speech-support.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
const { deliverySupport, eventSupport } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

test('saved directions stay editable when a documented cloning route cannot send them', () => {
  const cloning = { status: 'documented', instructions: 'none' };
  assert.equal(
    deliverySupport(cloning, 'instruction', 'Quietly').status,
    'unsupported'
  );
  assert.equal(
    deliverySupport(cloning, 'emotion', 'excited').status,
    'unsupported'
  );
  assert.match(
    deliverySupport(cloning, 'pace', 'brisk').description,
    /remains saved/
  );
});

test('native emotion enums require a complete resolved request and a valid value', () => {
  const native = {
    status: 'documented',
    instructions: 'none',
    emotion: { mode: 'enum', tags: ['neutral', 'happy'] }
  };
  assert.equal(deliverySupport(native, 'emotion', 'Happy').status, 'sent');
  assert.equal(
    deliverySupport(native, 'emotion', 'happy', 'phrase').status,
    'unsupported'
  );
  assert.equal(
    deliverySupport(native, 'emotion', 'happy', 'unresolved').label,
    'Request only'
  );
  assert.equal(
    deliverySupport(native, 'emotion', 'wistful', 'unresolved').status,
    'unsupported'
  );
  assert.equal(
    deliverySupport(native, 'instruction', 'Whisper').status,
    'unsupported'
  );
});

test('field instructions honestly distinguish request delivery from phrase hints', () => {
  const field = {
    status: 'documented',
    instructions: 'field',
    instruction_scope: ['request']
  };
  assert.equal(
    deliverySupport(field, 'instruction', 'Whisper').label,
    'Whole-request style'
  );
  assert.equal(
    deliverySupport(field, 'instruction', 'Whisper', 'phrase').status,
    'hint'
  );
  assert.match(
    deliverySupport(field, 'instruction', 'Whisper', 'phrase').description,
    /quoted phrase/
  );
  const requestOnly = { ...field, instructions: 'inline' };
  assert.equal(
    deliverySupport(requestOnly, 'instruction', 'Whisper', 'phrase').status,
    'unsupported'
  );
});

test('Gemini structured style feedback distinguishes native request scope and interpreted phrase hints', () => {
  const profile = {
    status: 'documented',
    dialect: 'gemini38',
    backend: 'vertex_ai',
    instructions: 'field',
    instruction_scope: ['request', 'span'],
    native_instruction_scope: ['request'],
    phrase_instruction_transport: 'quoted_hint'
  };
  const phrase = deliverySupport(
    profile,
    'instruction',
    'Emphasize this',
    'phrase'
  );
  assert.equal(phrase.status, 'hint');
  assert.match(phrase.description, /speechMetadata.style/);
  assert.match(phrase.description, /no hard phrase boundary/);
  assert.match(phrase.description, /XML is not sent/);
  assert.equal(
    deliverySupport(profile, 'instruction', 'Read warmly').label,
    'Whole-request style'
  );
  assert.match(
    deliverySupport({ ...profile, backend: 'gemini' }, 'instruction')
      .description,
    /speech_metadata.style/
  );
  assert.equal(
    deliverySupport(profile, 'emotion', 'excited', 'phrase', false).status,
    'disabled'
  );
});

test('unknown transports and generation switches do not promise delivery', () => {
  assert.equal(deliverySupport(undefined, 'instruction').status, 'unknown');
  assert.equal(
    deliverySupport({ status: 'unknown', instructions: 'field' }, 'instruction')
      .status,
    'unknown'
  );
  assert.equal(
    deliverySupport(
      { status: 'documented', instructions: 'field' },
      'instruction',
      'Whisper',
      'segment',
      false
    ).status,
    'disabled'
  );
  const cues = {
    status: 'documented',
    event_tags: { laugh: 'laugh', pause: 'short pause' }
  };
  assert.equal(eventSupport(cues, 'cough').status, 'unsupported');
  assert.equal(eventSupport(cues, 'laugh', false).status, 'disabled');
  assert.equal(eventSupport(cues, 'pause', false).status, 'hint');
  assert.match(eventSupport(cues, 'pause').description, /not inserted silence/);
});

test('voice descriptions are request hints and omit phrase delivery', () => {
  const design = {
    status: 'documented',
    instructions: 'field',
    instruction_kind: 'voice_description',
    instruction_scope: ['request']
  };
  assert.equal(
    deliverySupport(design, 'instruction', 'Low, tired voice').status,
    'hint'
  );
  assert.equal(
    deliverySupport(design, 'emotion', 'sad').label,
    'Voice description hint'
  );
  assert.equal(
    deliverySupport(design, 'instruction', 'Whisper', 'phrase').status,
    'unsupported'
  );
  assert.equal(
    deliverySupport(design, 'instruction', 'Whisper', 'unresolved').status,
    'unknown'
  );
});

test('Fish phrase cues are best effort while whole-segment cues are recommended', () => {
  const fish = {
    status: 'documented',
    instructions: 'inline',
    phrase_instruction_transport: 'inline_hint',
    recommended_instruction_scope: 'request'
  };
  const phrase = deliverySupport(fish, 'instruction', 'Whisper', 'phrase');
  assert.equal(phrase.label, 'Best effort');
  assert.match(phrase.description, /ignored or continue beyond/);
  assert.match(phrase.description, /not closing tags/);
  assert.equal(deliverySupport(fish, 'instruction').label, 'Whole-segment cue');
});

test('capability scope beats field heuristics and native emotion text remains request-only', () => {
  const requestOnly = {
    status: 'documented',
    instructions: 'field',
    phrase_instruction_transport: 'none'
  };
  assert.equal(
    deliverySupport(requestOnly, 'instruction', 'Whisper', 'phrase').status,
    'unsupported'
  );
  const emotionText = {
    status: 'documented',
    instructions: 'none',
    emotion: { mode: 'text_field' }
  };
  assert.equal(
    deliverySupport(emotionText, 'emotion', 'quiet delight').status,
    'sent'
  );
  assert.equal(
    deliverySupport(emotionText, 'emotion', 'quiet delight', 'phrase').status,
    'unsupported'
  );
  assert.equal(
    deliverySupport(emotionText, 'instruction', 'Whisper').status,
    'unsupported'
  );
});

test('provider SSML breaks describe the duration limit without promising assembly silence', () => {
  const pause = {
    status: 'documented',
    event_tags: { pause: 'break' },
    event_pause: { format: 'ssml_break', max_duration_ms: 3000 }
  };
  const support = eventSupport(pause, 'pause');
  assert.equal(support.status, 'sent');
  assert.match(support.description, /3 seconds/);
  assert.match(support.description, /Assembly timing is unchanged/);
});
