#!/bin/bash
# Run the gateway from its own virtualenv, with pinned package versions.
#
# Today the gateway's packages live in the system Python, unpinned, installed
# with --break-system-packages, so an OS update or an unrelated pip install
# can change them under it. This moves the service onto a venv built from
# requirements.lock. Nothing changes until you run `use`, and `unuse` puts
# the service back on /usr/bin/python3.
#
#   scripts/venv.sh freeze   write requirements.lock from the system Python
#                            (run once on the gateway; commit the result)
#   scripts/venv.sh build    create the venv and install the lock into it
#   scripts/venv.sh check    import everything with the venv's Python and
#                            run the test suite with it
#   scripts/venv.sh use      point the service at the venv (systemd drop-in);
#                            takes effect on the next restart
#   scripts/venv.sh unuse    remove the drop-in; back to /usr/bin/python3
#   scripts/venv.sh status   which Python the service will use
#
# The venv is made with --system-site-packages so apt-provided modules
# (SoapySDR, gi, ...) stay visible. Locked packages are installed into the
# venv and take precedence over the system copies.
#
# VENV defaults to ~/.radio-gateway-venv; override with VENV=/path.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-$HOME/.radio-gateway-venv}"
LOCK="$REPO/requirements.lock"
DROPIN_DIR=/etc/systemd/system/radio-gateway.service.d
DROPIN="$DROPIN_DIR/venv.conf"
KV4P_SRC="${KV4P_SRC:-$HOME/kv4p-ht-python}"

die() { echo "ERROR: $*" >&2; exit 1; }

cmd_freeze() {
    /usr/bin/python3 "$REPO/tools/freeze_lock.py"
}

cmd_build() {
    [ -f "$LOCK" ] || die "no $LOCK; run '$0 freeze' on the gateway first"
    if [ ! -x "$VENV/bin/python3" ]; then
        echo "Creating $VENV"
        /usr/bin/python3 -m venv --system-site-packages "$VENV"
    fi
    "$VENV/bin/python3" -m pip install -q --upgrade pip
    # --no-deps: the lock already lists every dependency at its exact version.
    "$VENV/bin/python3" -m pip install --no-deps -r "$LOCK"
    if [ -d "$KV4P_SRC" ]; then
        "$VENV/bin/python3" -m pip install --no-deps -e "$KV4P_SRC"
    else
        echo "  note: $KV4P_SRC not found; kv4p endpoints will use the system copy if any"
    fi
    echo "Built $VENV from $(grep -vc '^#' "$LOCK") locked packages."
    echo "Next: $0 check"
}

cmd_check() {
    [ -x "$VENV/bin/python3" ] || die "no venv at $VENV; run '$0 build'"
    echo "== hardware and Mumble libraries"
    (cd "$REPO" && "$VENV/bin/python3" -c "import hw_deps; hw_deps.require(); print('  ok')")
    echo "== installed versions match requirements.lock"
    (cd "$REPO" && "$VENV/bin/python3" tools/freeze_lock.py --check && echo "  ok")
    echo "== pip's own consistency check (warnings only)"
    "$VENV/bin/python3" -m pip check || true
    echo "== test suite with the venv's Python"
    (cd "$REPO" && "$VENV/bin/python3" tests/run_all.py)
}

cmd_use() {
    [ -x "$VENV/bin/python3" ] || die "no venv at $VENV; run '$0 build' and '$0 check'"
    sudo mkdir -p "$DROPIN_DIR"
    printf '[Service]\nExecStart=\nExecStart=%s %s\n' \
        "$VENV/bin/python3" "$REPO/radio_gateway.py" | sudo tee "$DROPIN" > /dev/null
    sudo systemctl daemon-reload
    echo "The service will run $VENV/bin/python3 from its next restart."
    echo "Restart when convenient:  sudo systemctl restart radio-gateway"
    echo "To go back:               $0 unuse"
}

cmd_unuse() {
    if [ -f "$DROPIN" ]; then
        sudo rm -f "$DROPIN"
        sudo systemctl daemon-reload
        echo "Removed $DROPIN. The service will run /usr/bin/python3 from its next restart."
    else
        echo "No drop-in installed; the service already uses /usr/bin/python3."
    fi
}

cmd_status() {
    if [ -f "$DROPIN" ]; then
        echo "Service is set to use: $(sed -n 's/^ExecStart=\(.\+\) .*/\1/p' "$DROPIN")"
    else
        echo "Service is set to use: /usr/bin/python3 (no venv drop-in)"
    fi
    systemctl show radio-gateway -p ExecStart 2>/dev/null | sed 's/^/  /' || true
}

case "${1:-}" in
    freeze|build|check|use|unuse|status) "cmd_$1" ;;
    *) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
