# TTS speech-plan scratch experiment

This directory tests the proposed speech/display separation without changing
Pandrator's production workflow.

It compares two authoring modes:

- `guarded`: the model returns decisions against host-supplied span IDs.
- `contextual`: the model also returns a complete speech template, while
  retaining host-supplied placeholders.

Both modes are validated and compiled into the same conceptual speech plan.
ASCII hyphens are removed deterministically only from `pronounce` values.
The compiler works from stable placeholders and token spans; it does not use
unbounded global string replacement.

The experiment records:

- display text;
- actual NeMo deterministic output when NeMo is available;
- automatic Hunspell and residual-risk candidates;
- manually seeded oracle candidates, kept distinguishable from automatic hits;
- full prompts and raw model responses;
- schema, placeholder, and pronunciation-format checks;
- compiled speech previews.

Residual checks currently add candidates for problems that NeMo or the model's
own audit can otherwise miss, including adjacent repeated words, number-word
ranges with a surviving dash, and single-letter abbreviations. These are
candidate detectors, not pronunciation authorities.

The book cases are short passages from the public-domain
[Project Gutenberg edition of *Twenty Thousand Leagues under the Sea*,
eBook #164](https://www.gutenberg.org/ebooks/164). Synthetic cases cover
names, currencies, measurements, abbreviations, URLs, and semantic-preservation
traps.

## Fedora example

Start KoboldCpp with the available Unsloth Gemma 4 26B-A4B QAT checkpoint:

```bash
KOBOLD_BIN=/home/lliniewicz/Pandrator/kobold-qwen-fastapi/bin/koboldcpp
MODEL_PATH=/var/lib/libvirt/images/lmstudio-models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf

"$KOBOLD_BIN" \
  --model "$MODEL_PATH" \
  --host 127.0.0.1 \
  --port 5012 \
  --contextsize 4096 \
  --gpulayers -1 \
  --usevulkan 0 \
  --batchsize 256 \
  --jinja \
  --jinjathink false \
  --quiet
```

Run the experiment with Pandrator's environment, which already contains NeMo:

```bash
PANDRATOR_PYTHON=/home/lliniewicz/Pandrator/envs/pandrator_installer/.pixi/envs/default/bin/python

"$PANDRATOR_PYTHON" run_experiment.py \
  --endpoint http://127.0.0.1:5012/v1 \
  --model gemma-4-26B-A4B-it-qat-UD-Q4_K_XL \
  --modes guarded contextual
```

Use `--prepare-only` to inspect NeMo and candidate detection without calling a
model. Results default to `Outputs/tts_speech_plan_scratch/<timestamp>` when
run inside the Pandrator repository.

Revalidate a completed run independently:

```bash
python analyze_results.py /path/to/results
```

## Observed Gemma 4 26B-A4B behavior

The first 12-case run produced parseable JSON in all 24 calls. Guarded plans
were valid in 12/12 cases; contextual plans were valid in 9/12. Contextual mode
used more prompt and completion tokens and did not make a whole-sentence
template change in any case. Known pronunciation reuse was deterministic, but
the two modes chose the exact same respelling in only 14/32 shared
pronunciation decisions.

A focused regression added deterministic candidates for NeMo's
`section section three– five` result and for coordinate abbreviations. Guarded
mode then compiled:

```text
Read chapter four, section three to five, then email qa at example dot org before five P M
```

It also rendered `N. lat.` and `W. long.` as `north latitude` and
`west longitude` without corrupting the word `later`. The model continued to
repeat supplied candidates in `discoveries`, even when explicitly told not to,
and one contextual plan cited an incorrect token range. This makes semantic
validation, duplicate suppression, and a guarded fallback mandatory; merely
parsing JSON is not sufficient.

These experiments assess structure and protocol adherence. They do not
establish that a phonetic respelling is objectively correct for a particular
voice or TTS backend.

## Observed Qwythos 9B v2 behavior

The same two-case focused regression was also run through the local
`Qwythos-9B-v2-Q6_K` with thinking enabled and a 1,400-token completion budget.
Only 1/2 guarded plans and 0/2 contextual plans were valid. Three of four calls
hit the completion limit. Mean latency was 141 seconds in guarded mode and 155
seconds in contextual mode, versus 33 and 54 seconds respectively for Gemma on
the same cases. The sole valid response kept every supplied candidate and then
repeated one as a conflicting discovery.

This run motivated two additional host safeguards now present in the scratch
code:

- high-confidence repeated-word and number-range residuals are required
  decisions, so `keep` is invalid;
- discoveries that duplicate protected candidates, use invalid token spans, or
  fail exact source-span matching are never compiled.

For this constrained task, the experiment provides no evidence that extended
reasoning improves reliability. The checkpoint's own
[model card](https://huggingface.co/empero-ai/Qwythos-9B-v2) describes it as a
reasoning-oriented model; that is useful context, but the results here are only
for this prompt and runtime configuration.
