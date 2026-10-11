# Choosing a sound or music model

Sound and music generators create clips that you can save in the sound library
and place in a project. They are separate from the voices used for narration.
Choose a model for the kind of clip you need, then make a short example before
committing to a longer piece.

## Duration and input requirements

The following ranges are the request limits used by Pandrator's audio.cpp
integration. A model can finish before the requested duration. The catalogue
shows the limits and available controls for the installed runtime.

| Model | Best suited to | Request duration | Input and practical limits |
| --- | --- | --- | --- |
| Stable Audio 3 Small SFX | Short effects | 1–30 seconds | Describe the sound and its surroundings. |
| Stable Audio 3 Small Music | Instrumental clips | 1–120 seconds | Describe instruments, mood, and arrangement. There is no separate lyrics field. |
| Stable Audio 3 Medium | Effects and music | 1–380 seconds | Describe the desired clip and choose Sound effect or Music. |
| ControlFoley Large | Effects and ambience | 1–60 seconds | Text descriptions are accepted. Start with a short effect. |
| MiDashengLM Gen | Effects, ambience, and music | 1–163.84 seconds | Use a plain description. Long clips can stop early and their quality may vary. |
| ACE-Step 1.5, including XL | Songs and instrumental music | 10–600 seconds | Supply a style description and optional lyrics. Ask explicitly for an instrumental when you want no vocals. |
| HeartMuLa | Songs | 1–240 seconds | Supply a style description or tags and optional lyrics. Leaving lyrics blank does not reliably prevent vocals. |
| MiniMax Music 3 | Songs | 1–300 seconds | Supply both a description and lyrics. Its documented `[Instrumental]` section can describe instrumental passages. |
| YuE2 | Songs and instrumental music | 8–360 seconds | Supply a style description and optional lyrics. Empty lyrics do not guarantee an instrumental. |
| MiniMax-H3 | Experimental effects and music | 4–10 seconds | Supply a description. Audio output still uses a joint audio/video model; the INT8 variant requires CUDA. |

For songs, keep the description of the musical style separate from the words
to sing. Describe the result directly—for example, “a quiet solo piano melody
with a slow, uneven rhythm.” Model responses depend on the prompt, checkpoint,
and quantization; a duration setting is not a promise of a finished arrangement.

## Downloads and hardware

The catalogue reports the complete download size, including required supporting
files. Download size is different from memory use. Smaller quantizations can
reduce storage requirements while changing sound quality, prompt adherence, or
musical structure.

CPU is the default backend. Vulkan and CUDA are offered when the installed
runtime supports them. Start with a short clip to check speed and quality on
your hardware. H3 remains experimental, and its large audio/video components
are required even when you export only audio.

Model licences can restrict commercial use or availability in particular
regions. Check the original model's terms before choosing it for a deployment;
the licence of a converted-weight repository may not cover every component.
Model cards and upstream guides include the relevant terms:

- [Stable Audio 3](https://github.com/Stability-AI/stable-audio-3)
- [ControlFoley](https://github.com/xiaomi-research/controlfoley)
- [MiDashengLM Gen](https://huggingface.co/mispeech/midashenglm-gen)
- [ACE-Step](https://github.com/ace-step/ACE-Step-1.5)
- [HeartMuLa](https://github.com/dapi4/heartmula)
- [MiniMax Music 3](https://huggingface.co/MiniMaxAI/MiniMax-Music3)
- [YuE2](https://huggingface.co/m-a-p/YuE2-3B)
- [MiniMax-H3](https://huggingface.co/MiniMaxAI/MiniMax-H3)

See [advanced settings](audio-generation-settings.md) for individual controls
and [generation requests](audio-generation-models.md) for seeds, batches, and
saved settings. [Sounds and mixing](sounds-and-mixing.md) explains how to place
finished clips in a project.
