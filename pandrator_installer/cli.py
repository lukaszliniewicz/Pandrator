"""Command-line and GUI application entry points."""

import argparse
import logging
import os
import sys

from .catalog import COMPONENTS, PACKAGING_COMPONENT_PATHS
from .service import HeadlessInstaller

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
            'rvc,whisperx,xtts_finetuning,chatterbox,chatterbox_cpu,magpie,magpie_cpu'
        ),
    )
    parser.add_argument(
        '--skip-pandrator',
        action='store_true',
        help='Do not select Pandrator core checkbox in headless mode.',
    )
    parser.add_argument(
        '--self-check',
        action='store_true',
        help='Validate packaged launcher imports and component metadata, then exit.',
    )
    return parser.parse_args(argv)

def run_headless_install_from_cli(args):
    if not args.workspace:
        raise RuntimeError('--workspace is required with --headless-install.')

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    workspace = os.path.abspath(os.path.expanduser(args.workspace))
    os.makedirs(workspace, exist_ok=True)

    installer = HeadlessInstaller(working_dir=workspace)

    try:
        components = parse_headless_components(args.components)
        installer.run_headless_install(
            components,
            install_pandrator=not args.skip_pandrator,
        )
    finally:
        installer.shutdown_apps()
        installer.shutdown_logging()

def run_gui_app():
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
        QPushButton {{
            background-color: {pastel_purple.name()};
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 4px;
        }}
        QPushButton:hover {{
            background-color: {pastel_purple_hover.name()};
        }}
        QPushButton:pressed {{
            background-color: {pastel_purple_pressed.name()};
        }}
        QPushButton:disabled {{
            background-color: #555555;
            color: #888888;
        }}
        QCheckBox {{
            spacing: 8px;
        }}
        QCheckBox:disabled {{
            color: #8A8A8A;
        }}
        QCheckBox::indicator {{
            width: 18px;
            height: 18px;
            border: 1px solid white;
            border-radius: 3px;
        }}
        QCheckBox::indicator:checked {{
            background-color: {pastel_purple.name()};
            border: 1px solid white;
        }}
        QCheckBox::indicator:unchecked {{
            background-color: transparent;
            border: 1px solid white;
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
            border: 1px solid #444444;
            border-radius: 4px;
            margin-top: 12px;
            padding-top: 15px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            subcontrol-position: top left;
            padding: 0 5px;
            color: {pastel_purple.name()};
        }}
        QTabWidget::pane {{
            border: 1px solid #444444;
            border-radius: 4px;
        }}
        QTabBar::tab {{
            background-color: #2D2D30;
            color: #FFFFFF;
            border: 1px solid #444444;
            border-bottom: none;
            border-top-left-radius: 4px;
            border-top-right-radius: 4px;
            padding: 8px 16px;
        }}
        QTabBar::tab:selected {{
            background-color: {pastel_purple.name()};
        }}
        QTabBar::tab:hover:!selected {{
            background-color: #3D3D40;
        }}
        QProgressBar {{
            border: 1px solid #444444;
            border-radius: 3px;
            text-align: center;
            height: 20px;
        }}
        QProgressBar::chunk {{
            background-color: {pastel_purple.name()};
            width: 10px;
        }}
    """)

    # Create and show the main window
    window = PandratorInstaller()
    window.show()

    return app.exec()


def run_self_check():
    required_components = {
        "xtts",
        "voxcpm",
        "fishs2",
        "voxtral",
        "kokoro",
        "silero",
        "whisperx",
        "xtts_finetuning",
        "rvc",
        "chatterbox",
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

    print(f"Pandrator installer self-check passed ({len(COMPONENTS)} component definitions).")
    return 0


def main(argv=None):
    cli_args = parse_launcher_cli_args(sys.argv[1:] if argv is None else argv)
    if cli_args.self_check:
        return run_self_check()
    if cli_args.headless_install:
        try:
            run_headless_install_from_cli(cli_args)
        except Exception as error:
            print(f"Headless installation failed: {error}")
            return 1
        return 0
    return run_gui_app()
