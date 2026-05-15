import os
import subprocess
import logging
import shutil
import time
from typing import Callable

# Assuming conda and trainer scripts are in known relative locations
CONDA_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'conda', 'Scripts', 'conda.exe'))
TRAINER_ENV_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'conda', 'envs', 'easy_xtts_trainer'))
TRAINER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'easy_xtts_trainer'))
TRAINER_SCRIPT = os.path.join(TRAINER_DIR, 'easy_xtts_trainer.py')
XTTS_MODELS_DIR = os.path.abspath(os.path.join(TRAINER_DIR, '..', 'xtts-api-server', 'xtts_models'))


def get_training_paths() -> dict[str, str]:
    """Returns all filesystem paths required by the XTTS training workflow."""
    return {
        "conda_path": CONDA_PATH,
        "trainer_env_path": TRAINER_ENV_PATH,
        "trainer_dir": TRAINER_DIR,
        "trainer_script": TRAINER_SCRIPT,
        "xtts_models_dir": XTTS_MODELS_DIR,
    }


def validate_training_environment() -> tuple[bool, str]:
    """Validates external dependencies and trainer paths in one place."""
    paths = get_training_paths()
    checks = [
        ("Conda executable", paths["conda_path"]),
        ("Trainer environment", paths["trainer_env_path"]),
        ("Trainer directory", paths["trainer_dir"]),
        ("Trainer script", paths["trainer_script"]),
    ]

    missing = [f"- {label}: {path}" for label, path in checks if not os.path.exists(path)]
    if missing:
        message = "XTTS trainer setup is incomplete:\n" + "\n".join(missing)
        logging.error(message)
        return False, message

    return True, "XTTS trainer environment is ready."


def _emit_status(status_callback: Callable[[str], None] | None, message: str):
    if status_callback:
        status_callback(message)


def start_training(
    settings: dict,
    output_callback: Callable[[str], None] | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """Runs XTTS training and returns `(success, message)`."""
    model_name = str(settings.get("model_name", "")).strip()
    source_audio_path = str(settings.get("source_audio_path", "")).strip()
    if not source_audio_path or not model_name:
        message = "Source audio path and model name are required for training."
        logging.error(message)
        return False, message

    if not os.path.exists(source_audio_path):
        message = f"Source audio path does not exist: {source_audio_path}"
        logging.error(message)
        return False, message

    setup_ok, setup_message = validate_training_environment()
    if not setup_ok:
        return False, setup_message

    paths = get_training_paths()
    _emit_status(status_callback, "Building XTTS training command...")

    command = [
        paths["conda_path"], "run", "-p", paths["trainer_env_path"], '--no-capture-output', "python", paths["trainer_script"],
        "--input", source_audio_path,
        "--source-language", settings.get("model_language", "en"),
        "--whisper-model", settings.get("whisper_model", "large-v3"),
        "--session", model_name,
        "--epochs", str(settings.get("epochs", 6)),
        "--gradient", str(settings.get("gradient", 1)),
        "--batch", str(settings.get("batches", 2)),
        "--sample-method", settings.get("sample_method", "Mixed").lower().replace(" ", "-"),
        "--sample-rate", str(settings.get("sample_rate", 22050)),
        "--max-audio-time", str(float(settings.get("max_duration", 11.0))),
        "--max-text-length", str(int(settings.get("max_text_length", 200))),
        "--method-proportion", settings.get("method_proportion", "6_4"),
        "--training-proportion", settings.get("training_split", "9_1"),
        "--voice-sample-mode", settings.get("voice_sample_mode", "basic")
    ]

    if settings.get("voice_sample_mode") != "basic":
        command.extend(["--voice-samples", str(settings.get("voice_samples_count", 3))])
    
    if settings.get("voice_sample_only_sentence"):
        command.append("--voice-sample-only-sentence")

    if settings.get("alignment_model", "").strip():
        command.extend(["--align-model", settings.get("alignment_model").strip()])

    if settings.get("enable_denoise"): command.append("--denoise")
    if settings.get("enable_breath_removal"): command.append("--breath")
    if settings.get("enable_normalize"): command.extend(["--normalize", str(settings.get("lufs_value", "-16")).strip('-')])
    if settings.get("enable_compress"): command.extend(["--compress", settings.get("compress_profile", "neutral")])
    if settings.get("enable_dess"): command.append("--dess")

    logging.info("Executing training command: %s", " ".join(command))

    _emit_status(status_callback, "XTTS training in progress...")
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, universal_newlines=True, encoding='utf-8', errors='replace',
            cwd=paths["trainer_dir"],
        )
        
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
