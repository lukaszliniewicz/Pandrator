import os
import shutil
import logging
import tempfile
import threading
from pydub import AudioSegment

# Conditional imports
try:
    import torch
    from rvc_python.infer import RVCInference
    RVC_AVAILABLE = True
except ImportError:
    RVC_AVAILABLE = False

RVC_INFERENCE_INSTANCE = None
RVC_LOCK = threading.RLock()
RVC_ACTIVE_MODEL = None
RVC_ACTIVE_PARAMS = None

def is_rvc_available() -> bool:
    """Checks if torch and rvc_python are installed."""
    return RVC_AVAILABLE

def get_rvc_models(rvc_models_dir: str) -> list[str]:
    """Lists the available RVC models from the specified directory."""
    if os.path.exists(rvc_models_dir):
        return [
            folder for folder in os.listdir(rvc_models_dir)
            if os.path.isdir(os.path.join(rvc_models_dir, folder))
        ]
    return []

def initialize_rvc(rvc_models_dir: str) -> bool:
    """Initializes the RVCInference instance."""
    global RVC_INFERENCE_INSTANCE, RVC_ACTIVE_MODEL, RVC_ACTIVE_PARAMS
    if not is_rvc_available():
        logging.warning("RVC functionality not available. Skipping initialization.")
        return False
    try:
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        with RVC_LOCK:
            if RVC_INFERENCE_INSTANCE:
                try:
                    RVC_INFERENCE_INSTANCE.unload_model()
                except Exception:
                    pass
            RVC_INFERENCE_INSTANCE = RVCInference(models_dir=rvc_models_dir, device=device)
            RVC_ACTIVE_MODEL = None
            RVC_ACTIVE_PARAMS = None
        logging.info(f"RVC initialized successfully. Using device: {device}")
        if torch.cuda.is_available():
            logging.info(f"GPU: {torch.cuda.get_device_name(0)}")
        return True
    except Exception as e:
        logging.error(f"Failed to initialize RVC: {e}")
        RVC_INFERENCE_INSTANCE = None
        RVC_ACTIVE_MODEL = None
        RVC_ACTIVE_PARAMS = None
        return False


def _build_rvc_param_signature(settings: dict) -> tuple:
    return (
        int(settings.get("pitch", 0)),
        str(settings.get("f0_method", "rmvpe")),
        float(settings.get("index_rate", 0.3)),
        int(settings.get("filter_radius", 3)),
        float(settings.get("volume_envelope", 1.0)),
        float(settings.get("protect", 0.3)),
    )


def _refresh_rvc_model_index() -> bool:
    if RVC_INFERENCE_INSTANCE and hasattr(RVC_INFERENCE_INSTANCE, "set_models_dir"):
        try:
            RVC_INFERENCE_INSTANCE.set_models_dir(RVC_INFERENCE_INSTANCE.models_dir)
            return True
        except Exception as e:
            logging.warning(f"Failed to refresh RVC model index: {e}")
    return False

def process_with_rvc(audio_segment: AudioSegment, settings: dict) -> AudioSegment:
    """
    Processes an audio segment with RVC.
    `settings` is a dictionary-like object (e.g., a dataclass).
    Returns the original audio segment if processing fails.
    """
    global RVC_ACTIVE_MODEL, RVC_ACTIVE_PARAMS

    if not RVC_INFERENCE_INSTANCE:
        logging.warning("RVC is not initialized. Skipping RVC processing.")
        return audio_segment

    model_name = settings.get("rvc_model")
    if not model_name:
        logging.warning("No RVC model selected. Skipping RVC processing.")
        return audio_segment

    temp_input_path = None
    temp_output_path = None

    try:
        with RVC_LOCK:
            if RVC_ACTIVE_MODEL != model_name:
                available_models = (
                    RVC_INFERENCE_INSTANCE.list_models()
                    if hasattr(RVC_INFERENCE_INSTANCE, "list_models")
                    else []
                )
                if model_name not in available_models:
                    _refresh_rvc_model_index()

                if RVC_ACTIVE_MODEL:
                    RVC_INFERENCE_INSTANCE.unload_model()

                RVC_INFERENCE_INSTANCE.load_model(model_name)
                RVC_ACTIVE_MODEL = model_name
                RVC_ACTIVE_PARAMS = None

            target_params = _build_rvc_param_signature(settings)
            if RVC_ACTIVE_PARAMS != target_params:
                RVC_INFERENCE_INSTANCE.set_params(
                    f0up_key=target_params[0],
                    f0method=target_params[1],
                    index_rate=target_params[2],
                    filter_radius=target_params[3],
                    resample_sr=40000,
                    rms_mix_rate=target_params[4],
                    protect=target_params[5]
                )
                RVC_ACTIVE_PARAMS = target_params

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_input_file:
            temp_input_path = temp_input_file.name
            audio_segment.export(temp_input_path, format="wav")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_output_file:
            temp_output_path = temp_output_file.name

        with RVC_LOCK:
            RVC_INFERENCE_INSTANCE.infer_file(temp_input_path, temp_output_path)

        return AudioSegment.from_wav(temp_output_path)

    except Exception as e:
        logging.error(f"RVC Processing Error: {e}")

        with RVC_LOCK:
            if RVC_INFERENCE_INSTANCE and RVC_ACTIVE_MODEL:
                try:
                    RVC_INFERENCE_INSTANCE.unload_model()
                except Exception:
                    pass
                RVC_ACTIVE_MODEL = None
                RVC_ACTIVE_PARAMS = None

        return audio_segment
    finally:
        if temp_input_path and os.path.exists(temp_input_path):
            os.unlink(temp_input_path)
        if temp_output_path and os.path.exists(temp_output_path):
            os.unlink(temp_output_path)

def upload_rvc_model(pth_file: str, index_file: str, rvc_models_dir: str) -> str:
    """Copies RVC model files to the rvc_models directory."""
    global RVC_ACTIVE_MODEL, RVC_ACTIVE_PARAMS

    model_name = os.path.splitext(os.path.basename(pth_file))[0]
    model_dir = os.path.join(rvc_models_dir, model_name)
    os.makedirs(model_dir, exist_ok=True)
    
    shutil.copy(pth_file, os.path.join(model_dir, f"{model_name}.pth"))
    
    index_ext = os.path.splitext(index_file)[1]
    shutil.copy(index_file, os.path.join(model_dir, f"{model_name}{index_ext}"))

    with RVC_LOCK:
        if RVC_INFERENCE_INSTANCE:
            _refresh_rvc_model_index()
            if RVC_ACTIVE_MODEL == model_name:
                RVC_INFERENCE_INSTANCE.unload_model()
                RVC_ACTIVE_MODEL = None
                RVC_ACTIVE_PARAMS = None
    
    return model_name
