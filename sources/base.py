"""Base class for audio sources, plus the BGM file-list helpers that
both the soundboard and the BGM player need."""

import os


def bgm_files(config):
    """[(slot, name_or_path), ...] from BGM_FILES. 1-based.

    Module-level because two classes need it: BGMSource plays these, and
    FilePlaybackSource must keep them out of the numbered soundboard slots.
    """
    raw = str(getattr(config, 'BGM_FILES', '') or '').strip()
    names = ([n.strip() for n in raw.replace('\n', ',').split(',') if n.strip()]
             if raw else ['bgm1.mp3', 'bgm2.mp3', 'bgm3.mp3'])
    return list(enumerate(names, 1))


def bgm_path(config, name, audio_dir):
    return name if os.path.isabs(name) else os.path.join(audio_dir, name)


class AudioSource:
    """Base class for all audio sources"""
    def __init__(self, name, config):
        self.name = name
        self.config = config
        self.enabled = True
        self.priority = 0  # Lower = higher priority
        self.volume = 1.0
        self.ptt_control = True  # Can this source trigger PTT?
        
    def initialize(self):
        """Initialize the audio source. Return True on success."""
        return True
    
    def cleanup(self):
        """Clean up resources"""
        pass
    
    def get_audio(self, chunk_size):
        """
        Get audio chunk from this source.
        Returns: (audio_bytes, should_trigger_ptt)
        audio_bytes: PCM audio data or None
        should_trigger_ptt: True if this audio should key PTT
        """
        return None, False
    
    def is_active(self):
        """Return True if source currently has audio to transmit"""
        return False
    
    def get_status(self):
        """Return status string for display"""
        return f"{self.name}: {'ON' if self.enabled else 'OFF'}"
