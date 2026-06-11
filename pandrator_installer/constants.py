"""Runtime pins, URLs, and installer filesystem constants."""

import os

from .catalog import (
    CHATTERBOX_GPU_SUPPORT_CONFIG_FLAG as CHATTERBOX_GPU_SUPPORT_CONFIG_FLAG,
    COMPONENTS,
    INSTALLER_STATE_FILENAME as INSTALLER_STATE_FILENAME,
    KOKORO_ENV_NAME as KOKORO_ENV_NAME,
    KOKORO_GPU_SUPPORT_CONFIG_FLAG as KOKORO_GPU_SUPPORT_CONFIG_FLAG,
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
SILERO_PYTHON_VERSION = '3.10'
KOKORO_PYTHON_VERSION = '3.11'
KOKORO_TORCH_BASE_VERSION = '2.8.0'
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
SUBDUB_EDITABLE_INSTALL_SPEC = '.[gui]'
SUBDUB_GUI_RUNTIME_REPAIR_SPECS = (
    PYQT6_RUNTIME_PIN,
    PYQT6_SIP_RUNTIME_SPEC,
    'matplotlib',
    'sounddevice',
)
SUBDUB_RUNTIME_REPAIR_SPECS = (
    'litellm',
    'tiktoken',
    'fastuuid',
    *SUBDUB_GUI_RUNTIME_REPAIR_SPECS,
)
SUBDUB_RUNTIME_CHECK_COMMAND = [
    'python',
    '-c',
    (
        'import subdub; import litellm, tiktoken, fastuuid; '
        'from PyQt6.QtWidgets import QApplication; '
        'import matplotlib; import sounddevice; '
        'import subdub.corrector.gui.app'
    ),
]
WHISPERX_PYTHON_VERSION = '3.13'
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
PANDRATOR_REPO_URL = 'https://github.com/lukaszliniewicz/Pandrator.git'
SUBDUB_REPO_URL = 'https://github.com/lukaszliniewicz/Subdub.git'
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
RVC_PYTHON_FORK_INSTALL_SPEC = 'git+https://github.com/JarodMica/rvc-python@782467ababe17698a4b5100aedfe16e69cebaa56'
RVC_PYTHON_FORK_SOURCE_FRAGMENT = 'github.com/jarodmica/rvc-python'
RVC_FAIRSEQ_WHEEL_URL_BY_PYTHON = {
    '3.10': 'https://huggingface.co/Jmica/rvc/resolve/main/fairseq-0.12.2-cp310-cp310-win_amd64.whl?download=true',
    '3.11': 'https://huggingface.co/Jmica/rvc/resolve/main/fairseq-0.12.4-cp311-cp311-win_amd64.whl?download=true',
}
RVC_TORCH_VERSION = '2.3.1'
RVC_TORCHVISION_VERSION = '0.18.1'
RVC_TORCHAUDIO_VERSION = '2.3.1'
RVC_NUMPY_SPEC = 'numpy<2'
RVC_TORCH_INDEX_URL = 'https://download.pytorch.org/whl/cu121'
RVC_REQUIRED_PACKAGE_SPECS = (
    'rvc-python',
    'fairseq',
    RVC_NUMPY_SPEC,
    f'torch=={RVC_TORCH_VERSION}',
    f'torchvision=={RVC_TORCHVISION_VERSION}',
    f'torchaudio=={RVC_TORCHAUDIO_VERSION}',
)
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
