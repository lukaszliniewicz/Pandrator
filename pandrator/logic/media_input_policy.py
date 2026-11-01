"""Native input options for self-contained local media and generated concat lists.

Apply these options to each input, before its ``-i`` or FFprobe path. File
ownership checks remain at the caller. These options restrict native format and
protocol selection; they are not a process sandbox.
"""

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
