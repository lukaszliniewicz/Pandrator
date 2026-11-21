"""Native input options for self-contained local media and generated concat lists.

Apply these options to each input, before its ``-i`` or FFprobe path. File
ownership checks remain at the caller. These options restrict native format and
protocol selection; they are not a process sandbox.
"""

import math

LOCAL_MEDIA_DEMUXERS = (
    "mov,matroska,webm,wav,mp3,ogg,flac,aac,aiff,asf,avi,mpeg,mpegts,m4v,"
    "subrip,srt,webvtt,ass,ffmetadata"
)

LOCAL_MEDIA_INPUT_OPTIONS = (
    "-protocol_whitelist",
    "file,pipe",
    "-format_whitelist",
    LOCAL_MEDIA_DEMUXERS,
)

# Only server-generated lists use concat. Every generated file entry also needs
# the member options below, so a selected file cannot become another playlist.
GENERATED_CONCAT_INPUT_OPTIONS = (
    "-protocol_whitelist",
    "file,pipe",
    "-format_whitelist",
    "concat",
)
CONCAT_MEMBER_OPTIONS = (
    f"option protocol_whitelist file,pipe\noption format_whitelist {LOCAL_MEDIA_DEMUXERS}\n"
)

# Deadlines for local media launches. Probes and bounded analysis passes take
# one everywhere; other work takes one only where no cancel event reaches it;
# cancellable renders take none. A probe reads container metadata only, so one
# fixed short deadline fits all.
MEDIA_PROBE_TIMEOUT_SECONDS = 60.0


def media_work_timeout_seconds(
    media_seconds: float | None, *, allowance_seconds: float = 600.0
) -> float:
    """Return the deadline for bounded audio work on ``media_seconds`` of input.

    Audio decode, resample, encode and stream copy run far faster than real
    time, so a child still running after the input's duration plus the
    allowance (ten minutes by default) is hung, not busy. An unknown or
    non-finite duration gets one hour. Use it for analysis passes anywhere, and
    for other work only where no cancel event reaches it; cancellable renders
    take no deadline.
    """

    try:
        seconds = float(media_seconds) if media_seconds is not None else math.nan
    except (TypeError, ValueError, OverflowError):
        seconds = math.nan
    if not math.isfinite(seconds):
        return 3600.0
    return allowance_seconds + max(0.0, seconds)
