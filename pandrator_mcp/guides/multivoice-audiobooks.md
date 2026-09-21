# Multiple-voice audiobooks

Use this procedure for a narrator plus character voices. Speaker annotation
preserves the spoken words; performance directions are a separate optional layer.

1. Create or reuse an audiobook session and attach its source. Read
   `pandrator_get_audiobook_setup` for the current engine, mode, narration budget,
   and `configuration_revision`. Use that token as `expected_revision` in
   `pandrator_configure_audiobook(mode="multi_voice")`. This atomically enables
   speaker annotation before generation and casting, preserving the engine,
   model, references, and delivery settings. It starts no work.
2. Clean the source and run `prepare_text`. For passive speaker annotation, pass
   the **prepared_text JSON** artifact to
   `pandrator_create_speech_optimization_dispatch_run` with
   `annotation_mode="speakers"` and `annotation_only=true`. `clean_text` is not
   a structured speech plan and is rejected for this mode. Claim and submit the
   supplied units in their original order. Preserve every spoken character,
   including reporting clauses in `<narrator>`, stable `<speaker ref="…">` IDs,
   and continuation boundaries. Finish the run and select its structured result.
3. Read `pandrator_get_generation_controls` once, then update its revisioned
   character dictionary and cast together. Use managed references that are
   published to the selected renderer. Example payload fragment:

   ```json
   {
     "characters": [{"id": "c-fred", "display_name": "Fred"}],
     "cast": {
       "narrator": {"voice": "narrator-reference", "voice_id": "managed-voice-id", "service": "audio_cpp", "model": "selected-model-id"},
       "characters": {"c-fred": {"voice": "fred-reference", "voice_id": "another-managed-voice-id", "service": "audio_cpp", "model": "selected-model-id"}}
     }
   }
   ```

   These IDs are illustrative: use returned identifiers, never invent a managed
   voice ID. Discover only what is missing. Filter `get_tts_catalog` by service
   and model; its default summary avoids the full voice inventory. Search
   `get_voice_catalog` with renderer, language, readiness, and a bounded limit.
   Design, promote, and publish a new voice only if no suitable reference exists.
4. Prepare/select the speech-plan revision. Use
   `pandrator_preview_speech_segment(session_id, revision_id, segment_id)` on
   representative mixed-speaker blocks. It compiles attribution and instructions
   without creating a performance draft or synthesizing audio. The default is
   compact; `include_request=true` adds provider request details. Supplying
   `generation_run_id` inspects the frozen settings of a historical run.
5. Review the selected plan, then generate it. Keep the returned work ID and use
   `pandrator_get_work(wait_for="terminal", wait_seconds=30)` when the transport
   timeout permits; request events only for diagnosis. Assemble the completed
   takes and export the assembly. Generation, assembly, and export are distinct.

Use `pandrator_patch_session_settings` for partial changes. PUT-style
`pandrator_update_session_settings` replaces a complete override section.
Do not repeat discovery or retrieve full settings between every step when the
previous response already supplies the needed revision and identifiers.

For delivery, first check the selected model's capabilities. Qwen Base cloning
does not support instruction-based delivery. Voices and speaker switching still
work. Automatic performance analysis adds delivery to accepted speaker structure;
manual review can edit the structure explicitly. The web generation drawer offers
select-text speaker/voice/delivery editing with a compiler preview and explicit
application of the reviewed change; existing audio remains available in history.

Narration length uses model-specific application budgets with headroom, not a
claim that the provider's largest accepted request sounds best. Short dialogue
paragraphs remain short. Changing the policy affects new preparation only; use
the immutable resegmentation draft/review flow to change an existing plan.
