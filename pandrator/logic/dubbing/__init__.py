"""Pandrator-native dubbing domain services.

Import concrete submodules directly, for example:
`from pandrator.logic.dubbing import srt_utils`.
"""

from typing import TYPE_CHECKING

# Describe the lazy submodule exports without loading their dependencies here.
if TYPE_CHECKING:
    import pandrator.logic.dubbing.artifacts as artifacts
    import pandrator.logic.dubbing.audio_sync as audio_sync
    import pandrator.logic.dubbing.boundary_correction as boundary_correction
    import pandrator.logic.dubbing.credentials as credentials
    import pandrator.logic.dubbing.crispasr as crispasr
    import pandrator.logic.dubbing.equalization as equalization
    import pandrator.logic.dubbing.languages as languages
    import pandrator.logic.dubbing.llm_config as llm_config
    import pandrator.logic.dubbing.llm_correction as llm_correction
    import pandrator.logic.dubbing.llm_translation as llm_translation
    import pandrator.logic.dubbing.manual_timing as manual_timing
    import pandrator.logic.dubbing.models as models
    import pandrator.logic.dubbing.settings as settings
    import pandrator.logic.dubbing.speech_blocks as speech_blocks
    import pandrator.logic.dubbing.srt_utils as srt_utils
    import pandrator.logic.dubbing.stt_backends as stt_backends
    import pandrator.logic.dubbing.subtitle_finalization as subtitle_finalization
    import pandrator.logic.dubbing.transcript_normalization as transcript_normalization
    import pandrator.logic.dubbing.transcription as transcription
    import pandrator.logic.dubbing.video_muxing as video_muxing
    import pandrator.logic.dubbing.zoom as zoom

__all__ = [
    "audio_sync",
    "artifacts",
    "boundary_correction",
    "credentials",
    "equalization",
    "languages",
    "llm_config",
    "llm_correction",
    "llm_translation",
    "manual_timing",
    "models",
    "crispasr",
    "speech_blocks",
    "subtitle_finalization",
    "settings",
    "srt_utils",
    "stt_backends",
    "transcription",
    "transcript_normalization",
    "video_muxing",
    "zoom",
]
