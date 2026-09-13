# Voiceover pipeline with preserved logical passages

This describes the logical-passage pipeline implemented in the development
checkout. It uses existing source timing evidence and adds no alignment pass
over generated speech.

```mermaid
flowchart TD
    A["Transcription or aligned imported subtitles"] --> B["Cue text + existing source word timings"]
    B --> C["Candidate passages at supported source boundaries"]
    C --> D["Correction with surrounding context"]
    D --> E["Corrected logical passages + source windows"]
    E --> F["Translation with surrounding context"]
    F --> G["Translated logical passages + source windows"]

    M["Model merge rule:<br/>one merged passage, one combined time window.<br/>Original IDs remain traceability only."]
    D -.-> M
    F -.-> M

    G --> H["Display formatting:<br/>wrap, split or combine for readability"]
    H --> I["Display subtitles"]

    G --> J["Speech planning:<br/>group passages by speaker, gaps and character limits"]
    J --> K["Larger TTS requests<br/>with surviving passage boundaries retained"]
    K --> L["Generate speech"]
    L --> N["Evaluate generated durations on the timeline:<br/>existing speed adjustment, optional slowdown,<br/>carry delay forward and catch up"]
    N --> O{"Meaningful early finish,<br/>no incoming delay,<br/>and a useful surviving boundary?"}
    O -->|Yes| P["Split at that passage boundary"]
    P --> Q["Regenerate the affected speech blocks"]
    Q --> N
    O -->|No| R["Keep the recordings and assemble voiceover"]

    S["A model-merged passage has no assumed internal timing boundaries.<br/>There is no additional 12-second speech-block ceiling."]
    S -.-> J
    S -.-> P
```

Correction and translation are optional workflow stages; when skipped, the
available passages continue to the next enabled stage. A passage's window locates
the corresponding source content. It is not a target duration the generated
speech must fill. Display subdivisions never become new speech repair anchors.

The existing early-repair pass still estimates internal advance from total audio
duration and text proportions. It does not measure generated word timings, and
it does not extend pauses inside an existing recording.
