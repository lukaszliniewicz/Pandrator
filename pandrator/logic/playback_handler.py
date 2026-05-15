import pygame
import logging
import time

class PlaybackHandler:
    def __init__(self):
        try:
            pygame.mixer.init()
            self._channel = pygame.mixer.Channel(0)
            self._sound = None
            self.is_paused = False
            self.is_playing = False
            logging.info("Pygame mixer initialized successfully.")
        except pygame.error as e:
            logging.error(f"Failed to initialize pygame mixer: {e}")
            self._channel = None
            self.is_playing = False
            self.is_paused = False

    def play(self, audio_path: str) -> bool:
        if not self._channel:
            logging.warning("Playback handler not initialized. Cannot play audio.")
            return False

        if self.is_playing:
            self.stop()

        try:
            self._sound = pygame.mixer.Sound(audio_path)
            self._channel.play(self._sound)
            self.is_playing = True
            self.is_paused = False
            return True
        except pygame.error as e:
            logging.error(f"Failed to play audio file {audio_path}: {e}")
            self._sound = None
            self.is_playing = False
            return False

    def stop(self):
        if not self._channel:
            return
            
        if self._channel.get_busy():
            self._channel.stop()

        if pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()
            pygame.mixer.music.unload()

        self.is_playing = False
        self.is_paused = False
        self._sound = None

    def toggle_pause(self):
        if not self._channel or not self.is_playing:
            return

        if self.is_paused:
            self._channel.unpause()
            self.is_paused = False
        else:
            self._channel.pause()
            self.is_paused = True
    
    def get_busy(self) -> bool:
        """Checks if audio is currently playing."""
        if not self._channel:
            return False
        return self._channel.get_busy()
        
    def check_if_finished(self):
        """Checks if the current sound has finished playing."""
        if self.is_playing and not self._channel.get_busy():
            self.is_playing = False
            self.is_paused = False
            self._sound = None
            return True
        return False

    def quit(self):
        if pygame.mixer.get_init():
            pygame.mixer.quit()
            logging.info("Pygame mixer quit.")
