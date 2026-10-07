"""Measured reading layouts and portable, audio-led video-book rendering."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence, cast

import regex
from PIL import ImageFont, features

from .book_cues import BookCue
from .book_settings import BOOK_DEFAULTS, validate_book_settings
from .cancellable_process import ProcessCancelled, run_cancellable
from .dubbing.text_units import subtitle_units
from .dubbing.video_muxing import escape_ffmpeg_subtitles_filter_path


@dataclass(frozen=True, slots=True)
class BookLayout:
    width: int
    height: int
    font_path: Path
    font_family: str
    font_size: int
    content_width: int
    max_lines: int
    style: str
    alignment: str
    _font: ImageFont.FreeTypeFont = field(repr=False, compare=False)

    def fit_lines(self, text: str) -> tuple[str, ...] | None:
        lines: list[str] = []
        for paragraph in text.splitlines() or [text]:
            current = ""
            for unit in subtitle_units(paragraph):
                candidate = current + unit
                if self._font.getlength(candidate.strip()) > self.content_width:
                    if (
                        not current.strip()
                        or self._font.getlength(unit.strip()) > self.content_width
                    ):
                        return None
                    lines.append(current.strip())
                    current = unit.lstrip()
                else:
                    current = candidate
            if current.strip():
                lines.append(current.strip())
            if len(lines) > self.max_lines:
                return None
        return tuple(lines) or None

    def as_dict(self) -> dict:
        return {
            "version": 1,
            "width": self.width,
            "height": self.height,
            "font_path": str(self.font_path),
            "font_family": self.font_family,
            "font_size": self.font_size,
            "content_width": self.content_width,
            "max_lines": self.max_lines,
            "style": self.style,
            "alignment": self.alignment,
        }


def _font_path(settings: dict, language: str) -> Path:
    supplied = str(settings.get("book_font_path") or "").strip()
    if supplied:
        path = Path(supplied).expanduser()
        if path.is_file():
            return path
        raise ValueError("The selected font file is unavailable.")
    matcher = shutil.which("fc-match")
    if matcher:
        code = language.split("-")[0]
        pattern = f":lang={code}" if code and code != "und" else "Noto Sans"
        result = subprocess.run(
            [matcher, "-f", "%{file}", pattern],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        path = Path(result.stdout.strip())
        if path.is_file():
            return path
    candidates = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return Path(candidate)
    raise ValueError(
        "Video books need a font file. Install Noto Sans or select a font in the video settings."
    )


def prepare_book_layout(settings: dict, text: str, language: str) -> BookLayout:
    validate_book_settings(settings)
    options = {**BOOK_DEFAULTS, **settings}
    width, height = (1280, 720) if options["book_resolution"] == "720p" else (1920, 1080)
    path = _font_path(options, language)
    size = round(options["book_font_size"] * height / 1080)
    try:
        font = ImageFont.truetype(str(path), size)
    except OSError as error:
        raise ValueError("The selected font file cannot be opened.") from error
    if regex.search(
        r"[\p{Arabic}\p{Hebrew}\p{Devanagari}\p{Bengali}\p{Tamil}]", text
    ) and not features.check_feature("raqm"):
        raise ValueError(
            "This writing system requires a Pillow build with complex text shaping (RAQM) for reliable video layout."
        )
    missing = font.getmask("\U0010ffff")
    missing_key = (missing.size, bytes(cast(Iterable[int], missing)))
    for cluster in set(regex.findall(r"\X", text)):
        if cluster.isspace() or not any(character.isalnum() for character in cluster):
            continue
        mask = font.getmask(cluster)
        if (mask.size, bytes(cast(Iterable[int], mask))) == missing_key:
            raise ValueError(
                f"The selected font lacks {cluster!r}. Choose a font that supports the book's writing system."
            )
    style = options["book_style"]
    lines = min(2, options["book_max_lines"]) if style == "captions" else options["book_max_lines"]
    if lines * size * 1.5 > height * 0.66:
        raise ValueError(
            "The text size and line limit exceed the video's reading area. Reduce either setting."
        )
    family = (
        (font.getname()[0] or path.stem).replace(",", " ").replace("\n", " ").replace("\r", " ")
    )
    return BookLayout(
        width,
        height,
        path,
        family,
        size,
        round(width * (0.82 if style == "captions" else 0.76)),
        lines,
        style,
        options["book_alignment"],
        font,
    )


def _ass_text(text: str) -> str:
    # libass interprets override braces and backslash escapes even inside subtitles.
    return (
        regex.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
        .replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\r", "")
        .replace("\n", "\\N")
    )


def _ass_time(ms: int) -> str:
    seconds, centiseconds = divmod(ms // 10, 100)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}.{centiseconds:02}"


def _colour(value: str) -> str:
    return f"&H00{value[5:7]}{value[3:5]}{value[1:3]}"


def write_book_ass(
    cues: Sequence[BookCue], path: Path, settings: dict, layout: BookLayout, title: str = ""
) -> None:
    options = {**BOOK_DEFAULTS, **settings}
    margin = (layout.width - layout.content_width) // 2
    alignment = (
        (2 if layout.alignment == "center" else 1)
        if layout.style == "captions"
        else (5 if layout.alignment == "center" else 4)
    )
    primary = _colour(options["book_foreground"])
    style = f"{layout.font_family},{layout.font_size},{primary},&H00000000,&H00303030,&H00000000,0,0,0,0,100,100,0,0,1,0,0,{alignment},{margin},{margin},{round(layout.height * 0.08)},1"
    heading_size = round(options["book_heading_font_size"] * layout.height / 1080)
    heading_style = f"{layout.font_family},{heading_size},{primary},&H00000000,&H00303030,&H00000000,0,0,0,0,100,100,0,0,1,0,0,8,{margin},{margin},{round(layout.height * 0.1)},1"
    header = (
        f"[Script Info]\nScriptType: v4.00+\nPlayResX: {layout.width}\nPlayResY: {layout.height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Reading,{style}\nStyle: Heading,{heading_style}\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    heading_layout = None
    if options["book_show_heading"] and layout.style == "reading":
        heading_layout = BookLayout(
            layout.width,
            layout.height,
            layout.font_path,
            layout.font_family,
            heading_size,
            layout.content_width,
            2,
            layout.style,
            "center",
            ImageFont.truetype(str(layout.font_path), heading_size),
        )
    for cue in cues:
        start, end = _ass_time(cue.start_ms), _ass_time(cue.end_ms)
        if start == end:
            raise ValueError("A display cue is too short for the video subtitle timebase.")
        text = "\n".join(cue.lines) if cue.lines else cue.text
        events.append(f"Dialogue: 0,{start},{end},Reading,,0,0,0,,{_ass_text(text)}")
        heading = cue.heading or title
        if heading_layout is not None and heading:
            heading_lines = heading_layout.fit_lines(heading)
            if heading_lines is None:
                raise ValueError(
                    "The book or chapter heading does not fit. Disable headings or shorten the displayed title."
                )
            events.append(
                f"Dialogue: 1,{start},{end},Heading,,0,0,0,,{_ass_text(chr(10).join(heading_lines))}"
            )
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def render_book_video(
    audio_path: Path,
    cues: Sequence[BookCue],
    destination: Path,
    settings: dict,
    *,
    layout: BookLayout,
    title: str,
    start_ms: int,
    duration_ms: int,
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> None:
    from .dubbing_handler import resolve_ffmpeg_for_burned_subtitles

    validate_book_settings(settings)
    if cancel_event.is_set():
        raise InterruptedError("Video-book export canceled.")
    if start_ms < 0 or duration_ms <= 0:
        raise ValueError("Video-book audio range must have positive duration.")
    ffmpeg = resolve_ffmpeg_for_burned_subtitles()
    if not ffmpeg:
        raise RuntimeError(
            "Video books require FFmpeg with the subtitles/libass filter. Install the bundled FFmpeg, or export subtitles separately."
        )
    options = {**BOOK_DEFAULTS, **settings}
    with tempfile.TemporaryDirectory(prefix=".book-video-", dir=destination.parent) as directory:
        root = Path(directory)
        ass = root / "passages.ass"
        fonts = root / "fonts"
        fonts.mkdir()
        shutil.copyfile(layout.font_path, fonts / layout.font_path.name)
        write_book_ass(cues, ass, options, layout, title)
        filter_value = f"subtitles=filename='{escape_ffmpeg_subtitles_filter_path(str(ass))}':fontsdir='{escape_ffmpeg_subtitles_filter_path(str(fonts))}'"
        duration = f"{duration_ms / 1000:.3f}"
        command = [
            ffmpeg,
            "-nostdin",
            "-y",
            "-filter_threads",
            "1",
            "-f",
            "lavfi",
            "-i",
            f"color=c={options['book_background']}:s={layout.width}x{layout.height}:r=24:d={duration}",
            "-ss",
            f"{start_ms / 1000:.3f}",
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            filter_value,
            "-t",
            duration,
            "-c:v",
            "libx264",
            "-threads",
            "4",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(destination),
        ]
        progress(0.7, "Rendering the video book")
        try:
            run_cancellable(
                command, cancel_event=cancel_event, check=True, capture_output=True, text=True
            )
        except ProcessCancelled as error:
            raise InterruptedError("Video-book export canceled.") from error
        except subprocess.CalledProcessError as error:
            lines = str(error.stderr or "").strip().splitlines()
            raise RuntimeError(
                f"Video-book render failed: {lines[-1] if lines else 'FFmpeg returned an error.'}"
            ) from error
    if cancel_event.is_set():
        raise InterruptedError("Video-book export canceled.")
