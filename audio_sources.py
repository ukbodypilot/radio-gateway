#!/usr/bin/env python3
"""Audio source and sink classes for radio-gateway.

The classes live in the ``sources`` package, one module per family:

    sources/base.py           AudioSource, bgm_files, bgm_path
    sources/playback.py       FilePlaybackSource, LoopPlaybackSource
    sources/network.py        EchoLinkSource, RemoteAudioServer, RemoteAudioSource,
                              LinkAudioSource, NetworkAnnouncementSource, MumbleSource
    sources/web.py            WebMicSource, WebMonitorSource
    sources/stream_output.py  StreamOutputSource
    sources/bgm.py            BGMSource, AnnouncerSource

This module re-exports all of them so ``from audio_sources import X`` keeps
working.
"""

from sources.base import AudioSource, bgm_files, bgm_path
from sources.playback import FilePlaybackSource, LoopPlaybackSource
from sources.network import (
    EchoLinkSource, RemoteAudioServer, RemoteAudioSource,
    LinkAudioSource, NetworkAnnouncementSource, MumbleSource,
)
from sources.web import WebMicSource, WebMonitorSource
from sources.stream_output import StreamOutputSource
from sources.bgm import BGMSource, AnnouncerSource

__all__ = [
    'AudioSource', 'bgm_files', 'bgm_path',
    'FilePlaybackSource', 'LoopPlaybackSource',
    'EchoLinkSource', 'RemoteAudioServer', 'RemoteAudioSource',
    'LinkAudioSource', 'NetworkAnnouncementSource', 'MumbleSource',
    'WebMicSource', 'WebMonitorSource',
    'StreamOutputSource',
    'BGMSource', 'AnnouncerSource',
]
