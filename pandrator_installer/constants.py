"""Runtime pins, URLs, and installer filesystem constants."""

import os

from .catalog import (
    CHATTERBOX_GPU_SUPPORT_CONFIG_FLAG as CHATTERBOX_GPU_SUPPORT_CONFIG_FLAG,
    COMPONENTS,
    INSTALLER_STATE_FILENAME as INSTALLER_STATE_FILENAME,
    KOBOLD_QWEN_GPU_SUPPORT_CONFIG_FLAG as KOBOLD_QWEN_GPU_SUPPORT_CONFIG_FLAG,
    KOKORO_ENV_NAME as KOKORO_ENV_NAME,
    KOKORO_GPU_SUPPORT_CONFIG_FLAG as KOKORO_GPU_SUPPORT_CONFIG_FLAG,
    MAGPIE_GPU_SUPPORT_CONFIG_FLAG as MAGPIE_GPU_SUPPORT_CONFIG_FLAG,
    PARAKEET_ONNX_ENV_NAME as PARAKEET_ONNX_ENV_NAME,
    RVC_GPU_SUPPORT_CONFIG_FLAG as RVC_GPU_SUPPORT_CONFIG_FLAG,
    PACKAGING_COMPONENT_PATHS as PACKAGING_COMPONENT_PATHS,
    PACKAGING_CONFIG_FLAGS as PACKAGING_CONFIG_FLAGS,
    PACKAGING_EXCLUDED_FILE_PREFIXES as PACKAGING_EXCLUDED_FILE_PREFIXES,
    PACKAGING_LAYOUT_FILENAME as PACKAGING_LAYOUT_FILENAME,
    PACKAGING_SHARED_PATHS as PACKAGING_SHARED_PATHS,
)

PIXI_BINARY_NAME = 'pixi.exe'
PIXI_DOWNLOAD_URL = 'https://github.com/prefix-dev/pixi/releases/latest/download/pixi-x86_64-pc-windows-msvc.exe'
PIXI_HOME_DIRNAME = '.pixi-home'
PIXI_CACHE_DIRNAME = '.pixi-cache'
PIXI_PIP_CACHE_SUBDIRNAME = 'pip'
PIXI_TEMP_SUBDIRNAME = 'tmp'
PANDRATOR_PYTHON_VERSION = '3.11'
NEMO_PYNINI_CONDA_SPEC = 'pynini=2.1.6.post1'
NEMO_TEXT_PROCESSING_SPEC = 'nemo_text_processing==1.2.0'
NEMO_TEXT_PROCESSING_PIP_DEPS = (
    'cdifflib',
    'editdistance',
    'inflect',
    'joblib',
    'pandas',
    'sacremoses>=0.0.43',
    'setuptools>=65.5.1',
    'tqdm>=4.41.0',
    'transformers',
    'wget',
    'wrapt',
)
NEMO_TEXT_PROCESSING_CDIFFLIB_SHIM = (
    "import difflib\n"
    "CSequenceMatcher = difflib.SequenceMatcher\n"
)
PANDRATOR_NUMPY_SPEC = 'numpy==1.26.4'
WTPSPLIT_LITE_SPEC = 'wtpsplit-lite==0.2.0'
WTPSPLIT_MODEL = 'sat-12l-sm'
WTPSPLIT_RETIRED_MODELS = ('sat-3l-sm',)
PANDRATOR_PYMUPDF_SPEC = 'PyMuPDF>=1.25,<2'
PANDRATOR_ONNXRUNTIME_SPEC = 'onnxruntime>=1.20,<2'
PANDRATOR_PADDLEOCR_SPEC = 'paddleocr==3.7.0'
SILERO_PYTHON_VERSION = '3.10'
KOKORO_PYTHON_VERSION = '3.11'
KOKORO_TORCH_BASE_VERSION = '2.8.0'
KOKORO_CPU_TORCH_INDEX_URL = 'https://download.pytorch.org/whl/cpu'
KOKORO_GPU_TORCH_VERSION_X86_64 = f'{KOKORO_TORCH_BASE_VERSION}+cu126'
KOKORO_GPU_TORCH_VERSION_ARM64 = f'{KOKORO_TORCH_BASE_VERSION}+cu129'
KOKORO_GPU_TORCH_INDEX_URL_X86_64 = 'https://download.pytorch.org/whl/cu126'
KOKORO_GPU_TORCH_INDEX_URL_ARM64 = 'https://download.pytorch.org/whl/cu129'
XTTS_FINETUNING_PYTHON_VERSION = '3.13'
PYQT6_RUNTIME_PIN = 'PyQt6==6.7.1'
PYQT6_SIP_RUNTIME_SPEC = 'PyQt6-sip>=13.8,<14'
PYGAME_RUNTIME_SPEC = 'pygame>=2.6.1,<3'
PANDRATOR_RUNTIME_REPAIR_SPECS = (
    PYQT6_RUNTIME_PIN,
    PYQT6_SIP_RUNTIME_SPEC,
    PYGAME_RUNTIME_SPEC,
)
WHISPERX_PYTHON_VERSION = '3.13'
PARAKEET_ONNX_PYTHON_VERSION = '3.11'
ONNX_ASR_VERSION = '0.11.0'
ONNX_ASR_INSTALL_SPEC = f'onnx-asr[cpu,hub]=={ONNX_ASR_VERSION}'
ONNX_ASR_REQUIRED_PACKAGE_SPECS = (
    f'onnx-asr=={ONNX_ASR_VERSION}',
    'onnxruntime>=1.20,<2',
)
WHISPERX_VERSION = '3.8.5'
WHISPERX_CTRANSLATE2_VERSION = '4.7.1'
WHISPERX_TORCH_VERSION = '2.8.0'
WHISPERX_TORCHVISION_VERSION = '0.23.0'
WHISPERX_TORCHAUDIO_VERSION = '2.8.0'
WHISPERX_TORCH_INDEX_URL = 'https://download.pytorch.org/whl/cu128'

XTTS_API_REPO_URL = COMPONENTS['xtts'].repo_url
XTTS_API_REPO_DIRNAME = COMPONENTS['xtts'].repo_dirname
VOXCPM_API_REPO_URL = COMPONENTS['voxcpm'].repo_url
VOXCPM_API_REPO_DIRNAME = COMPONENTS['voxcpm'].repo_dirname
FISHS2_API_REPO_URL = COMPONENTS['fishs2'].repo_url
FISHS2_API_REPO_DIRNAME = COMPONENTS['fishs2'].repo_dirname
VOXTRAL_API_REPO_URL = COMPONENTS['voxtral'].repo_url
VOXTRAL_API_REPO_DIRNAME = COMPONENTS['voxtral'].repo_dirname
KOKORO_API_REPO_URL = COMPONENTS['kokoro'].repo_url
KOKORO_API_REPO_DIRNAME = COMPONENTS['kokoro'].repo_dirname
CHATTERBOX_API_REPO_URL = COMPONENTS['chatterbox'].repo_url
CHATTERBOX_API_REPO_DIRNAME = COMPONENTS['chatterbox'].repo_dirname
KOBOLD_QWEN_API_REPO_URL = COMPONENTS['kobold_qwen'].repo_url
KOBOLD_QWEN_API_REPO_DIRNAME = COMPONENTS['kobold_qwen'].repo_dirname
MAGPIE_API_REPO_URL = COMPONENTS['magpie'].repo_url
MAGPIE_API_REPO_DIRNAME = COMPONENTS['magpie'].repo_dirname
RVC_API_REPO_URL = COMPONENTS['rvc'].repo_url
RVC_API_REPO_DIRNAME = COMPONENTS['rvc'].repo_dirname
RVC_LINUX_FAIRSEQ_WHEEL = (
    'https://huggingface.co/JackismyShephard/ultimate-rvc/resolve/'
    '8ca15ee3b546bd7dd4725e88bb9a997181e4c298/'
    'fairseq-0.12.2-cp311-cp311-linux_x86_64.whl'
    '#sha256=81b5af664d23ea941175de10e1176da5113177a8668979829705eff27d4db54d'
)
RVC_LINUX_FAIRSEQ_DEPENDENCIES = (
    'setuptools<81',
    'hydra-core>=1.3.2',
    'cffi',
    'cython',
    'regex',
    'sacrebleu>=1.4.12',
    'tqdm',
    'bitarray',
    'scikit-learn',
    'packaging',
)
PANDRATOR_REPO_URL = 'https://github.com/lukaszliniewicz/Pandrator.git'
# Release installers always clone the current default application branch.
PANDRATOR_REPO_BRANCH = os.environ.get('PANDRATOR_REPO_BRANCH', 'main').strip() or 'main'
PYCROPPDF_REPO_URL = 'https://github.com/lukaszliniewicz/PyCropPDF.git'
EASY_XTTS_TRAINER_REPO_URL = COMPONENTS['xtts_finetuning'].repo_url

ESPEAK_NG_MSI_URL = 'https://github.com/espeak-ng/espeak-ng/releases/download/1.52.0/espeak-ng.msi'
ESPEAK_NG_MSI_SHA256 = '7F673C709EA5DD579D3B5EBB98688CC575328A6AB7438D2BC405B88CEDAEAFB9'
ESPEAK_NG_DLL_RELATIVE_PATH = os.path.join('eSpeak NG', 'libespeak-ng.dll')
ESPEAK_NG_DATA_DIR_RELATIVE_PATH = os.path.join('eSpeak NG', 'espeak-ng-data')
CALIBRE_WIN64_MSI_URL = 'https://calibre-ebook.com/dist/win64'
CALIBRE_BUNDLED_DIRNAME = 'Calibre Portable'
CALIBRE_BUNDLED_CALIBRE_SUBDIR = 'Calibre'
CALIBRE_BUNDLED_EBOOK_CONVERT_RELATIVE_PATH = os.path.join(
    CALIBRE_BUNDLED_DIRNAME,
    CALIBRE_BUNDLED_CALIBRE_SUBDIR,
    'ebook-convert.exe',
)
FFMPEG_SUBTITLES_WINDOWS_ZIP_URL = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
FFMPEG_BUNDLED_RELATIVE_PATH = os.path.join('bin', 'ffmpeg.exe')

BUNDLED_WHEELS_RELATIVE_PATH = os.path.join('vendor', 'wheels')
PYOPENJTALK_WHEEL_PREFIX = 'pyopenjtalk-'
SILERO_REQUIRED_PACKAGE_SPECS = (
    'requests',
    'silero-api-server',
)
WHISPERX_REQUIRED_PACKAGE_SPECS = (
    f'whisperx=={WHISPERX_VERSION}',
    f'ctranslate2=={WHISPERX_CTRANSLATE2_VERSION}',
    f'torch=={WHISPERX_TORCH_VERSION}',
    f'torchvision=={WHISPERX_TORCHVISION_VERSION}',
    f'torchaudio=={WHISPERX_TORCHAUDIO_VERSION}',
)
XTTS_FINETUNING_TORCH_PACKAGE_SPECS = (
    f'torch=={WHISPERX_TORCH_VERSION}',
    f'torchvision=={WHISPERX_TORCHVISION_VERSION}',
    f'torchaudio=={WHISPERX_TORCHAUDIO_VERSION}',
)
XTTS_FINETUNING_TORCH_INDEX_URL = WHISPERX_TORCH_INDEX_URL
XTTS_FINETUNING_BUNDLED_WHEEL_PREFIX = 'ctc_forced_aligner-'
OPTIONAL_REQUIREMENT_EXCLUSIONS_BY_ENV = {
    'easy_xtts_trainer': (
        'breath-removal',
    ),
}
