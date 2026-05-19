import logging
import os
import re
import shutil
import subprocess
import sys
from typing import Any


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SUBDUB_REPO_PATH = os.path.join(PROJECT_ROOT, "Subdub")
SUBDUB_SRC_PATH = os.path.join(SUBDUB_REPO_PATH, "src")
WHISPERX_PIXI_EXE_ENV = "WHISPERX_PIXI_EXE"
WHISPERX_PIXI_MANIFEST_ENV = "WHISPERX_PIXI_MANIFEST"
WHISPERX_PIXI_MANIFEST_PATH = os.path.join(PROJECT_ROOT, "envs", "whisperx_installer", "pixi.toml")
WHISPERX_PIXI_EXE_CANDIDATES = (
    os.path.join(PROJECT_ROOT, "bin", "pixi.exe"),
    os.path.join(PROJECT_ROOT, "bin", "pixi"),
)
BUNDLED_FFMPEG_CANDIDATES = (
    os.path.join(PROJECT_ROOT, "bin", "ffmpeg.exe"),
    os.path.join(PROJECT_ROOT, "bin", "ffmpeg"),
)
SYNC_OVERWRITE_GUARDED_AUDIO_STEMS = (
    "original_audio",
    "mixed_audio",
    "amplified_dubbed_audio",
)
SYNC_OUTPUT_VIDEO_EXTENSIONS = (
    ".mp4",
    ".mkv",
    ".webm",
    ".avi",
    ".mov",
)

DEFAULT_LOCAL_API_BASE = "http://localhost:1234/v1"
DEFAULT_LOCAL_MODEL = "openai/gpt-5.4-mini"
DEFAULT_DUBBING_PROVIDER_ID = "anthropic"
DEFAULT_DUBBING_MODEL_ID = "claude-sonnet-4-6"
NON_LLM_TASK_MODEL = "openrouter/auto"
DEEPL_PROVIDER_ID = "deepl"
MANUAL_CORRECTION_RESULT_PREFIX = "PANDRATOR_MANUAL_CORRECTION_RESULT="
MANUAL_CORRECTION_AUDIO_EXTENSIONS = (
    ".wav",
    ".mp3",
    ".flac",
    ".m4a",
    ".ogg",
    ".aac",
    ".opus",
)

_FFMPEG_SUBTITLES_SUPPORT_CACHE: dict[str, bool] = {}

MODEL_ALIASES = {
    "gpt 5.4": "openai/gpt-5.4",
    "gpt 5.4-mini": "openai/gpt-5.4-mini",
    "gemini 3.1 pro": "gemini/gemini-3.1-pro-preview",
    "gemini 3.0 flash": "gemini/gemini-3-flash-preview",
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

KNOWN_PROVIDER_PREFIXES = {
    "openai",
    "gemini",
    "anthropic",
    "vertex_ai",
    "azure",
    "bedrock",
    "openrouter",
    "groq",
    "mistral",
    "ollama",
}

PROVIDER_API_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "azure": "AZURE_API_KEY",
    "text-completion-openai": "OPENAI_API_KEY",
    "custom_openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "bedrock": "AWS_SECRET_ACCESS_KEY",
    "vertex_ai": "GOOGLE_API_KEY",
}


def _resolve_python_executable() -> str:
    executable_name = os.path.basename(sys.executable).lower()
    return sys.executable if executable_name.startswith("python") else "python"


def _build_subdub_command_base() -> list[str]:
    return [_resolve_python_executable(), "-m", "subdub"]


def _resolve_subdub_path(path: str) -> str:
    expanded = os.path.expanduser(str(path or "").strip())
    return os.path.abspath(expanded) if expanded else expanded


def _is_local_api_base(api_base: str) -> bool:
    lowered = api_base.lower()
    return any(host in lowered for host in ("localhost", "127.0.0.1", "0.0.0.0"))


def _is_openai_compatible_model(model_name: str) -> bool:
    provider = model_name.split("/", 1)[0].strip().lower()
    return provider in {"openai", "azure", "text-completion-openai", "custom_openai"}


def _normalize_provider_id(raw_value: str | None) -> str:
    lowered = str(raw_value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")


def _normalize_provider_key(raw_value: str | None) -> str:
    provider = str(raw_value or "").strip().lower()
    aliases = {
        "google": "gemini",
        "google-ai": "gemini",
        "google_ai": "gemini",
        "google-ai-studio": "gemini",
        "openai-compatible": "openai",
        "openai_compatible": "openai",
    }
    return aliases.get(provider, provider)


def _normalize_model_for_provider(model_name: str, provider_key: str) -> str:
    normalized = str(model_name or "").strip()
    if not normalized:
        return ""

    if normalized.lower().startswith("custom:"):
        remainder = normalized[len("custom:") :].strip()
        _, separator, model_part = remainder.partition("/")
        if separator and model_part.strip():
            normalized = model_part.strip()

    if normalized.lower().startswith("models/"):
        normalized = normalized.split("/", 1)[1].strip()

    if "/" in normalized:
        prefix, remainder = normalized.split("/", 1)
        if _normalize_provider_key(prefix) == _normalize_provider_key(provider_key) and remainder.strip():
            return remainder.strip()

    return normalized


def _to_litellm_model_name(provider_key: str, model_name: str) -> str:
    normalized_model = str(model_name or "").strip()
    if not normalized_model:
        return ""

    if "/" in normalized_model:
        prefix, _ = normalized_model.split("/", 1)
        if _normalize_provider_key(prefix) in KNOWN_PROVIDER_PREFIXES:
            return normalized_model

    normalized_provider = _normalize_provider_key(provider_key) or "openai"
    return f"{normalized_provider}/{normalized_model}"


def _coerce_provider_configs(raw_configs: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_configs, list):
        return {}

    provider_configs: dict[str, dict[str, Any]] = {}
    for item in raw_configs:
        if not isinstance(item, dict):
            continue
        provider_id = _normalize_provider_id(item.get("id") or item.get("name") or "")
        if not provider_id:
            continue
        provider_configs[provider_id] = item

    return provider_configs


def _provider_models(provider_config: dict[str, Any]) -> list[str]:
    raw_models = provider_config.get("models", [])
    if not isinstance(raw_models, list):
        return []

    models: list[str] = []
    for model in raw_models:
        normalized = str(model or "").strip()
        if normalized:
            models.append(normalized)
    return models


def _provider_default_model(provider_config: dict[str, Any]) -> str:
    default_model = str(provider_config.get("default_model") or "").strip()
    if default_model:
        return default_model

    provider_models = _provider_models(provider_config)
    if provider_models:
        return provider_models[0]

    provider_key = _normalize_provider_key(provider_config.get("provider") or "")
    if provider_key == "anthropic":
        return DEFAULT_DUBBING_MODEL_ID
    if provider_key == "gemini":
        return "gemini-3-flash-preview"
    return DEFAULT_LOCAL_MODEL.split("/", 1)[-1]


def _api_key_env_for_provider(provider_key: str, model_name: str = "") -> str:
    normalized_provider = _normalize_provider_key(provider_key)
    if normalized_provider in PROVIDER_API_KEY_ENV:
        return PROVIDER_API_KEY_ENV[normalized_provider]

    model_prefix = ""
    model_text = str(model_name or "").strip()
    if "/" in model_text:
        model_prefix = _normalize_provider_key(model_text.split("/", 1)[0])

    if model_prefix in PROVIDER_API_KEY_ENV:
        return PROVIDER_API_KEY_ENV[model_prefix]

    return "OPENAI_API_KEY"


def _resolve_provider_backed_model_options(settings: dict) -> dict[str, Any] | None:
    provider_configs = _coerce_provider_configs(settings.get("llm_provider_configs"))
    selected_provider_id = _normalize_provider_id(settings.get("translation_provider") or "")
    if not selected_provider_id:
        return None

    if selected_provider_id == DEEPL_PROVIDER_ID:
        return {
            "model": "openrouter/auto",
            "provider": DEEPL_PROVIDER_ID,
            "use_deepl": True,
            "api_base": "",
            "api_key": "",
            "api_key_env": "DEEPL_API_KEY",
            "reasoning_effort": "",
        }

    provider_config = provider_configs.get(selected_provider_id)
    if provider_config is None:
        return None

    provider_key = _normalize_provider_key(provider_config.get("provider") or selected_provider_id)
    selected_model_raw = str(settings.get("translation_model") or "").strip()
    if selected_model_raw.lower() == "deepl":
        return {
            "model": "openrouter/auto",
            "provider": DEEPL_PROVIDER_ID,
            "use_deepl": True,
            "api_base": "",
            "api_key": "",
            "api_key_env": "DEEPL_API_KEY",
            "reasoning_effort": "",
        }

    selected_model = _normalize_model_for_provider(selected_model_raw, provider_key)

    if selected_model_raw.lower() in {"custom (litellm)", "custom", "local"}:
        selected_model = str(settings.get("custom_translation_model") or "").strip()

    if not selected_model:
        selected_model = _provider_default_model(provider_config)

    if selected_model.lower() in MODEL_ALIASES:
        selected_model = MODEL_ALIASES[selected_model.lower()]
    elif selected_model.lower() in LEGACY_MODEL_ALIASES:
        selected_model = LEGACY_MODEL_ALIASES[selected_model.lower()]

    model_value = _to_litellm_model_name(provider_key, selected_model)
    api_base = str(provider_config.get("api_base") or "").strip()
    custom_api_base = str(settings.get("custom_api_base") or "").strip()
    if custom_api_base and _is_openai_compatible_model(model_value):
        api_base = custom_api_base

    reasoning_effort = ""
    if selected_model_raw.lower() == "sonnet thinking":
        reasoning_effort = "high"

    api_key = str(provider_config.get("api_key") or "").strip()
    api_key_env = str(provider_config.get("api_key_env") or "").strip()
    if not api_key_env:
        api_key_env = _api_key_env_for_provider(provider_key, model_value)

    return {
        "model": model_value,
        "provider": provider_key,
        "use_deepl": False,
        "api_base": api_base,
        "api_key": api_key,
        "api_key_env": api_key_env,
        "reasoning_effort": reasoning_effort,
    }


def _resolve_legacy_model_options(settings: dict) -> dict[str, Any]:
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

    provider_prefix = model_value.split("/", 1)[0].strip().lower() if "/" in model_value else ""
    return {
        "model": model_value,
        "provider": provider_prefix,
        "use_deepl": use_deepl,
        "api_base": api_base,
        "api_key": "",
        "api_key_env": _api_key_env_for_provider(provider_prefix, model_value),
        "reasoning_effort": reasoning_effort,
    }


def _build_subdub_environment(model_options: dict[str, Any] | None = None) -> dict[str, str]:
    env = os.environ.copy()

    if os.name == "nt":
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")

    if os.path.isdir(SUBDUB_SRC_PATH):
        existing_pythonpath = env.get("PYTHONPATH", "")
        existing_entries = [entry for entry in existing_pythonpath.split(os.pathsep) if entry]
        if SUBDUB_SRC_PATH not in existing_entries:
            env["PYTHONPATH"] = (
                f"{SUBDUB_SRC_PATH}{os.pathsep}{existing_pythonpath}"
                if existing_pythonpath
                else SUBDUB_SRC_PATH
            )

    options = model_options or {}
    model_name = str(options.get("model") or "").strip()
    api_base = str(options.get("api_base") or "").strip()
    api_key = str(options.get("api_key") or "").strip()
    api_key_env = str(options.get("api_key_env") or "").strip()

    if api_key and api_key_env and not env.get(api_key_env):
        env[api_key_env] = api_key
    elif api_key and _is_openai_compatible_model(model_name) and not env.get("OPENAI_API_KEY"):
        env["OPENAI_API_KEY"] = api_key

    if api_base and _is_local_api_base(api_base) and not env.get("OPENAI_API_KEY"):
        env["OPENAI_API_KEY"] = api_key or "lm-studio"

    if not env.get(WHISPERX_PIXI_EXE_ENV):
        for candidate in WHISPERX_PIXI_EXE_CANDIDATES:
            if os.path.isfile(candidate):
                env[WHISPERX_PIXI_EXE_ENV] = candidate
                break

    if not env.get(WHISPERX_PIXI_MANIFEST_ENV) and os.path.isfile(WHISPERX_PIXI_MANIFEST_PATH):
        env[WHISPERX_PIXI_MANIFEST_ENV] = WHISPERX_PIXI_MANIFEST_PATH

    return env


def _resolve_model_options(settings: dict) -> dict[str, Any]:
    provider_backed = _resolve_provider_backed_model_options(settings)
    if provider_backed is not None:
        return provider_backed

    return _resolve_legacy_model_options(settings)


def _non_llm_task_model_options() -> dict[str, Any]:
    return {
        "model": NON_LLM_TASK_MODEL,
        "provider": "openrouter",
        "use_deepl": False,
        "api_base": "",
        "api_key": "",
        "api_key_env": "",
        "reasoning_effort": "",
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


def _run_subdub_command_with_output(
    command: list[str],
    task_name: str,
    model_options: dict[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """Runs a Subdub command and returns success flag with collected output lines."""
    logging.info(f"Executing {task_name} command: {' '.join(command)}")
    output_lines: list[str] = []
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
        if process.stdout is not None:
            for line in process.stdout:
                stripped_line = line.strip()
                output_lines.append(stripped_line)
                logging.info(f"Subdub ({task_name}): {stripped_line}")
        process.wait()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command)
        logging.info(f"{task_name} process completed successfully.")
        return True, output_lines
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logging.error(f"{task_name} failed: {str(e)}")
        return False, output_lines
    except Exception as e:
        logging.error(f"An unexpected error occurred during {task_name}: {str(e)}")
        return False, output_lines


def _run_subdub_command(command: list[str], task_name: str, model_options: dict[str, Any] | None = None) -> bool:
    """Helper to run a Subdub command and handle logging."""
    success, _ = _run_subdub_command_with_output(command, task_name, model_options)
    return success


def transcribe_video(session_dir: str, video_file: str, settings: dict, correction_prompt: str = "") -> bool:
    """Transcribes a video file using Subdub."""
    correction_enabled = bool(settings.get("correction_enabled"))
    model_options = _resolve_model_options(settings) if correction_enabled else _non_llm_task_model_options()
    resolved_session_dir = _resolve_subdub_path(session_dir)
    resolved_video_file = _resolve_subdub_path(video_file)
    command = _build_subdub_command_base() + [
        "-i",
        resolved_video_file,
        "-session",
        resolved_session_dir,
        "-sl",
        settings.get("whisper_language", "English"),
        "-whisper_model",
        settings.get("whisper_model", "large-v3"),
        "-task",
        "transcribe",
    ]
    _apply_model_options(command, model_options)

    if correction_enabled:
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

    resolved_session_dir = _resolve_subdub_path(session_dir)
    resolved_srt_file = _resolve_subdub_path(srt_file)
    command = _build_subdub_command_base() + [
        "-i",
        resolved_srt_file,
        "-session",
        resolved_session_dir,
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
    resolved_session_dir = _resolve_subdub_path(session_dir)
    resolved_srt_file = _resolve_subdub_path(srt_file)
    command = _build_subdub_command_base() + [
        "-i",
        resolved_srt_file,
        "-session",
        resolved_session_dir,
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
    model_options = _non_llm_task_model_options()
    resolved_session_dir = _resolve_subdub_path(session_dir)
    resolved_srt_file = _resolve_subdub_path(srt_file)
    command = _build_subdub_command_base() + [
        "-i",
        resolved_srt_file,
        "-session",
        resolved_session_dir,
        "-task",
        "speech_blocks",
    ]
    _apply_model_options(command, model_options)
    return _run_subdub_command(command, "Speech Block Generation", model_options)


def _clear_previous_sync_outputs(session_dir: str) -> None:
    """Removes stale sync artifacts that can block FFmpeg overwrite in Subdub."""
    if not os.path.isdir(session_dir):
        return

    removed_files: list[str] = []
    for file_name in os.listdir(session_dir):
        file_path = os.path.join(session_dir, file_name)
        if not os.path.isfile(file_path):
            continue

        lower_name = file_name.lower()
        stem, extension = os.path.splitext(lower_name)

        should_remove = False
        if extension == ".wav" and any(
            stem.startswith(audio_stem)
            for audio_stem in SYNC_OVERWRITE_GUARDED_AUDIO_STEMS
        ):
            should_remove = True
        elif extension in SYNC_OUTPUT_VIDEO_EXTENSIONS and stem.startswith("final_output"):
            should_remove = True

        if not should_remove:
            continue

        try:
            os.remove(file_path)
            removed_files.append(file_path)
        except OSError as error:
            logging.warning("Could not remove stale sync artifact '%s': %s", file_path, error)

    if removed_files:
        logging.info(
            "Removed %d stale sync artifact(s) before synchronization.",
            len(removed_files),
        )


def synchronize_audio(session_dir: str, video_file: str = "") -> bool:
    """Synchronizes generated audio with the original video using Subdub."""
    model_options = _non_llm_task_model_options()
    resolved_session_dir = _resolve_subdub_path(session_dir)
    _clear_previous_sync_outputs(resolved_session_dir)
    command = _build_subdub_command_base() + [
        "-session",
        resolved_session_dir,
        "-task",
        "sync",
    ]

    if video_file:
        command.extend(["-v", _resolve_subdub_path(video_file)])

    _apply_model_options(command, model_options)
    return _run_subdub_command(command, "Synchronization", model_options)


def equalize_subtitles(srt_file: str) -> bool:
    """Equalizes subtitles timings using Subdub."""
    model_options = _non_llm_task_model_options()
    resolved_srt_file = _resolve_subdub_path(srt_file)
    command = _build_subdub_command_base() + [
        "-i",
        resolved_srt_file,
        "-task",
        "equalize",
    ]
    _apply_model_options(command, model_options)
    return _run_subdub_command(command, "Equalization", model_options)


def find_latest_audio_file(session_dir: str) -> str | None:
    """Finds the newest audio file in a session directory."""
    if not os.path.isdir(session_dir):
        return None

    candidates: list[str] = []
    for file_name in os.listdir(session_dir):
        if not file_name.lower().endswith(MANUAL_CORRECTION_AUDIO_EXTENSIONS):
            continue

        full_path = os.path.join(session_dir, file_name)
        if os.path.isfile(full_path):
            candidates.append(full_path)

    if not candidates:
        return None

    return max(candidates, key=lambda path: (os.path.getmtime(path), path.lower()))


def extract_audio_for_manual_correction(video_file: str, session_dir: str) -> str | None:
    """Extracts a mono WAV reference track for Subdub's timing correction GUI."""
    if not video_file or not os.path.exists(video_file):
        logging.error("Cannot extract manual correction audio: video path is invalid (%s).", video_file)
        return None

    os.makedirs(session_dir, exist_ok=True)

    video_stem = os.path.splitext(os.path.basename(video_file))[0]
    audio_path = os.path.join(session_dir, f"{video_stem}_manual_correction.wav")

    if os.path.exists(audio_path):
        try:
            if os.path.getsize(audio_path) > 0 and os.path.getmtime(audio_path) >= os.path.getmtime(video_file):
                return audio_path
        except OSError:
            pass

    ffmpeg_command = [
        "ffmpeg",
        "-y",
        "-i",
        video_file,
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-af",
        "aresample,loudnorm",
        audio_path,
    ]

    logging.info("Extracting audio for manual timing correction: %s", " ".join(ffmpeg_command))
    try:
        process = subprocess.Popen(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
        )
        if process.stdout is not None:
            for line in process.stdout:
                logging.info("FFmpeg (Manual Correction): %s", line.strip())
        process.wait()

        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, ffmpeg_command)

        if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
            raise RuntimeError("FFmpeg did not produce a valid audio file for manual correction.")

        return audio_path
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logging.error("Failed to extract audio for manual correction: %s", e)
        return None
    except Exception as e:
        logging.error("Unexpected error while preparing manual correction audio: %s", e)
        return None


def open_manual_correction_gui(srt_file: str, audio_file: str, session_dir: str) -> str | None:
    """Opens Subdub's dedicated timing correction GUI for an SRT/audio pair."""
    srt_path = os.path.abspath(srt_file) if srt_file else ""
    if not srt_path or not os.path.exists(srt_path):
        logging.error("Cannot open manual correction GUI: SRT path is invalid (%s).", srt_file)
        return None

    audio_path = os.path.abspath(audio_file) if audio_file else ""
    if not audio_path or not os.path.exists(audio_path):
        logging.error("Cannot open manual correction GUI: audio path is invalid (%s).", audio_file)
        return None

    session_path = os.path.abspath(session_dir) if session_dir else ""
    if not session_path:
        logging.error("Cannot open manual correction GUI: session path is invalid (%s).", session_dir)
        return None

    os.makedirs(session_path, exist_ok=True)

    script = "\n".join(
        [
            "import sys",
            "try:",
            "    from subdub.workflows.manual_correction import open_manual_correction_gui",
            "except Exception:",
            "    from subdub.workflows.pipeline import open_manual_correction_gui",
            "result = open_manual_correction_gui(sys.argv[1], sys.argv[2], sys.argv[3])",
            f"print('{MANUAL_CORRECTION_RESULT_PREFIX}' + str(result or ''))",
        ]
    )

    command = [
        _resolve_python_executable(),
        "-c",
        script,
        srt_path,
        audio_path,
        session_path,
    ]

    success, output_lines = _run_subdub_command_with_output(command, "Manual Boundary Correction")
    if not success:
        return None

    corrected_srt_path = ""
    for line in reversed(output_lines):
        if line.startswith(MANUAL_CORRECTION_RESULT_PREFIX):
            corrected_srt_path = line[len(MANUAL_CORRECTION_RESULT_PREFIX) :].strip()
            break

    if corrected_srt_path:
        candidate_paths: list[str] = []
        if os.path.isabs(corrected_srt_path):
            candidate_paths.append(os.path.abspath(corrected_srt_path))
        else:
            raw_candidates = [
                corrected_srt_path,
                os.path.join(session_path, corrected_srt_path),
                os.path.join(os.path.dirname(srt_path), corrected_srt_path),
            ]
            if os.path.isdir(SUBDUB_REPO_PATH):
                raw_candidates.append(os.path.join(SUBDUB_REPO_PATH, corrected_srt_path))

            for raw_candidate in raw_candidates:
                candidate_path = os.path.abspath(raw_candidate)
                if candidate_path not in candidate_paths:
                    candidate_paths.append(candidate_path)

        for candidate_path in candidate_paths:
            if os.path.exists(candidate_path):
                return candidate_path

        logging.warning(
            "Manual correction GUI returned a non-existent path: %s",
            corrected_srt_path,
        )

    fallback_srt = srt_path
    return fallback_srt if os.path.exists(fallback_srt) else None

def _escape_ffmpeg_subtitles_filter_path(path: str) -> str:
    """Escapes an absolute subtitle path for FFmpeg subtitles filter usage."""
    normalized_path = os.path.abspath(path).replace("\\", "/")
    escaped_path = normalized_path.replace(":", r"\:")
    escaped_path = escaped_path.replace("'", r"\'")
    escaped_path = escaped_path.replace(",", r"\,")
    escaped_path = escaped_path.replace("[", r"\[")
    escaped_path = escaped_path.replace("]", r"\]")
    return escaped_path


def _normalize_executable_cache_key(executable: str) -> str:
    normalized = str(executable or "").strip()
    if not normalized:
        return ""

    expanded = os.path.expandvars(os.path.expanduser(normalized))
    resolved = shutil.which(expanded) if not os.path.isabs(expanded) else expanded
    final_path = resolved or expanded
    return os.path.normcase(os.path.abspath(final_path))


def _ffmpeg_supports_subtitles_filter(ffmpeg_executable: str) -> bool:
    cache_key = _normalize_executable_cache_key(ffmpeg_executable)
    if not cache_key:
        return False

    cached = _FFMPEG_SUBTITLES_SUPPORT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    supported = False
    try:
        probe = subprocess.run(
            [
                ffmpeg_executable,
                "-hide_banner",
                "-v",
                "error",
                "-h",
                "filter=subtitles",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
        )
        probe_output = str(probe.stdout or "")
        lowered_output = probe_output.lower()
        supported = (
            "unknown filter 'subtitles'" not in lowered_output
            and "no such filter" not in lowered_output
        )
    except (FileNotFoundError, OSError):
        supported = False

    _FFMPEG_SUBTITLES_SUPPORT_CACHE[cache_key] = supported
    return supported


def _discover_ffmpeg_candidates() -> list[str]:
    candidates: list[str] = []

    explicit_ffmpeg = str(os.environ.get("PANDRATOR_FFMPEG_EXE") or "").strip()
    if explicit_ffmpeg:
        candidates.append(explicit_ffmpeg)

    for bundled_candidate in BUNDLED_FFMPEG_CANDIDATES:
        if os.path.isfile(bundled_candidate):
            candidates.append(bundled_candidate)

    default_ffmpeg = shutil.which("ffmpeg")
    if default_ffmpeg:
        candidates.append(default_ffmpeg)

    candidates.append("ffmpeg")

    if os.name == "nt":
        try:
            where_result = subprocess.run(
                ["where.exe", "ffmpeg"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
            )
            if where_result.returncode == 0:
                for raw_line in str(where_result.stdout or "").splitlines():
                    candidate = raw_line.strip().strip('"')
                    if candidate:
                        candidates.append(candidate)
        except Exception:
            pass

    unique_candidates: list[str] = []
    seen_keys: set[str] = set()
    for candidate in candidates:
        cache_key = _normalize_executable_cache_key(candidate)
        if not cache_key or cache_key in seen_keys:
            continue
        seen_keys.add(cache_key)
        unique_candidates.append(candidate)

    return unique_candidates


def _resolve_ffmpeg_for_burned_subtitles() -> str | None:
    for candidate in _discover_ffmpeg_candidates():
        if _ffmpeg_supports_subtitles_filter(candidate):
            return candidate
    return None


def add_subtitles_to_video(
    synced_video_path: str,
    equalized_srt_path: str,
    output_video_path: str,
    subtitle_mode: str = "soft",
) -> bool:
    """Adds subtitles to a video file using FFmpeg.

    Writes to a temporary file first and only replaces the target output on
    success, so a failed remux does not destroy an existing final video.
    """
    normalized_subtitle_mode = str(subtitle_mode or "soft").strip().lower()
    if normalized_subtitle_mode not in {"soft", "burned"}:
        normalized_subtitle_mode = "soft"

    output_abs_path = os.path.abspath(output_video_path)
    output_dir = os.path.dirname(output_abs_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    output_name = os.path.basename(output_abs_path)
    output_stem, output_ext = os.path.splitext(output_name)
    temp_ext = output_ext or ".mp4"
    temp_output_path = os.path.join(output_dir or ".", f".{output_stem}_tmp{temp_ext}")

    if normalized_subtitle_mode == "burned":
        ffmpeg_executable = _resolve_ffmpeg_for_burned_subtitles()
        if not ffmpeg_executable:
            logging.error(
                "Cannot burn subtitles: no FFmpeg executable with the subtitles filter was found. "
                "Use an FFmpeg build compiled with --enable-libass, or use soft subtitles."
            )
            return False

        escaped_subtitle_path = _escape_ffmpeg_subtitles_filter_path(equalized_srt_path)
        ffmpeg_command = [
            ffmpeg_executable,
            "-y",
            "-i",
            synced_video_path,
            "-vf",
            f"subtitles=filename='{escaped_subtitle_path}'",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-c:a",
            "copy",
            temp_output_path,
        ]
    else:
        ffmpeg_command = [
            "ffmpeg",
            "-y",
            "-i",
            synced_video_path,
            "-i",
            equalized_srt_path,
            "-c",
            "copy",
            "-c:s",
            "mov_text",
            "-metadata:s:s:0",
            "language=eng",
            temp_output_path,
        ]

    logging.info(
        "Executing FFmpeg command to add subtitles (%s): %s",
        normalized_subtitle_mode,
        " ".join(ffmpeg_command),
    )
    try:
        process = subprocess.Popen(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
        )
        for line in process.stdout:
            logging.info(f"FFmpeg: {line.strip()}")
        process.wait()
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, ffmpeg_command)

        if not os.path.exists(temp_output_path) or os.path.getsize(temp_output_path) == 0:
            raise RuntimeError("FFmpeg did not create a valid output video.")

        os.replace(temp_output_path, output_abs_path)
        logging.info(
            "Subtitles have been successfully added to the final video (%s mode).",
            normalized_subtitle_mode,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logging.error(f"Failed to add subtitles: {e}")
        return False
    except Exception as e:
        logging.error(f"Unexpected error while adding subtitles: {e}")
        return False
    finally:
        if os.path.exists(temp_output_path):
            try:
                os.remove(temp_output_path)
            except OSError:
                pass
