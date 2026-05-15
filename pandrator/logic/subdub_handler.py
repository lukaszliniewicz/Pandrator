import subprocess
import logging
import os
import ffmpeg

# Assuming Subdub script is located relative to the project root
SUBDUB_SCRIPT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'Subdub', 'subdub.py'))

def _run_subdub_command(command: list[str], task_name: str) -> bool:
    """Helper to run a Subdub command and handle logging."""
    logging.info(f"Executing {task_name} command: {' '.join(command)}")
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding='utf-8',
            errors='replace'
        )
        for line in process.stdout:
            logging.info(f"Subdub ({task_name}): {line.strip()}")
        process.wait()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command)
        logging.info(f"{task_name} process completed successfully.")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logging.error(f"{task_name} failed: {str(e)}")
        return False
    except Exception as e:
        logging.error(f"An unexpected error occurred during {task_name}: {str(e)}")
        return False

def transcribe_video(session_dir: str, video_file: str, settings: dict, correction_prompt: str = "") -> bool:
    """Transcribes a video file using Subdub."""
    command = [
        "python", SUBDUB_SCRIPT_PATH,
        "-i", video_file,
        "-session", session_dir,
        "-sl", settings.get("whisper_language", "English"),
        "-whisper_model", settings.get("whisper_model", "large-v3"),
        "-task", "transcribe"
    ]
    if settings.get("correction_enabled"):
        command.append("-correct")
        model = settings.get("translation_model", "sonnet")
        if model == "deepl": command.extend(["-llmapi", "deepl"])
        elif model == "local": command.extend(["-llmapi", "local"])
        elif model == "sonnet thinking": command.extend(["-llm-model", "sonnet", "-thinking"])
        else: command.extend(["-llm-model", model])
        
        if correction_prompt:
            command.extend(["-correct_prompt", correction_prompt])
    
    return _run_subdub_command(command, "Transcription")

def correct_subtitles(session_dir: str, srt_file: str, settings: dict, correction_prompt: str = "") -> bool:
    """Corrects an SRT file using Subdub."""
    command = [
        "python", SUBDUB_SCRIPT_PATH,
        "-i", srt_file,
        "-session", session_dir,
        "-task", "correct",
        "-context"
    ]
    model = settings.get("translation_model", "sonnet")
    if model == "deepl": command.extend(["-llmapi", "deepl"])
    elif model == "local": command.extend(["-llmapi", "local"])
    elif model == "sonnet thinking": command.extend(["-llm-model", "sonnet", "-thinking"])
    else: command.extend(["-llm-model", model])
    
    if correction_prompt:
        command.extend(["-correct_prompt", correction_prompt])
    
    return _run_subdub_command(command, "Correction")

def translate_subtitles(session_dir: str, srt_file: str, settings: dict, correction_prompt: str = "") -> bool:
    """Translates an SRT file using Subdub."""
    command = [
        "python", SUBDUB_SCRIPT_PATH,
        "-i", srt_file,
        "-session", session_dir,
        "-sl", settings.get("original_language", "English"),
        "-tl", settings.get("target_language", "en"),
        "-task", "translate",
        "-context"
    ]
    model = settings.get("translation_model", "sonnet")
    if model == "deepl": command.extend(["-llmapi", "deepl"])
    elif model == "local": command.extend(["-llmapi", "local"])
    elif model == "sonnet thinking": command.extend(["-llm-model", "sonnet", "-thinking"])
    else: command.extend(["-llm-model", model])

    if settings.get("chain_of_thought_enabled"):
        command.append("-cot")
    if settings.get("glossary_enabled") and model != "deepl":
        command.append("-glossary")
    if settings.get("correction_enabled"):
        command.append("-correct")
        if correction_prompt:
            command.extend(["-correct_prompt", correction_prompt])
            
    return _run_subdub_command(command, "Translation")

def generate_speech_blocks(session_dir: str, srt_file: str) -> bool:
    """Generates speech blocks from an SRT file using Subdub."""
    command = [
        "python", SUBDUB_SCRIPT_PATH,
        "-i", srt_file,
        "-session", session_dir,
        "-task", "speech_blocks"
    ]
    return _run_subdub_command(command, "Speech Block Generation")

def synchronize_audio(session_dir: str) -> bool:
    """Synchronizes generated audio with the original video using Subdub."""
    command = [
        "python", SUBDUB_SCRIPT_PATH,
        "-session", session_dir,
        "-task", "sync"
    ]
    return _run_subdub_command(command, "Synchronization")

def equalize_subtitles(srt_file: str) -> bool:
    """Equalizes subtitles timings using Subdub."""
    command = [
        "python", SUBDUB_SCRIPT_PATH,
        "-i", srt_file,
        "-task", "equalize"
    ]
    return _run_subdub_command(command, "Equalization")

def add_subtitles_to_video(synced_video_path: str, equalized_srt_path: str, output_video_path: str) -> bool:
    """Adds subtitles to a video file using FFmpeg."""
    ffmpeg_command = [
        "ffmpeg", "-y",
        "-i", synced_video_path,
        "-i", equalized_srt_path,
        "-c", "copy",
        "-c:s", "mov_text",
        "-metadata:s:s:0", "language=eng",
        output_video_path
    ]
    logging.info(f"Executing FFmpeg command to add subtitles: {' '.join(ffmpeg_command)}")
    try:
        process = subprocess.Popen(
            ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, encoding='utf-8', errors='replace'
        )
        for line in process.stdout:
            logging.info(f"FFmpeg: {line.strip()}")
        process.wait()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, ffmpeg_command)
        logging.info("Subtitles have been successfully embedded into the final video.")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logging.error(f"Failed to add subtitles: {e}")
        return False
