#!/usr/bin/env bash
# Build rtl_airband from upstream with the radio-gateway patch and install it
# to /usr/local/bin, which is ahead of /usr/bin on the gateway service's PATH.
#
#   tools/rtl_airband/build.sh            # build + install
#   tools/rtl_airband/build.sh --no-install
#   tools/rtl_airband/build.sh --check    # exit 0 if the patched build is installed
#
# Extra CMake flags (e.g. -DPLATFORM=rpiv2 on a Pi) via RTL_AIRBAND_CMAKE_FLAGS.
# scripts/install.sh runs this in its SDR step.
#
# The patch (0001-radio-gateway-fixes.patch) adds, against upstream 040caa6:
#   - pulse outputs: `rate_match` (PI loop on the stream latency; adds or drops
#     one sample at a time, spread over a batch with a windowed-sinc stretch).
#     The RSPduo's real sample rate is up to +/-160 ppm off and differs at every
#     start, so without it a continuous stream drains into an underflow (43 ms
#     hole) every few minutes. Upstream PR: rtl-airband/RTLSDR-Airband#586.
#   - pulse outputs: optional `buffer_ms` (explicit target latency; 0 = server
#     default). The plugin sets 250: audio is written in 125 ms blocks and the
#     PipeWire default (~145 ms) left ~20 ms of slack before each write.
#   - demodulate(): wait on a condition variable signalled by the input thread
#     instead of a blind SLEEP(10) poll (up to 10 ms of jitter per batch).
#   - stats file: pulse_underflow_count{stream} counter, pulse_latency_ms{stream}
#     gauge (buffered audio before the last write), and written via
#     tmp + rename() so a reader never sees a half-written file.
#   - "buffer overflow" warning via log() instead of cerr, so a daemonised
#     rtl_airband reports it to syslog.
#
# Remove /usr/local/bin/rtl_airband to fall back to the distro package.
set -euo pipefail

TARGET=/usr/local/bin/rtl_airband
# "rate_match" is a config key only the patched build knows
if [[ "${1:-}" == "--check" ]]; then
    [[ -x "$TARGET" ]] && grep -aq 'rate_match' "$TARGET"
    exit $?
fi

UPSTREAM=https://github.com/rtl-airband/RTLSDR-Airband.git
COMMIT=040caa693207e7fca0ef9fb2bc1048ea71bdc3f8
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git clone -q "$UPSTREAM" "$WORK/src"
git -C "$WORK/src" checkout -q "$COMMIT"
git -C "$WORK/src" apply "$HERE/0001-radio-gateway-fixes.patch"
# upstream CMake runs `git describe` in the CURRENT directory for the version
cd "$WORK/src"
cmake -S "$WORK/src" -B "$WORK/build" -DCMAKE_BUILD_TYPE=Release \
      -DNFM=ON -DRTLSDR=ON -DSOAPYSDR=ON -DPULSEAUDIO=ON ${RTL_AIRBAND_CMAKE_FLAGS:-} >/dev/null
make -C "$WORK/build" -j"$(nproc)" >/dev/null
"$WORK/build/src/rtl_airband" -v

if [[ "${1:-}" != "--no-install" ]]; then
    if [[ $EUID -eq 0 ]]; then
        install -m 0755 "$WORK/build/src/rtl_airband" "$TARGET"
    else
        sudo install -m 0755 "$WORK/build/src/rtl_airband" "$TARGET"
    fi
    echo "installed $TARGET (restart rtl_airband to use it)"
fi
