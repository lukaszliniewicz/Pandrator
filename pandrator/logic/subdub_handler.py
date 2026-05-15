import logging
import os
import subprocess
import sys
from typing import Any


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SUBDUB_REPO_PATH = os.path.join(PROJECT_ROOT, "Subdub")
SUBDUB_SRC_PATH = os.path.join(SUBDUB_REPO_PATH, "src")

DEFAULT_LOCAL_API_BASE = "http://localhost:1234/v1"
DEFAULT_LOCAL_MODEL = "openai/gpt-5.4-mini"

MODEL_ALIASES = {
    "gpt 5.4": "openai/gpt-5.4",
    "gpt 5.4-mini": "openai/gpt-5.4-mini",
    "gemini 3.1 pro": "gemini/gemini-3.1-pro",
    "gemini 3.0 flash": "gemini/gemini-3.0-flash",
    "opus 4.7": "anthropic/claude-opus-4-7",
    "sonnet 4.6": "anthropic/claude-sonnet-4-6",
}

LEGACY_MODEL_ALIASES = {
    "haiku": "anthropic/claude-3-5-haiku-20241022",
    "sonnet": "anthropic/claude-3-5-sonnet-20241022",
    "sonnet thinking": "anthropic/claude-3-5-sonnet-20241022",
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "gpt-4o": "openai/gpt-4o",
    "gemini-flash": "gemini/gemini-2.0-flash",
    "gemini-pro": "gemini/gemini-1.5-pro",
    "deepseek-r1": "openrouter/deepseek/deepseek-r1",
    "qwq-32b": "openrouter/qwen/qwq-32b",
}


def _build_subdub_command_base() -> list[str]:
    executable_name = os.path.basename(sys.executable).lower()
    python_executable = sys.executable if executable_name.startswith("python") else "python"
    return [python_executable, "-m", "subdub"]


def _is_local_api_base(api_base: str) -> bool:
    lowered = api_base.lower()
    return any(host in lowered for host in ("localhost", "127.0.0.1", "0.0.0.0"))


def _is_openai_compatible_model(model_name: str) -> bool:
    provider = model_name.split("/", 1)[0].strip().lower()
    return provider in {"openai", "azure", "text-completion-openai", "custom_openai"}


def _build_subdub_environment(model_options: dict[str, Any] | None = None) -> dict[str, str]:
    env = os.environ.copy()

    if os.path.isdir(SUBDUB_SRC_PATH):
        existing_pythonpath = env.get("PYTHONPATH", "")
        existing_entries = [entry for entry in existing_pythonpath.split(os.pathsep) if entry]
        if SUBDUB_SRC_PATH not in existing_entries:
            env["PYTHONPATH"] = (
                f"{SUBDUB_SRC_PATH}{os.pathsep}{existing_pythonpath}"
                if existing_pythonpath
                else SUBDUB_SRC_PATH
            )

    api_base = str((model_options or {}).get("api_base", "")).strip()
    if api_base and _is_local_api_base(api_base) and not env.get("OPENAI_API_KEY"):
        env["OPENAI_API_KEY"] = "lm-studio"

    return env


def _resolve_model_options(settings: dict) -> dict[str, Any]:
    selected_model_raw = str(settings.get("translation_model", "Sonnet 4.6")).strip()
    selected_model = selected_model_raw.lower()
    custom_model = str(settings.get("custom_translation_model", "")).strip()
    custom_api_base = str(settings.get("custom_api_base", "")).strip()

    model_value = (
        MODEL_ALIASES.get(selected_model)
        or LEGACY_MODEL_ALIASES.get(selected_model)
        or selected_model_raw
        or MODEL_ALIASES["sonnet 4.6"]
    )
    reasoning_effort = ""
    use_deepl = False
    api_base = ""

    if selected_model == "deepl":
        use_deepl = True
        model_value = "openrouter/auto"
    elif selected_model in {"custom (litellm)", "custom"}:
        if custom_model:
            model_value = custom_model
        else:
            model_value = DEFAULT_LOCAL_MODEL
            logging.warning(
                "Custom LiteLLM model is empty. Falling back to %s.",
                DEFAULT_LOCAL_MODEL,
            )

        if custom_api_base and _is_openai_compatible_model(model_value):
            api_base = custom_api_base
    elif selected_model == "local":
        # Backward compatibility for older sessions.
        api_base = str(
            custom_api_base
            or settings.get("local_api_base")
            or os.environ.get("PANDRATOR_SUBDUB_LOCAL_API_BASE")
            or DEFAULT_LOCAL_API_BASE
        ).strip()
        model_value = str(
            custom_model
            or settings.get("local_model")
            or os.environ.get("PANDRATOR_SUBDUB_LOCAL_MODEL")
            or DEFAULT_LOCAL_MODEL
        ).strip()
    elif selected_model == "sonnet thinking":
        model_value = LEGACY_MODEL_ALIASES["sonnet"]
        reasoning_effort = "high"

    if custom_api_base and not api_base and _is_openai_compatible_model(model_value):
        api_base = custom_api_base

    if settings.get("chain_of_thought_enabled") and not reasoning_effort and not use_deepl:
        reasoning_effort = "medium"

    return {
        "model": model_value,
        "use_deepl": use_deepl,
        "api_base": api_base,
        "reasoning_effort": reasoning_effort,
    }


def _apply_model_options(command: list[str], model_options: dict[str, Any]):
    command.extend(["-model", str(model_options["model"])])

    if model_options.get("use_deepl"):
        command.append("--use-deepl")

    api_base = str(model_options.get("api_base", "")).strip()
    if api_base:
        command.extend(["-api_base", api_base])

    reasoning_effort = str(model_options.get("reasoning_effort", "")).strip()
    if reasoning_effort:
        command.extend(["-reasoning_effort", reasoning_effort])


def _run_subdub_command(command: list[str], task_name: str, model_options: dict[str, Any] | None = None) -> bool:
    """Helper to run a Subdub command and handle logging."""
    logging.info(f"Executing {task_name} command: {' '.join(command)}")
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            cwd=SUBDUB_REPO_PATH if os.path.isdir(SUBDUB_REPO_PATH) else None,
            env=_build_subdub_environment(model_options),
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
    model_options = _resolve_model_options(settings)
    command = _build_subdub_command_base() + [
        "-i",
        video_file,
        "-session",
        session_dir,
        "-sl",
        settings.get("whisper_language", "English"),
        "-whisper_model",
        settings.get("whisper_model", "large-v3"),
        "-task",
        "transcribe",
    ]
    _apply_model_options(command, model_options)

    if settings.get("correction_enabled"):
        if model_options.get("use_deepl"):
            logging.warning("Skipping correction stage: DeepL mode does not support correction-only LLM steps.")
        else:
            command.append("-correct")
            if correction_prompt:
                command.extend(["-correct_prompt", correction_prompt])

    return _run_subdub_command(command, "Transcription", model_options)


def correct_subtitles(session_dir: str, srt_file: str, settings: dict, correction_prompt: str = "") -> bool:
    """Corrects an SRT file using Subdub."""
    model_options = _resolve_model_options(settings)
    if model_options.get("use_deepl"):
        logging.error("DeepL cannot be used for correction-only tasks. Choose an LLM model for correction.")
        return False

    command = _build_subdub_command_base() + [
        "-i",
        srt_file,
        "-session",
        session_dir,
        "-task",
        "correct",
        "-context",
    ]
    _apply_model_options(command, model_options)

    if correction_prompt:
        command.extend(["-correct_prompt", correction_prompt])

    return _run_subdub_command(command, "Correction", model_options)


def translate_subtitles(session_dir: str, srt_file: str, settings: dict, correction_prompt: str = "") -> bool:
    """Translates an SRT file using Subdub."""
    model_options = _resolve_model_options(settings)
    command = _build_subdub_command_base() + [
        "-i",
        srt_file,
        "-session",
        session_dir,
        "-sl",
        settings.get("original_language", "English"),
        "-tl",
        settings.get("target_language", "en"),
        "-task",
        "translate",
        "-context",
    ]
    _apply_model_options(command, model_options)

    if settings.get("glossary_enabled") and not model_options.get("use_deepl"):
        command.append("-translation_memory")

    if settings.get("correction_enabled"):
        if model_options.get("use_deepl"):
            logging.warning("Skipping correction stage: DeepL mode does not support correction-only LLM steps.")
        else:
            command.append("-correct")
            if correction_prompt:
                command.extend(["-correct_prompt", correction_prompt])

    return _run_subdub_command(command, "Translation", model_options)


def generate_speech_blocks(session_dir: str, srt_file: str) -> bool:
    """Generates speech blocks from an SRT file using Subdub."""
    command = _build_subdub_command_base() + [
        "-i",
        srt_file,
        "-session",
        session_dir,
        "-task",
        "speech_blocks",
    ]
    return _run_subdub_command(command, "Speech Block Generation")


def synchronize_audio(session_dir: str) -> bool:
    """Synchronizes generated audio with the original video using Subdub."""
    command = _build_subdub_command_base() + [
        "-session",
        session_dir,
        "-task",
        "sync",
    ]
    return _run_subdub_command(command, "Synchronization")


def equalize_subtitles(srt_file: str) -> bool:
    """Equalizes subtitles timings using Subdub."""
    command = _build_subdub_command_base() + [
        "-i",
        srt_file,
        "-task",
        "equalize",
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
