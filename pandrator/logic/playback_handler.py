import logging
import threading

_PYGAME_MODULE = None
_PYGAME_IMPORT_ATTEMPTED = False


def _get_pygame_module():
    global _PYGAME_MODULE, _PYGAME_IMPORT_ATTEMPTED
    if not _PYGAME_IMPORT_ATTEMPTED:
        _PYGAME_IMPORT_ATTEMPTED = True
        try:
            import pygame as pygame_module
        except Exception as e:
            logging.warning("Pygame is not available: %s", e)
        else:
            _PYGAME_MODULE = pygame_module

    return _PYGAME_MODULE

class PlaybackHandler:
    def __init__(self):
        self._lock = threading.RLock()
        self._channel = None
        self._sound = None
        self._pygame = None
        self.is_paused = False
        self.is_playing = False

    def _ensure_initialized(self) -> bool:
        if self._is_available():
            return True

        pygame_module = _get_pygame_module()
        if pygame_module is None:
            return False

        self._pygame = pygame_module
        try:
            if self._pygame.mixer.get_init() is None:
                self._pygame.mixer.init()
            self._channel = self._pygame.mixer.Channel(0)
            logging.info("Pygame mixer initialized successfully.")
            return True
        except Exception as e:
            logging.error("Failed to initialize pygame mixer: %s", e)
            self._channel = None
            return False

    def _is_available(self) -> bool:
        return (
            self._pygame is not None
            and self._channel is not None
            and self._pygame.mixer.get_init() is not None
        )

    def play(self, audio_path: str) -> bool:
        with self._lock:
            if not self._ensure_initialized():
                logging.warning("Playback handler not initialized. Cannot play audio.")
                return False

            if self.is_playing:
                self.stop()

            try:
                self._sound = self._pygame.mixer.Sound(audio_path)
                self._channel.play(self._sound)
                self.is_playing = True
                self.is_paused = False
                return True
            except (FileNotFoundError, OSError, Exception) as e:
                logging.error(f"Failed to play audio file {audio_path}: {e}")
                self._sound = None
                self.is_playing = False
                self.is_paused = False
                return False

    def stop(self):
        with self._lock:
            try:
                if self._is_available() and self._channel.get_busy():
                    self._channel.stop()
            except Exception as e:
                logging.warning("Failed to stop playback channel cleanly: %s", e)
            finally:
                self.is_playing = False
                self.is_paused = False
                self._sound = None

    def toggle_pause(self):
        with self._lock:
            if not self._is_available() or not self.is_playing:
                return

            try:
                if self.is_paused:
                    self._channel.unpause()
                    self.is_paused = False
                else:
                    self._channel.pause()
                    self.is_paused = True
            except Exception as e:
                logging.warning("Failed to toggle pause state: %s", e)
                self.is_playing = False
                self.is_paused = False
                self._sound = None
    
    def get_busy(self) -> bool:
        """Checks if audio is currently playing."""
        with self._lock:
            if not self._is_available():
                return False

            try:
                return self._channel.get_busy()
            except Exception as e:
                logging.warning("Could not query playback channel state: %s", e)
                self.is_playing = False
                self.is_paused = False
                self._sound = None
                return False
        
    def check_if_finished(self):
        """Checks if the current sound has finished playing."""
        with self._lock:
            if not self.is_playing:
                return False

            if not self._is_available():
                self.is_playing = False
                self.is_paused = False
                self._sound = None
                return True

            try:
                if self._channel.get_busy():
                    return False
            except Exception as e:
                logging.warning("Could not check playback completion: %s", e)

            self.is_playing = False
            self.is_paused = False
            self._sound = None
            return True

    def quit(self):
        with self._lock:
            self.stop()
            if self._pygame is None:
                self._channel = None
                return

            if self._pygame.mixer.get_init() is None:
                self._channel = None
                return

            try:
                self._pygame.mixer.quit()
                logging.info("Pygame mixer quit.")
            except Exception as e:
                logging.warning("Failed to quit pygame mixer cleanly: %s", e)
            finally:
                self._channel = None
