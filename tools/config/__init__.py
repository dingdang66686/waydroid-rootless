# Copyright 2021 Oliver Smith
# SPDX-License-Identifier: GPL-3.0-or-later
import os
import pwd

#
# Exported functions
#
from tools.config.load import load, load_channels
from tools.config.save import save

#
# Exported variables (internal configuration)
#
version = "1.6.3"
tools_src = os.path.normpath(os.path.realpath(__file__) + "/../../..")

# Keys saved in the config file (mostly what we ask in 'waydroid init')
config_keys = ["arch",
               "images_path",
               "vendor_type",
               "system_datetime",
               "vendor_datetime",
               "suspend_action",
               "mount_overlays",
               "auto_adb",
               "container_backend",
               "podman_image",
               "podman_image_remote",
               "podman_data_volume",
               "podman_adb_port",
               "podman_binderfs",
               "podman_uid_map",
               "podman_pasta_options",
               "podman_overlay_path",
               "podman_overlay_image"]

# Config file/commandline default values
# $WORK gets replaced with the actual value for args.work (which may be
# overridden on the commandline)
defaults = {
    "arch": "arm64",
    "work": "/var/lib/waydroid",
    "vendor_type": "MAINLINE",
    "system_datetime": "0",
    "vendor_datetime": "0",
    "preinstalled_images_paths": [
        "/etc/waydroid-extra/images",
        "/usr/share/waydroid-extra/images",
    ],
    "suspend_action": "freeze",
    "mount_overlays": "True",
    "auto_adb": "False",
    "container_xdg_runtime_dir": "/run/xdg",
    "container_wayland_display": "wayland-0",
    # Container runtime backend: "lxc" (the historical rootful runtime) or
    # "podman" (rootless).  See tools/helpers/runtime/ and
    # docs/rfc/rootless-backend.md.
    "container_backend": "lxc",
    # podman backend knobs (ignored by the lxc backend):
    #   podman_image     - required: the OCI image to boot.  There is no
    #                      external-rootfs mode; how the image is built is the
    #                      build system's concern (see waydroid-rootless/image).
    #   podman_image_remote - image ref pulled by `waydroid upgrade` and then
    #                      tagged as podman_image (placeholder until images are
    #                      published; a failed pull leaves the running image
    #                      untouched).  `waydroid upgrade --offline` skips the
    #                      pull and only restarts the user service.
    #   podman_data_volume - named podman volume mounted at Android's /data
    #                      (default "waydroid-data").  Wipe it with
    #                      `podman volume rm <name>`.  Upstream semantics: the
    #                      backend never wipes /data on an Android upgrade.
    #   podman_adb_port  - host loopback TCP port forwarded by pasta to the
    #                      container's adbd (default 5555; "0" disables).
    #                      `waydroid adb connect` then uses 127.0.0.1:<port>.
    #   podman_binderfs  - "container" (binderfs created inside the container
    #                      by devstage; the host reaches Android's service
    #                      manager through the wd-broker fd pass, so the
    #                      gbinder session services work without root) or
    #                      "host" (legacy: share the host's /dev/binderfs
    #                      nodes; rootless cannot claim the context manager)
    #   podman_uid_map   - "auto" (derive from /etc/subuid), "keep-id" (let
    #                      podman generate the mapping) or an explicit
    #                      --userns= value
    #   podman_pasta_options - extra comma-separated pasta options appended to
    #                      the --network string
    #   podman_overlay_path - local overlay directory checked at every
    #                      container start (default: $WORK/overlay).  It uses
    #                      upstream's lowerdir layout: system/, vendor/ and
    #                      other rootfs-relative paths.  props/ and remove.list
    #                      are optional extensions.  The result is imported as
    #                      podman_overlay_image.  mount_overlays=False disables
    #                      it; empty/absent means the normal public-image path.
    #   podman_overlay_image - derived image tag for the local overlay
    #                      (default: localhost/waydroid-overlay:<base tag>).
    #                      It must differ from podman_image.
    "podman_image": "",
    "podman_image_remote": "registry.example.invalid/waydroid/waydroid-arm64:13",
    "podman_data_volume": "waydroid-data",
    "podman_adb_port": "5555",
    "podman_binderfs": "container",
    "podman_uid_map": "auto",
    "podman_pasta_options": "",
    "podman_overlay_path": "",
    "podman_overlay_image": "",
}
defaults["images_path"] = defaults["work"] + "/images"
defaults["rootfs"] = defaults["work"] + "/rootfs"
defaults["overlay"] = defaults["work"] + "/overlay"
defaults["overlay_rw"] = defaults["work"] + "/overlay_rw"
defaults["overlay_work"] = defaults["work"] + "/overlay_work"
defaults["data"] = defaults["work"] + "/data"
defaults["lxc"] = defaults["work"] + "/lxc"
defaults["host_perms"] = defaults["work"] + "/host-permissions"
defaults["container_pulse_runtime_path"] = defaults["container_xdg_runtime_dir"] + "/pulse"


def rebase(work):
    """Re-derive the $WORK-relative default paths for a custom work directory.

    Upstream assumes $WORK is /var/lib/waydroid (root-owned) and every derived
    path is computed once at import time.  A rootless backend keeps its state in
    a user-writable directory instead, so tools.prep_args() calls this with the
    requested path and all the /var/lib/waydroid-derived defaults follow.
    """
    work = os.path.normpath(os.path.expanduser(work))
    defaults["work"] = work
    defaults["images_path"] = work + "/images"
    defaults["rootfs"] = work + "/rootfs"
    defaults["overlay"] = work + "/overlay"
    defaults["overlay_rw"] = work + "/overlay_rw"
    defaults["overlay_work"] = work + "/overlay_work"
    defaults["data"] = work + "/data"
    defaults["lxc"] = work + "/lxc"
    defaults["host_perms"] = work + "/host-permissions"

session_defaults = {
    "user_name": pwd.getpwuid(os.getuid()).pw_name,
    "user_id": str(os.getuid()),
    "group_id": str(os.getgid()),
    "host_user": os.path.expanduser("~"),
    "pid": str(os.getpid()),
    "xdg_data_home": str(os.environ.get('XDG_DATA_HOME', os.path.expanduser("~") + "/.local/share")),
    "xdg_runtime_dir": str(os.environ.get('XDG_RUNTIME_DIR')),
    "wayland_display": str(os.environ.get('WAYLAND_DISPLAY')),
    "pulse_runtime_path": str(os.environ.get('PULSE_RUNTIME_PATH')),
    "state": "STOPPED",
    "lcd_density": "0",
    "background_start": "true"
}
session_defaults["waydroid_user_state"] = session_defaults["xdg_data_home"] + "/waydroid"
session_defaults["waydroid_data"] = session_defaults["waydroid_user_state"] + "/data"
if session_defaults["pulse_runtime_path"] == "None":
    session_defaults["pulse_runtime_path"] = session_defaults["xdg_runtime_dir"] + "/pulse"

channels_defaults = {
    "config_path": "/usr/share/waydroid-extra/channels.cfg",
    "system_channel": "https://ota.waydro.id/system",
    "vendor_channel": "https://ota.waydro.id/vendor",
    "rom_type": "lineage",
    "system_type": "VANILLA"
}
channels_config_keys = ["system_channel",
                        "vendor_channel",
                        "rom_type",
                        "system_type"]
