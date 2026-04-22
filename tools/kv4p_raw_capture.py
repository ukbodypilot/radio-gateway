#!/usr/bin/env python3
"""Capture raw Opus frames from KV4P HT and decode to WAV.

Bypasses the entire gateway pipeline — records directly from the radio
serial port to isolate whether audio artifacts originate in the radio
or in the gateway's processing.
"""
import sys, time, struct, wave, os

sys.path.insert(0, os.path.expanduser('~/kv4p-ht-python'))
for mod in list(sys.modules):
    if mod.startswith('kv4p'):
        del sys.modules[mod]

from kv4p import KV4PRadio, GroupConfig
import opuslib

PORT = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyUSB1'
DURATION = int(sys.argv[2]) if len(sys.argv) > 2 else 15
_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_RAW = os.path.join(_HERE, 'kv4p_raw.wav')
OUT_INFO = os.path.join(_HERE, 'kv4p_raw_info.txt')

radio = KV4PRadio(PORT)
dec = opuslib.Decoder(48000, 1)

frames = []  # list of (timestamp, opus_bytes, pcm_bytes)
t0 = [0]

def on_audio(opus_data):
    if t0[0] == 0:
        t0[0] = time.monotonic()
    ts = time.monotonic() - t0[0]
    try:
        pcm = dec.decode(opus_data, 1920)
        frames.append((ts, opus_data, pcm))
    except Exception as e:
        frames.append((ts, opus_data, None))

radio.on_rx_audio = on_audio

try:
    print(f"Connecting to {PORT}...")
    ver = radio.open(handshake_timeout=10)
    print(f"Connected: fw v{ver.firmware_version}")
    radio.tune(GroupConfig(tx_freq=146.520, rx_freq=146.520, squelch=2))
    print(f"Tuned to 146.520 MHz, squelch=2")
    print(f"\n>>> TRANSMIT NOW — recording for {DURATION} seconds <<<\n")
    time.sleep(DURATION)
except KeyboardInterrupt:
    pass

# Write raw decoded PCM to WAV (no resampling, no processing)
pcm_data = b''
for ts, opus, pcm in frames:
    if pcm:
        pcm_data += pcm

with wave.open(OUT_RAW, 'w') as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(48000)
    wf.writeframes(pcm_data)

# Write frame info
with open(OUT_INFO, 'w') as f:
    f.write(f"Frames: {len(frames)}\n")
    f.write(f"Duration: {frames[-1][0]:.3f}s\n")
    f.write(f"FPS: {len(frames)/frames[-1][0]:.2f}\n")
    f.write(f"PCM bytes: {len(pcm_data)}\n")
    f.write(f"PCM duration: {len(pcm_data)/96000:.3f}s\n\n")

    # Frame timing
    intervals = [frames[i+1][0] - frames[i][0] for i in range(len(frames)-1)]
    f.write(f"Frame intervals:\n")
    f.write(f"  mean: {sum(intervals)/len(intervals)*1000:.2f}ms\n")
    f.write(f"  min:  {min(intervals)*1000:.2f}ms\n")
    f.write(f"  max:  {max(intervals)*1000:.2f}ms\n\n")

    # Opus frame sizes
    sizes = [len(opus) for ts, opus, pcm in frames]
    f.write(f"Opus frame sizes:\n")
    f.write(f"  min: {min(sizes)}B  max: {max(sizes)}B  mean: {sum(sizes)/len(sizes):.1f}B\n")

    # Per-frame detail
    f.write(f"\n{'frame':>6} {'time':>8} {'dt_ms':>7} {'opus_B':>7} {'pcm_B':>7}\n")
    for i, (ts, opus, pcm) in enumerate(frames[:200]):
        dt = (frames[i][0] - frames[i-1][0]) * 1000 if i > 0 else 0
        f.write(f"{i:6d} {ts:8.3f} {dt:7.2f} {len(opus):7d} {len(pcm) if pcm else 0:7d}\n")

print(f"\nCaptured {len(frames)} frames in {frames[-1][0]:.1f}s ({len(frames)/frames[-1][0]:.1f} fps)")
print(f"Raw WAV: {OUT_RAW} ({len(pcm_data)/96000:.1f}s)")
print(f"Info: {OUT_INFO}")
os._exit(0)  # Force exit — radio.close() hangs due to reader thread
