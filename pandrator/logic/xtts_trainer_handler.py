import os
import subprocess
import logging
import shutil
import time
from typing import Callable

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
WORKSPACE_ROOT = os.path.abspath(os.path.join(PROJECT_ROOT, '..'))

TRAINER_ENV_NAME = 'easy_xtts_trainer'
WHISPERX_ENV_NAME = 'whisperx_installer'
WHISPERX_PIXI_EXE_ENV = 'WHISPERX_PIXI_EXE'
WHISPERX_PIXI_MANIFEST_ENV = 'WHISPERX_PIXI_MANIFEST'
PANDRATOR_DUBBING_CACHE_DIR_ENV = 'PANDRATOR_DUBBING_CACHE_DIR'
DEFAULT_WHISPER_CACHE_ROOT = os.path.join(PROJECT_ROOT, 'cache')


def _deduplicate_paths(paths: list[str]) -> list[str]:
    unique_paths: list[str] = []
    seen_normalized: set[str] = set()

    for path in paths:
        normalized = os.path.normcase(os.path.normpath(path))
        if normalized in seen_normalized:
            continue

        seen_normalized.add(normalized)
        unique_paths.append(path)

    return unique_paths


def _candidate_roots() -> list[str]:
    return _deduplicate_paths([PROJECT_ROOT, WORKSPACE_ROOT])


def _first_existing_path(candidates: list[str], exists_check: Callable[[str], bool]) -> str | None:
    for candidate in candidates:
        if exists_check(candidate):
            return candidate
    return None


def _is_executable_available(executable_path: str) -> bool:
    if os.path.isfile(executable_path):
        return True
    return shutil.which(executable_path) is not None


def _normalize_path(path: str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(str(path or '').strip()))
    return os.path.abspath(expanded) if expanded else ''


def _resolve_whisper_cache_root() -> str:
    configured_path = str(os.environ.get(PANDRATOR_DUBBING_CACHE_DIR_ENV, '')).strip()
    if configured_path:
        return _normalize_path(configured_path)

    return DEFAULT_WHISPER_CACHE_ROOT


def _setdefault_directory_env(env: dict[str, str], env_key: str, default_path: str) -> None:
    configured_path = str(env.get(env_key) or '').strip()
    target_path = _normalize_path(configured_path or default_path)
    if not target_path:
        return

    if not os.path.isdir(target_path):
        try:
            os.makedirs(target_path, exist_ok=True)
        except OSError as error:
            logging.warning(
                "Could not prepare cache directory for %s at %s: %s",
                env_key,
                target_path,
                error,
            )
            return

    env[env_key] = target_path


def _apply_whisper_cache_environment(env: dict[str, str]) -> None:
    cache_root = _resolve_whisper_cache_root()
    huggingface_root = os.path.join(cache_root, 'huggingface')
    huggingface_hub_cache = os.path.join(huggingface_root, 'hub')

    _setdefault_directory_env(env, 'XDG_CACHE_HOME', cache_root)
    _setdefault_directory_env(env, 'HF_HOME', huggingface_root)
    _setdefault_directory_env(env, 'HF_HUB_CACHE', huggingface_hub_cache)
    _setdefault_directory_env(env, 'HUGGINGFACE_HUB_CACHE', huggingface_hub_cache)
    _setdefault_directory_env(env, 'TRANSFORMERS_CACHE', os.path.join(huggingface_root, 'transformers'))
    _setdefault_directory_env(env, 'TORCH_HOME', os.path.join(cache_root, 'torch'))
    _setdefault_directory_env(env, 'TTS_HOME', os.path.join(cache_root, 'tts'))


def _manifest_candidates(roots: list[str], env_name: str) -> list[str]:
    return [os.path.join(root, 'envs', env_name, 'pixi.toml') for root in roots]


def _resolve_manifest_path(roots: list[str], env_name: str) -> str:
    candidates = _manifest_candidates(roots, env_name)
    existing_manifest = _first_existing_path(candidates, os.path.isfile)
    if existing_manifest:
        return existing_manifest
    return candidates[0]


def _resolve_pixi_executable(roots: list[str]) -> str:
    pixi_override = str(os.environ.get(WHISPERX_PIXI_EXE_ENV, '')).strip()
    if pixi_override and _is_executable_available(pixi_override):
        return pixi_override

    pixi_candidates = []
    for root in roots:
        pixi_candidates.append(os.path.join(root, 'bin', 'pixi.exe'))
        pixi_candidates.append(os.path.join(root, 'bin', 'pixi'))

    existing_pixi = _first_existing_path(pixi_candidates, os.path.isfile)
    if existing_pixi:
        return existing_pixi

    path_pixi = shutil.which('pixi')
    if path_pixi:
        return path_pixi

    return pixi_candidates[0]


def _resolve_trainer_dir(roots: list[str]) -> str:
    trainer_candidates = [os.path.join(root, 'easy_xtts_trainer') for root in roots]
    existing_trainer_dir = _first_existing_path(trainer_candidates, os.path.isdir)
    if existing_trainer_dir:
        return existing_trainer_dir
    return trainer_candidates[0]


def _resolve_xtts_models_dir(roots: list[str], trainer_dir: str) -> str:
    model_dir_candidates = [
        os.path.abspath(os.path.join(trainer_dir, '..', 'xtts-api-server', 'xtts_models')),
        os.path.abspath(os.path.join(trainer_dir, '..', 'xtts2_api', 'xtts_models')),
    ]

    for root in roots:
        model_dir_candidates.extend(
            [
                os.path.join(root, 'xtts-api-server', 'xtts_models'),
                os.path.join(root, 'xtts2_api', 'xtts_models'),
            ]
        )

    existing_models_dir = _first_existing_path(
        _deduplicate_paths(model_dir_candidates),
        os.path.isdir,
    )
    if existing_models_dir:
        return existing_models_dir

    return model_dir_candidates[0]


def _detect_legacy_conda_paths(roots: list[str]) -> tuple[str, str, str]:
    conda_executable_candidates = [
        os.path.join(root, 'conda', 'Scripts', 'conda.exe')
        for root in roots
    ]
    trainer_conda_env_candidates = [
        os.path.join(root, 'conda', 'envs', TRAINER_ENV_NAME)
        for root in roots
    ]
    whisperx_conda_env_candidates = [
        os.path.join(root, 'conda', 'envs', WHISPERX_ENV_NAME)
        for root in roots
    ]

    legacy_conda_executable = _first_existing_path(conda_executable_candidates, os.path.isfile) or ''
    legacy_trainer_conda_env = _first_existing_path(trainer_conda_env_candidates, os.path.isdir) or ''
    legacy_whisperx_conda_env = _first_existing_path(whisperx_conda_env_candidates, os.path.isdir) or ''
    return legacy_conda_executable, legacy_trainer_conda_env, legacy_whisperx_conda_env


def _build_trainer_subprocess_env(paths: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    _apply_whisper_cache_environment(env)
    env.setdefault(WHISPERX_PIXI_EXE_ENV, paths['pixi_executable'])
    env.setdefault(WHISPERX_PIXI_MANIFEST_ENV, paths['whisperx_manifest'])
    return env


def get_training_paths() -> dict[str, str]:
    """Returns all filesystem paths required by the XTTS training workflow."""
    roots = _candidate_roots()
    trainer_dir = _resolve_trainer_dir(roots)
    trainer_script = os.path.join(trainer_dir, 'easy_xtts_trainer.py')
    legacy_conda_executable, legacy_trainer_conda_env, legacy_whisperx_conda_env = _detect_legacy_conda_paths(roots)

    return {
        'pixi_executable': _resolve_pixi_executable(roots),
        'trainer_manifest': _resolve_manifest_path(roots, TRAINER_ENV_NAME),
        'trainer_dir': trainer_dir,
        'trainer_script': trainer_script,
        'xtts_models_dir': _resolve_xtts_models_dir(roots, trainer_dir),
        'whisperx_manifest': _resolve_manifest_path(roots, WHISPERX_ENV_NAME),
        'legacy_conda_executable': legacy_conda_executable,
        'legacy_trainer_conda_env': legacy_trainer_conda_env,
        'legacy_whisperx_conda_env': legacy_whisperx_conda_env,
    }


def validate_training_environment() -> tuple[bool, str]:
    """Validates external dependencies and trainer paths in one place."""
    paths = get_training_paths()
    checks = [
        ('Pixi executable', paths['pixi_executable'], _is_executable_available),
        ('Trainer Pixi manifest', paths['trainer_manifest'], os.path.isfile),
        ('WhisperX Pixi manifest', paths['whisperx_manifest'], os.path.isfile),
        ('Trainer directory', paths['trainer_dir'], os.path.isdir),
        ('Trainer script', paths['trainer_script'], os.path.isfile),
    ]

    missing = [
        f'- {label}: {path}'
        for label, path, exists_check in checks
        if not exists_check(path)
    ]

    if missing:
        message_lines = ['XTTS trainer setup is incomplete:', *missing]

        if (
            paths['legacy_conda_executable']
            or paths['legacy_trainer_conda_env']
            or paths['legacy_whisperx_conda_env']
        ):
            message_lines.extend(
                [
                    '',
                    'Legacy Conda trainer artifacts were detected during migration:',
                ]
            )
            if paths['legacy_conda_executable']:
                message_lines.append(f"- Conda executable: {paths['legacy_conda_executable']}")
            if paths['legacy_trainer_conda_env']:
                message_lines.append(f"- Trainer env: {paths['legacy_trainer_conda_env']}")
            if paths['legacy_whisperx_conda_env']:
                message_lines.append(f"- WhisperX env: {paths['legacy_whisperx_conda_env']}")

            message_lines.append(
                'Run Pandrator Installer -> Update Pandrator to migrate this workspace to '
                'Pixi manifests in envs/easy_xtts_trainer/pixi.toml and envs/whisperx_installer/pixi.toml.'
            )

        message = '\n'.join(message_lines)
        logging.error(message)
        return False, message

    return True, "XTTS trainer environment is ready."


def _emit_status(status_callback: Callable[[str], None] | None, message: str):
    if status_callback:
        status_callback(message)


def _normalize_sample_method(value: str) -> str:
    normalized = str(value or '').strip().lower().replace('_', '-').replace(' ', '-')
    aliases = {
        'mixed': 'mixed',
        'maximise-punctuation': 'maximise-punctuation',
        'maximize-punctuation': 'maximise-punctuation',
        'punctuation': 'punctuation-only',
        'punctuation-only': 'punctuation-only',
    }
    return aliases.get(normalized, normalized)


def start_training(
    settings: dict,
    output_callback: Callable[[str], None] | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """Runs XTTS training and returns `(success, message)`."""
    model_name = str(settings.get("model_name", "")).strip()
    source_audio_path = str(settings.get("source_audio_path", "")).strip()
    source_text_path = str(settings.get('source_text_path', '')).strip()

    if not source_audio_path or not model_name:
        message = "Source audio path and model name are required for training."
        logging.error(message)
        return False, message

    if not os.path.exists(source_audio_path):
        message = f"Source audio path does not exist: {source_audio_path}"
        logging.error(message)
        return False, message

    if source_text_path and not os.path.isfile(source_text_path):
        message = f"CTC text source path does not exist: {source_text_path}"
        logging.error(message)
        return False, message

    sample_method = _normalize_sample_method(settings.get('sample_method', 'mixed'))

    chapter_per_audio = 1
    try:
        chapter_per_audio = max(1, int(settings.get('chapter_per_audio', 1)))
    except (TypeError, ValueError):
        chapter_per_audio = 1

    setup_ok, setup_message = validate_training_environment()
    if not setup_ok:
        return False, setup_message

    paths = get_training_paths()
    _emit_status(status_callback, "Building XTTS training command...")

    command = [
        paths['pixi_executable'],
        'run',
        '--manifest-path',
        paths['trainer_manifest'],
        '--executable',
        'python',
        paths['trainer_script'],
        '--input',
        source_audio_path,
        '--source-language',
        settings.get('model_language', 'en'),
        '--whisper-model',
        settings.get('whisper_model', 'large-v3'),
        '--session',
        model_name,
        '--epochs',
        str(settings.get('epochs', 6)),
        '--gradient',
        str(settings.get('gradient', 1)),
        '--batch',
        str(settings.get('batches', 2)),
        '--sample-method',
        sample_method,
        '--sample-rate',
        str(settings.get('sample_rate', 22050)),
        '--max-audio-time',
        str(float(settings.get('max_duration', 11.0))),
        '--max-text-length',
        str(int(settings.get('max_text_length', 200))),
        '--method-proportion',
        settings.get('method_proportion', '6_4'),
        '--training-proportion',
        settings.get('training_split', '8_2'),
        '--voice-sample-mode',
        settings.get('voice_sample_mode', 'basic'),
    ]

    if settings.get('voice_sample_mode') != 'basic':
        command.extend(['--voice-samples', str(settings.get('voice_samples_count', 3))])
    
    if settings.get('voice_sample_only_sentence'):
        command.append('--voice-sample-only-sentence')

    if settings.get('alignment_model', '').strip():
        command.extend(['--align-model', settings.get('alignment_model').strip()])

    if source_text_path:
        command.extend(['--source-text', source_text_path])
        command.extend(['--chapter-per-audio', str(chapter_per_audio)])

    if settings.get('enable_denoise'):
        command.append('--denoise')
    if settings.get('enable_breath_removal'):
        command.append('--breath')
    if settings.get('enable_normalize'):
        command.extend(['--normalize', str(settings.get('lufs_value', '-16')).strip('-')])
    if settings.get('enable_compress'):
        command.extend(['--compress', settings.get('compress_profile', 'neutral')])
    if settings.get('enable_dess'):
        command.append('--dess')

    process_env = _build_trainer_subprocess_env(paths)
    logging.info('Executing training command: %s', ' '.join(command))
    logging.info(
        'XTTS trainer WhisperX contract: %s=%s, %s=%s',
        WHISPERX_PIXI_EXE_ENV,
        process_env.get(WHISPERX_PIXI_EXE_ENV, ''),
        WHISPERX_PIXI_MANIFEST_ENV,
        process_env.get(WHISPERX_PIXI_MANIFEST_ENV, ''),
    )

    _emit_status(status_callback, "XTTS training in progress...")
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, universal_newlines=True, encoding='utf-8', errors='replace',
            cwd=paths['trainer_dir'],
            env=process_env,
        )
        
        if process.stdout is not None:
            for line in process.stdout:
                cleaned = line.strip()
                if not cleaned:
                    continue

                logging.info("XTTS Trainer: %s", cleaned)
                if output_callback:
                    output_callback(cleaned)
        
        process.wait()

        if process.returncode == 0:
            _emit_status(status_callback, "Training finished. Copying model artifacts...")
            copy_ok, copy_message = _copy_trained_model(model_name, paths)
            if copy_ok:
                success_message = "Training completed and model copied successfully."
                logging.info(success_message)
                _emit_status(status_callback, success_message)
                return True, success_message
            return False, copy_message
        else:
            message = f"Training failed with return code {process.returncode}."
            logging.error(message)
            return False, message

    except Exception as e:
        message = f"An exception occurred during XTTS training: {e}"
        logging.error(message, exc_info=True)
        return False, message


def _copy_trained_model(model_name: str, paths: dict[str, str]) -> tuple[bool, str]:
    """Copies the final trained model files to the xtts-api-server directory."""
    try:
        source_dir = os.path.join(paths["trainer_dir"], model_name, "models")
        target_dir = os.path.join(paths["xtts_models_dir"], model_name)

        if not os.path.isdir(source_dir):
            message = f"Training output directory not found: {source_dir}"
            logging.error(message)
            return False, message

        os.makedirs(target_dir, exist_ok=True)

        xtts_folder = None
        for attempt in range(10):
            xtts_folder = next((f for f in os.listdir(source_dir) if f.startswith("xtts")), None)
            if xtts_folder:
                break

            logging.info(
                "Attempt %d: waiting for XTTS model folder in %s",
                attempt + 1,
                source_dir,
            )
            time.sleep(3)

        if not xtts_folder:
            message = "Trained XTTS model folder not found after waiting for artifacts."
            logging.error(message)
            return False, message

        source_xtts_dir = os.path.join(source_dir, xtts_folder)
        for item in os.listdir(source_xtts_dir):
            if item != 'run':
                s = os.path.join(source_xtts_dir, item)
                d = os.path.join(target_dir, item)
                if os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True)
                else:
                    shutil.copy2(s, d)

        message = f"Trained model '{model_name}' copied to {target_dir}"
        logging.info(message)
        return True, message
    except Exception as e:
        message = f"Error copying trained model: {e}"
        logging.error(message, exc_info=True)
        return False, message
