import sys
import argparse
import logging
import os
import datetime
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer

from pandrator.app_logic import AppLogic
from pandrator.gui.main_window import MainWindow

def setup_logging() -> str:
    """
    Configures logging to file and console.
    Returns the path to the log file.
    """
    logs_dir = "logs"
    os.makedirs(logs_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file_path = os.path.join(logs_dir, f"pandrator_{timestamp}.log")

    # Get the root logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Clear existing handlers
    if logger.hasHandlers():
        logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # File handler
    file_handler = logging.FileHandler(log_file_path, mode='w', encoding='utf-8')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Stream handler (console)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    
    logging.info(f"Logging initialized. Log file: {log_file_path}")
    return log_file_path

def main():
    """Application entry point."""
    log_file_path = setup_logging()
    
    parser = argparse.ArgumentParser(description="Pandrator")
    parser.add_argument("-connect", "--connect", action="store_true", help="Connect to a TTS service on launch")
    parser.add_argument("-xtts", "--xtts", action="store_true", help="Connect to XTTS")
    parser.add_argument("-voxtral", "--voxtral", action="store_true", help="Connect to Voxtral")
    parser.add_argument("-kokoro", "--kokoro", action="store_true", help="Connect to Kokoro")
    parser.add_argument("-silero", "--silero", action="store_true", help="Connect to Silero")
    args = parser.parse_args()
    logging.info(f"Command line arguments: {args}")

    app = QApplication(sys.argv)
    try:
        with open("style.qss", "r") as f:
            custom_stylesheet = f.read()
        app.setStyleSheet(custom_stylesheet)
    except FileNotFoundError:
        logging.warning("style.qss not found. Loading default Qt style.")

    logic = AppLogic()
    logic.set_log_file_path(log_file_path)
    app.aboutToQuit.connect(logic.shutdown)
    main_window = MainWindow(logic)

    auto_connect_service = None
    if args.connect:
        if args.xtts:
            auto_connect_service = "XTTS"
        elif args.voxtral:
            auto_connect_service = "Voxtral"
        elif args.kokoro:
            auto_connect_service = "Kokoro"
        elif args.silero:
            auto_connect_service = "Silero"

    if auto_connect_service:
        logic.state.tts.service = auto_connect_service
        if auto_connect_service == "XTTS":
            logic.state.tts.use_external_server = False
            logic.state.tts.external_server_url = "http://127.0.0.1:8020"
        elif auto_connect_service == "Voxtral":
            logic.state.tts.use_external_server = False
            logic.state.tts.external_server_url = "http://127.0.0.1:8000"
        elif auto_connect_service == "Kokoro":
            logic.state.tts.use_external_server = False
            logic.state.tts.external_server_url = "http://127.0.0.1:8880"
        logic.state_changed.emit()

    main_window.show()

    if auto_connect_service:
        logging.info("Auto-connecting to %s on launch", auto_connect_service)
        QTimer.singleShot(800, logic.connect_tts_server)

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
