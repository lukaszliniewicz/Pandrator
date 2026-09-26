# Frozen-tail video exports

When an approved voiceover runs beyond the recording, export must preserve all speech and display the last source frame for the remaining video duration. `resolve_video_tail_extension_ms` remains the single owner of the approval policy, tolerance and frame-rounded duration; the same duration feeds the video and mixed soundtrack.

## Rendering routes

- **Stream-copy tail:** for eligible H.264 sources, copy the existing encoded video, extract its last displayed frame with a near-end seek, encode only the short still clip with compatible stream parameters, then concatenate the video-only clips. The ordinary export pipeline adds the chosen soundtrack and selectable subtitles and creates a fast-start MP4. The original video is not re-encoded.
- **Single final encode:** when burned subtitles, explicit video transcoding or resizing already require a full encode, apply `tpad` in that final render. Apply the freeze before the subtitle overlay, so captions in the extension follow their own timestamps rather than freezing an already-burned caption.
- **Compatibility fallback:** unsupported inputs or a failed fast-path validation use the existing full-video tail render. Cancellation propagates instead of silently starting a fallback.

The fast path is deliberately conservative: conventional H.264 profiles, 8-bit 4:2:0 SDR, square pixels, no rotation, consistent known frame rate, usable time base and near-zero start time. It requires FFmpeg with `libx264`. Extensions above 30 seconds use the fallback; this is not a user-facing limit on approved extensions. Other codecs, HDR and variable-rate sources are not assumed safe to concatenate.

Audio is excluded from the concatenated clips so a longer original audio stream cannot move the video join. Replacement audio retains its full length; the existing shared mixed-soundtrack calculation remains responsible for covering the voiceover. The fast helper must not add another safety frame to the already-rounded extension.

## Validation and lifecycle

`pandrator/web/video_tail_fast.py` probes the source and output, checks stream properties and duration, checks packet continuity around the join, compares decoded original-frame hashes and presentation times in a bounded pre-join window, and strictly decodes the join. Validation decodes only short windows rather than the whole recording. Full-file stream-copy and final muxing are still proportional to file size.

All scratch work lives in the export's temporary directory and is removed on success, failure or cancellation. The source is never edited. Publication continues through the existing export publisher only after rendering succeeds.

Export metadata records `tail_extension_method` as `none`, `stream_copy_tail`, `render_filter` or `full_transcode`. `video_transcoded` describes whether the original video was re-encoded, not whether a newly synthesized still clip was encoded. A fast-tail result therefore reports `video_transcoded=false` unless a subsequent compatibility render actually transcodes it.

## Focused checks

Run the tail and export suites with the repository's locked Python environment, with FFmpeg and libx264 available:

```sh
.pixi/envs/default/bin/python -m pytest -q \
  tests/test_video_tail_fast.py \
  tests/test_export_video_single_pass_tail.py \
  tests/test_video_tail_freeze.py \
  tests/test_video_tail_extension.py \
  tests/test_export_video_commands.py \
  tests/test_export_video_cleanup.py \
  tests/test_export_video_tail_decision.py
```

Real-media tests are important: command-shape tests alone cannot establish decoded-frame preservation or a valid B-frame seam. Browser and hardware-decoder coverage remains separate from FFmpeg validation.

## FFmpeg references

Concat demuxer requirements: https://ffmpeg.org/ffmpeg-formats.html#concat

Frozen-tail filter: https://ffmpeg.org/ffmpeg-filters.html#tpad
