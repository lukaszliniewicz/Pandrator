from __future__ import annotations

from .subtitle_comparison import comparison_rows


def _ass_time(ms: int) -> str:
    centiseconds = max(0, int(ms)) // 10
    seconds, cs = divmod(centiseconds, 100)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}.{cs:02}"


def _escape(text: str) -> str:
    return str(text or "").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def write_bilingual_ass(source_srt: str, translated_srt: str, output_path: str) -> str:
    rows = comparison_rows({"source": source_srt, "translated": translated_srt})
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,BackColour,Bold,Italic,Alignment,MarginL,MarginR,MarginV,Outline,Shadow
Style: Source,Arial,38,&H00FFFFFF,&H00000000,&H70000000,0,0,2,80,80,112,2,0
Style: Translation,Arial,46,&H0000FFFF,&H00000000,&H70000000,-1,0,2,80,80,52,2,0

[Events]
Format: Layer,Start,End,Style,Text
"""
    lines = [header]
    for row in rows:
        start = _ass_time(row["start_ms"])
        end = _ass_time(row["end_ms"])
        source = _escape(row.get("source", ""))
        translation = _escape(row.get("translated", ""))
        if source:
            lines.append(f"Dialogue: 0,{start},{end},Source,{source}\n")
        if translation:
            lines.append(f"Dialogue: 1,{start},{end},Translation,{translation}\n")
    with open(output_path, "w", encoding="utf-8-sig") as handle:
        handle.write("".join(lines))
    return output_path
