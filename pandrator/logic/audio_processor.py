import os
import io
import json
import base64
import logging
import tempfile
import subprocess
from pydub import AudioSegment
from PIL import Image

# Mutagen imports for metadata
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, TIT2, TALB, TPE1, TCON, PictureType
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.flac import Picture

def apply_fade(audio_data: AudioSegment, fade_in: int, fade_out: int) -> AudioSegment:
    """Applies fade in and fade out to an AudioSegment."""
    return audio_data.fade_in(fade_in).fade_out(fade_out)

def save_output(
    session_name: str,
    output_path: str,
    output_format: str,
    bitrate: str,
    metadata: dict,
    cover_image_path: str | None
) -> bool:
    """
    Concatenates sentence WAVs, saves to the specified format,
    and applies metadata and cover art.
    """
    session_dir = os.path.join("Outputs", session_name)
    sentence_wavs_dir = os.path.join(session_dir, "Sentence_wavs")
    
    json_path = os.path.join(session_dir, f"{session_name}_sentences.json")
    if not os.path.exists(json_path):
        logging.error(f"Sentences JSON file not found at {json_path}")
        return False
        
    with open(json_path, "r", encoding="utf-8") as f:
        processed_sentences = json.load(f)

    wav_files = []
    chapters = []
    current_time_ms = 0

    for sentence_dict in processed_sentences:
        sentence_number = sentence_dict.get("sentence_number")
        wav_filename = os.path.join(sentence_wavs_dir, f"{session_name}_sentence_{sentence_number}.wav")
        if os.path.exists(wav_filename):
            wav_files.append(wav_filename)
            audio = AudioSegment.from_wav(wav_filename)
            
            if sentence_dict.get("chapter") == "yes":
                chapters.append((current_time_ms / 1000, sentence_dict.get("original_sentence", "")))
            
            current_time_ms += len(audio)

    if not wav_files:
        logging.warning("No WAV files found to concatenate.")
        return False

    with tempfile.NamedTemporaryFile(mode="w", delete=False, encoding='utf-8', suffix=".txt") as temp_file:
        for wav_file in wav_files:
            temp_file.write(f"file '{os.path.abspath(wav_file)}'\n")
        input_list_path = temp_file.name

    ffmpeg_command = ["ffmpeg", "-f", "concat", "-safe", "0", "-i", input_list_path, "-y"]

    if output_format == "wav":
        ffmpeg_command += ["-c:a", "pcm_s16le"]
    elif output_format == "mp3":
        ffmpeg_command += ["-c:a", "libmp3lame", "-b:a", bitrate]
    elif output_format == "m4b":
        ffmpeg_command += ["-c:a", "aac", "-b:a", bitrate]
    elif output_format == "opus":
        ffmpeg_command += ["-c:a", "libopus", "-b:a", bitrate]

    ffmpeg_command.append(output_path)

    try:
        subprocess.run(ffmpeg_command, check=True, capture_output=True, text=True)
        
        # Apply metadata and cover art
        _save_metadata_and_cover(output_path, output_format, metadata, cover_image_path)
        
        # Add chapters for M4B format
        if output_format == "m4b" and chapters:
            _add_chapters_to_m4b(output_path, chapters)
            # Re-apply metadata after chapter modification
            _save_metadata_and_cover(output_path, output_format, metadata, cover_image_path)
        
        logging.info(f"Output file saved successfully to {output_path}")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"FFmpeg error during output save: {e.stderr}")
        return False
    except Exception as e:
        logging.error(f"An unexpected error occurred during output save: {e}")
        return False
    finally:
        if 'input_list_path' in locals() and os.path.exists(input_list_path):
            os.remove(input_list_path)

def _optimize_image(image_path: str, target_format: str = 'JPEG', max_size: tuple = (500, 500)) -> bytes:
    """Optimizes an image for embedding."""
    with Image.open(image_path) as img:
        if img.mode == 'RGBA':
            img = img.convert('RGB')
        img.thumbnail(max_size)
        bio = io.BytesIO()
        img.save(bio, format=target_format)
        return bio.getvalue()

def _save_metadata_and_cover(output_path: str, output_format: str, metadata: dict, cover_image_path: str | None):
    """Internal function to handle metadata tagging."""
    if output_format == "wav":
        return

    try:
        if output_format == "mp3":
            audio = MP3(output_path, ID3=ID3)
            audio.tags = ID3()
            if metadata.get("title"): audio.tags.add(TIT2(encoding=3, text=metadata["title"]))
            if metadata.get("album"): audio.tags.add(TALB(encoding=3, text=metadata["album"]))
            if metadata.get("artist"): audio.tags.add(TPE1(encoding=3, text=metadata["artist"]))
            if metadata.get("genre"): audio.tags.add(TCON(encoding=3, text=metadata["genre"]))
            
            if cover_image_path and os.path.exists(cover_image_path):
                cover_data = _optimize_image(cover_image_path)
                audio.tags.add(APIC(encoding=3, mime='image/jpeg', type=PictureType.COVER_FRONT, desc='Cover', data=cover_data))
            audio.save()

        elif output_format == "m4b":
            audio = MP4(output_path)
            if metadata.get("title"): audio["\xa9nam"] = [metadata["title"]]
            if metadata.get("album"): audio["\xa9alb"] = [metadata["album"]]
            if metadata.get("artist"): audio["\xa9ART"] = [metadata["artist"]]
            if metadata.get("genre"): audio["\xa9gen"] = [metadata["genre"]]
            
            if cover_image_path and os.path.exists(cover_image_path):
                with open(cover_image_path, "rb") as f:
                    cover_data = f.read()
                fmt = MP4Cover.FORMAT_JPEG if cover_image_path.lower().endswith(('.jpg', '.jpeg')) else MP4Cover.FORMAT_PNG
                audio["covr"] = [MP4Cover(cover_data, imageformat=fmt)]
            audio.save()

        elif output_format == "opus":
            audio = OggOpus(output_path)
            for key, value in metadata.items():
                if value:
                    audio[key] = value
            
            if cover_image_path and os.path.exists(cover_image_path):
                cover_data = _optimize_image(cover_image_path)
                picture = Picture()
                picture.data = cover_data
                picture.type = PictureType.COVER_FRONT
                picture.mime = "image/jpeg"
                img = Image.open(io.BytesIO(cover_data))
                picture.width, picture.height = img.size
                picture.depth = 24
                encoded_data = base64.b64encode(picture.write()).decode("ascii")
                audio["metadata_block_picture"] = [encoded_data]
            audio.save()
            
    except Exception as e:
        logging.error(f"Failed to save metadata/cover for {output_path}: {e}")

def _add_chapters_to_m4b(file_path: str, chapters: list[tuple]):
    """Internal function to add chapter markers to an M4B file."""
    chapter_file = ""
    try:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt", encoding='utf-8') as temp_file:
            temp_file.write(";FFMETADATA1\n")
            for i, (start_time_sec, title) in enumerate(chapters):
                start_time_ms = int(start_time_sec * 1000)
                # FFmpeg chapter end time is exclusive, but many players treat it as inclusive.
                # A very large number ensures it goes to the end of the file.
                end_time_ms = int(chapters[i+1][0] * 1000) if i + 1 < len(chapters) else 9223372036854775807
                temp_file.write(f"\n[CHAPTER]\nTIMEBASE=1/1000\nSTART={start_time_ms}\nEND={end_time_ms}\ntitle={title}\n")
            chapter_file = temp_file.name

        temp_output_path = f"{file_path}.temp.m4b"
        ffmpeg_command = [
            "ffmpeg", "-i", file_path, "-i", chapter_file,
            "-map", "0", "-map_chapters", "1",
            "-c", "copy", "-y", temp_output_path
        ]
        
        subprocess.run(ffmpeg_command, check=True, capture_output=True, text=True)
        os.replace(temp_output_path, file_path)
        logging.info(f"Successfully added {len(chapters)} chapters to {file_path}")

    except subprocess.CalledProcessError as e:
        logging.error(f"FFmpeg error adding chapters: {e.stderr}")
    except Exception as e:
        logging.error(f"Error adding chapters to M4B: {e}")
    finally:
        if chapter_file and os.path.exists(chapter_file):
            os.remove(chapter_file)
