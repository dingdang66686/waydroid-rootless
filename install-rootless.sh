#!/usr/bin/env bash
#
# install-rootless.sh - switch the system Waydroid to the rootless podman
# backend, or roll it back.
#
#   ./install-rootless.sh install
#   ./install-rootless.sh uninstall
#
# The runtime contract is: one OCI image (podman_image) + one named volume
# (podman_data_volume) + the Wayland/Pulse/DRI sockets.  The image is published
# by the waydroid-rootless-image repository (or built locally); this script
# never builds it.
#
# Everything is reversible: the distro package is not modified, the rootful
# service is only disabled, and /var/lib/waydroid/data is left untouched.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE"
PREFIX="/opt/waydroid-rootless"
WRAPPER="/usr/local/bin/waydroid"
WORK="${WAYDROID_WORK:-${XDG_STATE_HOME:-$HOME/.local/state}/waydroid}"
IMAGE="${WAYDROID_IMAGE:-ghcr.io/dingdang66686/waydroid-rootless-image:13}"
VOLUME="${WAYDROID_DATA_VOLUME:-waydroid-data}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/waydroid.service"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your desktop user; it uses sudo only for system parts." >&2
    exit 1
fi

install_rootless() {
    command -v podman >/dev/null || {
        echo "podman is required" >&2
        exit 1
    }
    if ! podman image exists "$IMAGE"; then
        echo "WARNING: $IMAGE is not in this user's podman storage." >&2
        echo "Pull it with 'podman pull $IMAGE', or build it from" >&2
        echo "https://github.com/dingdang66686/waydroid-rootless-image." >&2
    fi

    sudo mkdir -p "$PREFIX"
    sudo rm -rf "$PREFIX/tools" "$PREFIX/waydroid.py"
    sudo cp -a "$SRC/tools" "$SRC/waydroid.py" "$PREFIX/"
    sudo find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} +

    sudo tee "$WRAPPER" >/dev/null <<'WRAPPER_EOF'
#!/bin/sh
# Rootless Waydroid launcher.  Root keeps using the distro package.
if [ "$(id -u)" -eq 0 ]; then
    exec /usr/bin/waydroid "$@"
fi
export WAYDROID_BACKEND=podman
export WAYDROID_WORK="${WAYDROID_WORK:-${XDG_STATE_HOME:-$HOME/.local/state}/waydroid}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
exec python3 /opt/waydroid-rootless/waydroid.py "$@"
WRAPPER_EOF
    sudo chmod 755 "$WRAPPER"

    mkdir -p "$WORK"
    if [ ! -f "$WORK/waydroid.cfg" ]; then
        cat > "$WORK/waydroid.cfg" <<CFG_EOF
[waydroid]
container_backend = podman
podman_image = $IMAGE
podman_image_remote = $IMAGE
podman_data_volume = $VOLUME
podman_binderfs = container

[properties]
CFG_EOF
        echo "Wrote $WORK/waydroid.cfg"
    else
        echo "Keeping existing $WORK/waydroid.cfg"
    fi

    mkdir -p "$UNIT_DIR"
    cp "$SRC/systemd/waydroid-session.service" "$UNIT"

    if systemctl list-unit-files waydroid-container.service >/dev/null 2>&1; then
        echo "Disabling rootful waydroid-container.service"
        sudo systemctl disable --now waydroid-container.service || true
    fi

    systemctl --user daemon-reload
    systemctl --user enable --now waydroid.service
    echo
    echo "Installed. Check:"
    echo "  systemctl --user status waydroid.service"
    echo "  waydroid status"
    echo "Logs: journalctl --user -u waydroid.service -f"
}

uninstall_rootless() {
    systemctl --user disable --now waydroid.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload
    sudo rm -rf "$PREFIX" "$WRAPPER"
    if systemctl list-unit-files waydroid-container.service >/dev/null 2>&1; then
        sudo systemctl enable --now waydroid-container.service || true
    fi
    echo "Removed the rootless service and launcher."
    echo "Kept work dir: $WORK"
    echo "Kept data volume: $VOLUME (podman volume rm $VOLUME to delete it)"
}

case "${1:-install}" in
    install) install_rootless ;;
    uninstall) uninstall_rootless ;;
    *) echo "usage: $0 [install|uninstall]" >&2; exit 1 ;;
esac
