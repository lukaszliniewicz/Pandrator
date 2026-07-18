"""Command-line and GUI application entry points."""

import argparse
import logging
import os
import sys
import tempfile

from .catalog import COMPONENTS, PACKAGING_COMPONENT_PATHS
from .platforms import (
    normalized_machine,
    normalized_system,
    pixi_binary_name,
    pixi_manifest_platform,
    resolve_launcher_workspace,
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
        description="Pandrator installer GUI and headless automation entrypoint.",
    )
    parser.add_argument(
        '--headless-install',
        action='store_true',
        help='Run installation without showing the GUI.',
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
    parser.add_argument('--crispasr-engine', choices=('whisper-large-v3', 'parakeet-tdt-0.6b-v3'), default='whisper-large-v3')
    parser.add_argument('--crispasr-model-quantization', choices=('f16', 'q8_0', 'q5_0', 'q4_k'), default='f16')
    parser.add_argument('--qwen-backend', choices=('auto', 'cpu', 'cuda', 'vulkan', 'metal'), default='auto')
    parser.add_argument('--qwen-model-size', choices=('0.6b', '1.7b'), default='0.6b')
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
        help='Validate packaged launcher imports and component metadata, then exit.',
    )
    parser.add_argument(
        '--gui-smoke-check',
        action='store_true',
        help='Instantiate the installer GUI offscreen, then exit.',
    )
    parser.add_argument(
        '--tls-self-check',
        action='store_true',
        help='Verify packaged OpenSSL and CA certificates with an HTTPS request, then exit.',
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

def run_gui_app(args=None):
    # Import needed modules
    from PyQt6.QtGui import QColor, QPalette
    from PyQt6.QtWidgets import QApplication
    from .gui.main_window import PandratorInstaller

    # Set up application style
    app = QApplication(sys.argv)
    app.setStyle('Fusion')  # Use Fusion style for a modern look

    # Define pastel purple color
    pastel_purple = QColor('#7e57c2')  # Main button color
    pastel_purple_hover = QColor('#9575cd')  # Lighter for hover
    pastel_purple_pressed = QColor('#5e35b1')  # Darker for pressed state

    # Create dark palette
    dark_palette = QPalette()
    # Set colors using the QPalette.ColorRole enum
    dark_palette.setColor(QPalette.ColorRole.Window, QColor(45, 45, 48))
    dark_palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 255, 255))
    dark_palette.setColor(QPalette.ColorRole.Base, QColor(30, 30, 30))
    dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(45, 45, 48))
    dark_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(255, 255, 255))
    dark_palette.setColor(QPalette.ColorRole.ToolTipText, QColor(255, 255, 255))
    dark_palette.setColor(QPalette.ColorRole.Text, QColor(255, 255, 255))
    dark_palette.setColor(QPalette.ColorRole.Button, QColor(45, 45, 48))
    dark_palette.setColor(QPalette.ColorRole.ButtonText, QColor(255, 255, 255))
    dark_palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
    dark_palette.setColor(QPalette.ColorRole.Link, QColor(0, 155, 255))
    dark_palette.setColor(QPalette.ColorRole.Highlight, pastel_purple)  # Changed to match buttons
    dark_palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    app.setPalette(dark_palette)

    # Set stylesheet for custom styling with pastel purple buttons and clearer disabled states
    app.setStyleSheet(f"""
        QMainWindow {{
            background-color: #2D2D30;
        }}
        QWidget {{
            background-color: #2D2D30;
            color: #FFFFFF;
        }}
        QLabel,
        QCheckBox {{
            background-color: transparent;
        }}
        QScrollArea#installerScrollArea,
        QScrollArea#installerScrollArea > QWidget > QWidget {{
            background-color: #29292C;
            border: none;
        }}
        QLabel#titleLabel {{
            color: #F4F1FA;
        }}
        QLabel#introLabel {{
            color: #D0CDD7;
            padding: 2px 4px;
        }}
        QLabel#mutedLabel {{
            color: #AAA6B2;
            font-size: 11px;
        }}
        QLabel#voiceCapabilityBadge[supported="true"] {{
            background-color: #48405E;
            border: 1px solid #695B88;
            border-radius: 8px;
            color: #E2D8F3;
            font-size: 10px;
            font-weight: bold;
            padding: 3px 7px;
        }}
        QLabel#voiceCapabilityBadge[supported="false"] {{
            background-color: #2D2D31;
            border: 1px solid #3F3E44;
            border-radius: 8px;
            color: #737078;
            font-size: 10px;
            padding: 3px 7px;
        }}
        QLabel#backendRuntimeStatus {{
            background-color: #264A3D;
            border: 1px solid #3D8268;
            border-radius: 8px;
            color: #BDF2DA;
            font-size: 10px;
            font-weight: bold;
            padding: 3px 8px;
        }}
        QLabel#statusLabel {{
            background-color: #28282B;
            border: 1px solid #414147;
            border-radius: 7px;
            color: #D9D5E0;
            padding: 6px 10px;
        }}
        QFrame#optionCard {{
            background-color: #343438;
            border: 1px solid #48484F;
            border-radius: 11px;
        }}
        QFrame#optionCard:hover {{
            background-color: #37363B;
            border-color: #635A77;
        }}
        QFrame#optionCard[expanded="true"] {{
            background-color: #37363B;
            border-color: #766795;
        }}
        QFrame#optionCardSummary {{
            background-color: transparent;
            border: none;
        }}
        QFrame#optionCardDetails {{
            background-color: #303035;
            border: none;
            border-top: 1px solid #48474E;
            border-bottom-left-radius: 10px;
            border-bottom-right-radius: 10px;
        }}
        QLabel#optionCardEyebrow {{
            color: #A594C3;
            font-size: 9px;
            font-weight: bold;
        }}
        QLabel#optionCardLanguages,
        QLabel#optionCardModels {{
            color: #DDD9E2;
            font-size: 11px;
        }}
        QToolButton#optionCardChevron {{
            background-color: transparent;
            border: none;
            border-radius: 7px;
            color: #B4A8C8;
            min-width: 24px;
            min-height: 24px;
            padding: 2px;
        }}
        QToolButton#optionCardChevron:hover {{
            background-color: #45404E;
        }}
        QFrame#installLocationRow {{
            background-color: #303034;
            border: 1px solid #45454B;
            border-radius: 9px;
        }}
        QLabel#installLocationHeading {{
            color: #9F90B9;
            font-size: 9px;
            font-weight: bold;
        }}
        QLabel#installLocationPath {{
            color: #D5D1D9;
            font-size: 11px;
            padding-left: 6px;
        }}
        QPushButton#linkButton {{
            background-color: transparent;
            border: none;
            color: #C9B4ED;
            font-weight: bold;
            padding: 4px 7px;
        }}
        QPushButton#linkButton:hover {{
            background-color: #3C3745;
            border: none;
            color: #FFFFFF;
        }}
        QPushButton {{
            background-color: #49494F;
            color: white;
            border: 1px solid #5A5A62;
            padding: 8px 16px;
            border-radius: 6px;
        }}
        QPushButton:hover {{
            background-color: #585860;
            border-color: #6B6B75;
        }}
        QPushButton:pressed {{
            background-color: #414147;
        }}
        QPushButton:disabled {{
            background-color: #38383C;
            border-color: #414147;
            color: #77777E;
        }}
        QPushButton#primaryButton,
        QPushButton#installButton {{
            background-color: {pastel_purple.name()};
            border-color: {pastel_purple.name()};
            font-weight: bold;
        }}
        QPushButton#primaryButton:hover,
        QPushButton#installButton:hover {{
            background-color: {pastel_purple_hover.name()};
            border-color: {pastel_purple_hover.name()};
        }}
        QPushButton#primaryButton:pressed,
        QPushButton#installButton:pressed {{
            background-color: {pastel_purple_pressed.name()};
            border-color: {pastel_purple_pressed.name()};
        }}
        QPushButton#installButton:disabled {{
            background-color: #44444A;
            border-color: #52525A;
            color: #FFFFFF;
        }}
        QPushButton#githubButton {{
            background-color: transparent;
            border: 1px solid transparent;
            color: #D8C9F1;
            padding: 6px 9px;
        }}
        QPushButton#githubButton:hover {{
            background-color: #3A3543;
            border-color: #554A68;
            color: #FFFFFF;
        }}
        QPushButton#githubButton:pressed {{
            background-color: #302B38;
        }}
        QCheckBox {{
            spacing: 8px;
        }}
        QCheckBox:disabled {{
            color: #8A8A8A;
        }}
        QCheckBox::indicator {{
            width: 16px;
            height: 16px;
            border: 1px solid #85858D;
            border-radius: 4px;
        }}
        QCheckBox::indicator:checked {{
            background-color: {pastel_purple.name()};
            border: 1px solid {pastel_purple_hover.name()};
        }}
        QCheckBox::indicator:unchecked {{
            background-color: #29292D;
            border: 1px solid #85858D;
        }}
        QCheckBox::indicator:disabled {{
            border: 1px solid #7A7A7A;
            background-color: transparent;
        }}
        QCheckBox::indicator:checked:disabled {{
            background-color: #544372;
            border: 1px solid #7A7A7A;
        }}
        QCheckBox::indicator:unchecked:disabled {{
            background-color: transparent;
            border: 1px solid #7A7A7A;
        }}
        QGroupBox {{
            background-color: #303034;
            border: 1px solid #45454B;
            border-radius: 9px;
            font-weight: bold;
            margin-top: 14px;
            padding-top: 16px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            subcontrol-position: top left;
            padding: 0 7px;
            color: #D6C8EE;
        }}
        QTabWidget::pane {{
            background-color: #29292C;
            border: 1px solid #414147;
            border-radius: 8px;
        }}
        QTabBar::tab {{
            background-color: #2D2D30;
            color: #FFFFFF;
            border: 1px solid #414147;
            border-bottom: none;
            border-top-left-radius: 7px;
            border-top-right-radius: 7px;
            padding: 9px 20px;
        }}
        QTabBar::tab:selected {{
            background-color: {pastel_purple.name()};
        }}
        QTabBar::tab:hover:!selected {{
            background-color: #3D3D40;
        }}
        QProgressBar {{
            background-color: #29292D;
            border: 1px solid #414147;
            border-radius: 4px;
            text-align: center;
            height: 7px;
        }}
        QProgressBar::chunk {{
            background-color: {pastel_purple.name()};
            border-radius: 3px;
        }}
        QScrollBar:vertical {{
            background: transparent;
            border: none;
            margin: 2px;
            width: 10px;
        }}
        QScrollBar:horizontal {{
            background: transparent;
            border: none;
            height: 10px;
            margin: 2px;
        }}
        QScrollBar::handle:vertical {{
            background-color: #5B5B63;
            border-radius: 4px;
            min-height: 32px;
        }}
        QScrollBar::handle:horizontal {{
            background-color: #5B5B63;
            border-radius: 4px;
            min-width: 32px;
        }}
        QScrollBar::handle:hover {{
            background-color: #777780;
        }}
        QScrollBar::add-line,
        QScrollBar::sub-line {{
            background: transparent;
            border: none;
            height: 0px;
            width: 0px;
        }}
        QScrollBar::add-page,
        QScrollBar::sub-page {{
            background: transparent;
        }}
    """)

    # Create and show the main window
    workspace = resolve_launcher_workspace(args.workspace if args else None)
    window = PandratorInstaller(working_dir=workspace)
    window.show()

    return app.exec()


def run_gui_smoke_check(args=None):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt6.QtWidgets import QApplication
    from .gui.main_window import PandratorInstaller

    app = QApplication.instance() or QApplication(["pandrator-installer-gui-smoke"])
    with tempfile.TemporaryDirectory(prefix="pandrator-gui-smoke-") as temp_dir:
        workspace_value = args.workspace if args and args.workspace else temp_dir
        workspace = resolve_launcher_workspace(workspace_value)
        window = PandratorInstaller(
            working_dir=workspace,
            skip_space_warning=True,
        )
        window.show()
        app.processEvents()
        window.close()
        window.shutdown_apps()

    print("Pandrator installer GUI smoke-check passed.")
    return 0


def run_self_check():
    import ssl

    import certifi

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
    """Verify that the packaged runtime can complete a trusted TLS request."""

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
    if cli_args.gui_smoke_check:
        return run_gui_smoke_check(cli_args)
    if cli_args.headless_install:
        try:
            run_headless_install_from_cli(cli_args)
        except Exception as error:
            print(f"Headless installation failed: {error}")
            return 1
        return 0
    return run_gui_app(cli_args)
