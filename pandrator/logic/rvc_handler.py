import os
import shutil
import logging
import tempfile
from pydub import AudioSegment

# Conditional imports
try:
    import torch
    from rvc_python.infer import RVCInference
    RVC_AVAILABLE = True
except ImportError:
    RVC_AVAILABLE = False

RVC_INFERENCE_INSTANCE = None

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
    global RVC_INFERENCE_INSTANCE
    if not is_rvc_available():
        logging.warning("RVC functionality not available. Skipping initialization.")
        return False
    try:
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        RVC_INFERENCE_INSTANCE = RVCInference(models_dir=rvc_models_dir, device=device)
        logging.info(f"RVC initialized successfully. Using device: {device}")
        if torch.cuda.is_available():
            logging.info(f"GPU: {torch.cuda.get_device_name(0)}")
        return True
    except Exception as e:
        logging.error(f"Failed to initialize RVC: {e}")
        RVC_INFERENCE_INSTANCE = None
        return False

def process_with_rvc(audio_segment: AudioSegment, settings: dict) -> AudioSegment:
    """
    Processes an audio segment with RVC.
    `settings` is a dictionary-like object (e.g., a dataclass).
    Returns the original audio segment if processing fails.
    """
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
        RVC_INFERENCE_INSTANCE.load_model(model_name)
        RVC_INFERENCE_INSTANCE.set_params(
            f0up_key=settings.get("pitch", 0),
            f0method=settings.get("f0_method", "rmvpe"),
            index_rate=settings.get("index_rate", 0.3),
            filter_radius=settings.get("filter_radius", 3),
            resample_sr=40000,
            rms_mix_rate=settings.get("volume_envelope", 1.0),
            protect=settings.get("protect", 0.3)
        )

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_input_file:
            temp_input_path = temp_input_file.name
            audio_segment.export(temp_input_path, format="wav")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_output_file:
            temp_output_path = temp_output_file.name

        RVC_INFERENCE_INSTANCE.infer_file(temp_input_path, temp_output_path)
        return AudioSegment.from_wav(temp_output_path)

    except Exception as e:
        logging.error(f"RVC Processing Error: {e}")
        return audio_segment
    finally:
        if RVC_INFERENCE_INSTANCE:
            RVC_INFERENCE_INSTANCE.unload_model()
        if temp_input_path and os.path.exists(temp_input_path):
            os.unlink(temp_input_path)
        if temp_output_path and os.path.exists(temp_output_path):
            os.unlink(temp_output_path)

def upload_rvc_model(pth_file: str, index_file: str, rvc_models_dir: str) -> str:
    """Copies RVC model files to the rvc_models directory."""
    model_name = os.path.splitext(os.path.basename(pth_file))[0]
    model_dir = os.path.join(rvc_models_dir, model_name)
    os.makedirs(model_dir, exist_ok=True)
    
    shutil.copy(pth_file, os.path.join(model_dir, f"{model_name}.pth"))
    
    index_ext = os.path.splitext(index_file)[1]
    shutil.copy(index_file, os.path.join(model_dir, f"{model_name}{index_ext}"))
    
    return model_name
