"""Legacy headless installer compatibility entry points."""

import argparse
import logging
import os
import sys

from .catalog import COMPONENTS, PACKAGING_COMPONENT_PATHS
from .models import DEFAULT_QWEN_MODEL_SIZE
from .platforms import (
    normalized_machine,
    normalized_system,
    pixi_binary_name,
    pixi_manifest_platform,
)
from .service import HeadlessInstaller
from .subprocess_env import external_subprocess_environment


def parse_headless_components(raw_components):
    parsed_components = set()
    for raw_component in str(raw_components or '').split(','):
        normalized = raw_component.strip().lower().replace('-', '_')
        if normalized:
            parsed_components.add(normalized)
    return parsed_components


def parse_launcher_cli_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Pandrator headless installer compatibility entry point.",
    )
    parser.add_argument(
        '--headless-install',
        action='store_true',
        help='Run legacy source preparation without a user interface.',
    )
    parser.add_argument(
        '--workspace',
        default=None,
        help='Directory where the installer should create/use the Pandrator folder.',
    )
    parser.add_argument(
        '--components',
        default='',
        help=(
            'Comma-separated component list for headless mode: '
            'xtts,xtts_cpu,voxcpm,fishs2,silero,voxtral,kokoro,kokoro_cpu,'
            'rvc,rvc_cpu,crispasr,xtts_finetuning,chatterbox,chatterbox_cpu,'
            'kobold_qwen,kobold_qwen_cpu,magpie,magpie_cpu'
        ),
    )
    parser.add_argument(
        '--crispasr-backend',
        choices=('auto', 'cpu', 'cuda', 'vulkan', 'metal'),
        default='auto',
        help='CrispASR runtime variant; auto chooses the best detected backend.',
    )
    parser.add_argument(
        '--crispasr-engine',
        choices=('whisper-large-v3', 'parakeet-tdt-0.6b-v3', 'moss-transcribe-diarize-0.9b'),
        default='whisper-large-v3',
    )
    parser.add_argument('--crispasr-model-quantization', choices=('f16', 'q8_0', 'q5_0', 'q4_k'))
    parser.add_argument('--qwen-backend', choices=('auto', 'cpu', 'cuda', 'vulkan', 'metal'), default='auto')
    parser.add_argument(
        '--qwen-model-size',
        choices=('0.6b', '1.7b'),
        default=DEFAULT_QWEN_MODEL_SIZE,
    )
    parser.add_argument('--qwen-quantization', choices=('f16', 'q8_0'), default='f16')
    parser.add_argument(
        '--qwen-initial-model',
        choices=('base', 'customvoice', 'both'),
        default='base',
        help='Qwen3 TTS model variant(s) to download; CustomVoice and both require 1.7B.',
    )
    parser.add_argument(
        '--skip-pandrator',
        action='store_true',
        help=(
            'Do not explicitly select the Pandrator core checkbox in headless mode. '
            'The shared core runtime may still be prepared when a fresh install requires it.'
        ),
    )
    parser.add_argument(
        '--self-check',
        action='store_true',
        help='Validate compatibility entry point imports and component metadata, then exit.',
    )
    parser.add_argument(
        '--tls-self-check',
        action='store_true',
        help='Verify OpenSSL and CA certificates with an HTTPS request, then exit.',
    )
    return parser.parse_args(argv)


def run_headless_install_from_cli(args):
    if not args.workspace:
        raise RuntimeError('--workspace is required with --headless-install.')

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    workspace = os.path.abspath(os.path.expanduser(args.workspace))
    os.makedirs(workspace, exist_ok=True)

    installer = HeadlessInstaller(working_dir=workspace)

    completed = False
    try:
        components = parse_headless_components(args.components)
        installer.run_headless_install(
            components,
            install_pandrator=not args.skip_pandrator,
            crispasr_backend=args.crispasr_backend,
            crispasr_engine=args.crispasr_engine,
            crispasr_model_quantization=args.crispasr_model_quantization,
            kobold_qwen_backend=args.qwen_backend,
            kobold_qwen_model_size=args.qwen_model_size,
            kobold_qwen_quantization=args.qwen_quantization,
            kobold_qwen_initial_model=args.qwen_initial_model,
        )
        completed = True
    finally:
        if not completed:
            installer.shutdown_apps()
        installer.shutdown_logging()


def run_self_check():
    import bz2
    import ctypes
    import lzma
    import sqlite3
    import ssl
    from xml.parsers import expat

    import certifi

    if ctypes.sizeof(ctypes.c_void_p) not in {4, 8}:
        raise RuntimeError("Installer self-check failed: ctypes runtime is unavailable.")

    if not bz2.compress(b"Pandrator") or not lzma.compress(b"Pandrator"):
        raise RuntimeError("Installer self-check failed: compression runtimes are unavailable.")
    if expat.ParserCreate() is None:
        raise RuntimeError("Installer self-check failed: XML runtime is unavailable.")
    with sqlite3.connect(":memory:") as database:
        if database.execute("SELECT 1").fetchone() != (1,):
            raise RuntimeError("Installer self-check failed: SQLite runtime is unavailable.")

    ssl_context = ssl.create_default_context(cafile=certifi.where())
    if not ssl_context.get_ca_certs():
        raise RuntimeError("Installer self-check failed: TLS trust store is empty.")

    required_components = {
        "xtts",
        "voxcpm",
        "fishs2",
        "voxtral",
        "kokoro",
        "silero",
        "crispasr",
        "xtts_finetuning",
        "rvc",
        "chatterbox",
        "kobold_qwen",
        "magpie",
    }
    missing_components = sorted(required_components.difference(COMPONENTS))
    missing_packaging_paths = sorted(
        key
        for key in required_components
        if COMPONENTS[key].paths and key not in PACKAGING_COMPONENT_PATHS
    )
    if missing_components or missing_packaging_paths:
        raise RuntimeError(
            "Installer self-check failed. "
            f"Missing components: {missing_components}; "
            f"missing packaging paths: {missing_packaging_paths}"
        )

    external_environment = external_subprocess_environment()
    if sys.platform.startswith("linux"):
        if "LD_LIBRARY_PATH_ORIG" in external_environment:
            raise RuntimeError("Installer self-check failed: private library backup leaked to child processes.")
        bundle_root = str(getattr(sys, "_MEIPASS", "") or "")
        child_library_path = external_environment.get("LD_LIBRARY_PATH", "")
        if bundle_root and child_library_path:
            normalized_bundle_root = os.path.normcase(os.path.abspath(bundle_root))
            leaked_entries = [
                entry
                for entry in child_library_path.split(os.pathsep)
                if entry
                and (
                    os.path.normcase(os.path.abspath(entry)) == normalized_bundle_root
                    or os.path.normcase(os.path.abspath(entry)).startswith(normalized_bundle_root + os.sep)
                )
            ]
            if leaked_entries:
                raise RuntimeError(
                    "Installer self-check failed: private libraries would leak to child processes."
                )

    print(
        "Pandrator installer self-check passed "
        f"({len(COMPONENTS)} component definitions; "
        f"platform={normalized_system()}-{normalized_machine()}; "
        f"pixi={pixi_binary_name()}; "
        f"manifest={pixi_manifest_platform()}; "
        f"openssl={ssl.OPENSSL_VERSION})."
    )
    return 0


def run_tls_self_check(url="https://github.com/"):
    """Verify that the compatibility runtime can complete a trusted TLS request."""

    import ssl
    import urllib.request

    import certifi

    ssl_context = ssl.create_default_context(cafile=certifi.where())
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "PandratorInstaller-TLS-Self-Check"},
        method="HEAD",
    )
    with urllib.request.urlopen(request, context=ssl_context, timeout=20) as response:
        status = int(getattr(response, "status", response.getcode()))
    if not 200 <= status < 400:
        raise RuntimeError(f"Installer TLS self-check failed with HTTP status {status}.")

    print(
        "Pandrator installer TLS self-check passed "
        f"(url={url}; status={status}; openssl={ssl.OPENSSL_VERSION})."
    )
    return 0


def main(argv=None):
    raw_args = sys.argv[1:] if argv is None else list(argv)
    if any(item in {"list", "probe", "plan", "install", "update", "repair", "launch", "service", "stop", "uninstall"} for item in raw_args):
        from .lifecycle import main as lifecycle_main

        return lifecycle_main(raw_args)
    cli_args = parse_launcher_cli_args(raw_args)
    if cli_args.self_check:
        return run_self_check()
    if cli_args.tls_self_check:
        return run_tls_self_check()
    if cli_args.headless_install:
        try:
            run_headless_install_from_cli(cli_args)
        except Exception as error:
            print(f"Headless installation failed: {error}")
            return 1
        return 0

    print(
        "No installer command specified. Use --headless-install for legacy source preparation "
        "or pandrator-installer --help for lifecycle commands.",
        file=sys.stderr,
    )
    return 2
