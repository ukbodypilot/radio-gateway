#!/usr/bin/env python3
"""RemoteAudioSource (port 9602) with a silence-suppressing sender.

The Windows client may stop streaming when nothing is live and send only a
tiny keepalive ~1/s. Covers: link survives the gap, keepalives are not audio,
a burst after idle is re-primed (no clipped start), a burst shorter than the
prefill still plays out (tail padded, not stranded), and the meter decays.
"""
import os
import socket
import struct
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _hwstubs  # noqa: E402,F401
import numpy as np  # noqa: E402
from sources.network import RemoteAudioSource  # noqa: E402

FAILS = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        FAILS.append(name)


CB = 4800
cfg = types.SimpleNamespace(
    REMOTE_AUDIO_PRIORITY=2, REMOTE_AUDIO_DUCK=False, AUDIO_CHUNK_SIZE=2400,
    REMOTE_AUDIO_JITTER_PREFILL=4, REMOTE_AUDIO_DISPLAY_GAIN=1.0,
    REMOTE_AUDIO_AUDIO_BOOST=1.0, REMOTE_AUDIO_PORT=0, VERBOSE_LOGGING=False)
gw = types.SimpleNamespace(tx_muted=False, rx_muted=False)
s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()
src = RemoteAudioSource(cfg, gw)
src.IDLE_UNPRIME_S, src.BURST_FLUSH_S = 0.3, 0.1
src.setup_audio(port_override=port)

c = socket.create_connection(('127.0.0.1', port))
def send(payload): c.sendall(struct.pack('>I', len(payload)) + payload)
loud = (np.full(2400, 8000, dtype=np.int16)).tobytes()
def drain():
    n = 0
    while src.get_audio(2400)[0] is not None:
        n += 1
    return n

time.sleep(0.2)
check("connected", src.server_connected)

# keepalives alone: not audio, not queued, link stays up
for _ in range(3):
    send(b'\x00' * 32); time.sleep(0.05)
check("keepalive not queued", src._chunk_queue.empty() and src.keepalives == 3)
check("keepalive yields no audio", src.get_audio(2400)[0] is None)

# burst of 10 chunks (> prefill 4): all delivered, nothing dropped
for _ in range(10): send(loud)
time.sleep(0.2)
check("burst delivered in full", drain() == 10)
check("meter nonzero after audio", src.audio_level > 0)

# idle: meter decays, re-primes
for _ in range(12):
    src.get_audio(2400)
time.sleep(0.4); src.get_audio(2400)
check("meter decays when starved", src.audio_level < 5)
check("un-primed after idle", src._jitter_primed is False)

# resume: audio held until prefill reached (cushion), then flows without loss
send(loud); send(loud)
time.sleep(0.03)
check("holds below prefill while burst still arriving", src.get_audio(2400)[0] is None)
send(loud); send(loud); time.sleep(0.05)
check("primes at prefill", drain() == 4)

# short burst (< prefill): must still play out after it stops, incl. tail
time.sleep(0.4); src.get_audio(2400)
send(loud); send(loud); send(loud[:1000] + b'')  # partial tail (1000 B)
time.sleep(0.25)
check("short burst + tail flushed", drain() == 3)

# link still alive after >timeout-ish quiet with keepalives
send(b'\x00' * 32)
check("still connected", src.server_connected)
c.close(); time.sleep(0.2)
check("disconnect detected", not src.server_connected)
src.cleanup()
print("FAILED: %s" % FAILS if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
